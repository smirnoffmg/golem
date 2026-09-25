"""Server-side sessions and login transactions in golem_ui (ADR 0011).

The browser holds only a random session id; the row is keyed by its hash, so reading the table
gives no cookie that works. Tokens and PKCE verifiers are encrypted with a Fernet key the
database never sees.
"""

import asyncio
import hashlib
import secrets
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime

import psycopg
from cryptography.fernet import Fernet, InvalidToken

from golem.ui.oidc import Tokens

# A login transaction (state, nonce, verifier) lives this long and is used at most once.
LOGIN_LIFETIME_SECONDS = 600
# However often the access token is refreshed, the user signs in again after this long
# (ASVS 5.0, 7.3.2).
SESSION_LIFETIME_SECONDS = 12 * 3600
CONNECT_TIMEOUT_SECONDS = 2
# golem_ui's CONNECTION LIMIT (10) is shared by both replicas and a surge pod during a rollout.
CONNECTIONS = 3
# A renewal holds its connection across an identity provider call (up to 4 s), so a request
# waits longer than that for a free one before it fails.
CONNECTION_WAIT_SECONDS = 6

SCHEMA = """
CREATE TABLE IF NOT EXISTS logins (
    state         text        PRIMARY KEY,
    binding       text        NOT NULL,
    nonce         text        NOT NULL,
    code_verifier text        NOT NULL,
    expires_at    timestamptz NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    id            text        PRIMARY KEY,
    subject       text        NOT NULL,
    name          text        NOT NULL,
    access_token  text        NOT NULL,
    refresh_token text,
    id_token      text,
    expires_at    timestamptz NOT NULL,
    csrf          text        NOT NULL,
    created_at    timestamptz NOT NULL
);
"""

SESSION_COLUMNS = (
    "id, subject, name, access_token, refresh_token, id_token, expires_at, csrf, created_at"
)


@dataclass(frozen=True)
class Login:
    binding: str
    nonce: str
    verifier: str = field(repr=False)


@dataclass(frozen=True)
class Session:
    id: str
    subject: str
    name: str
    access_token: str = field(repr=False)
    refresh_token: str | None = field(repr=False)
    id_token: str | None = field(repr=False)
    expires_at: float
    csrf: str = field(repr=False)
    created_at: float


Renew = Callable[[Session], Awaitable[Tokens | None]]


async def apply_schema(conn: psycopg.AsyncConnection) -> None:
    await conn.execute(SCHEMA)


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class SessionStore:
    def __init__(
        self,
        dsn: str,
        key: str,
        *,
        clock: Callable[[], float] = time.time,
        connections: int = CONNECTIONS,
    ) -> None:
        self._dsn = dsn
        self._fernet = Fernet(key)
        self._clock = clock
        self._slots = asyncio.Semaphore(connections)

    async def begin_login(self, *, state: str, binding: str, nonce: str, verifier: str) -> None:
        async with self._connection() as conn:
            await conn.execute("DELETE FROM logins WHERE expires_at < %s", (self._at(0),))
            await conn.execute(
                "INSERT INTO logins (state, binding, nonce, code_verifier, expires_at)"
                " VALUES (%s, %s, %s, %s, %s)",
                (
                    state,
                    digest(binding),
                    nonce,
                    self._seal(verifier),
                    self._at(LOGIN_LIFETIME_SECONDS),
                ),
            )

    async def take_login(self, state: str) -> Login | None:
        """The login transaction for ``state``, removed: a state is used once, then gone."""
        async with self._connection() as conn:
            cursor = await conn.execute(
                "DELETE FROM logins WHERE state = %s RETURNING binding, nonce, code_verifier,"
                " expires_at",
                (state,),
            )
            row = await cursor.fetchone()
        if row is None or row[3].timestamp() <= self._clock():
            return None
        verifier = self._open(row[2])
        return None if verifier is None else Login(binding=row[0], nonce=row[1], verifier=verifier)

    async def create(
        self, *, subject: str, name: str, tokens: Tokens, replacing: str | None
    ) -> tuple[str, Session]:
        """A new session and the cookie value naming it; the browser's old session ends
        (ASVS 5.0, 7.2.4)."""
        cookie = secrets.token_urlsafe(32)
        now = self._clock()
        session = Session(
            id=digest(cookie),
            subject=subject,
            name=name,
            access_token=tokens.access_token,
            refresh_token=tokens.refresh_token,
            id_token=tokens.id_token,
            expires_at=now + tokens.expires_in,
            csrf=secrets.token_urlsafe(32),
            created_at=now,
        )
        async with self._connection() as conn, conn.transaction():
            if replacing:
                await conn.execute("DELETE FROM sessions WHERE id = %s", (digest(replacing),))
            await conn.execute(
                "DELETE FROM sessions WHERE created_at < %s",
                (self._at(-SESSION_LIFETIME_SECONDS),),
            )
            await conn.execute(
                f"INSERT INTO sessions ({SESSION_COLUMNS})"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    session.id,
                    subject,
                    name,
                    self._seal(session.access_token),
                    self._seal_optional(session.refresh_token),
                    self._seal_optional(session.id_token),
                    _timestamp(session.expires_at),
                    session.csrf,
                    _timestamp(now),
                ),
            )
        return cookie, session

    async def get(self, cookie: str) -> Session | None:
        async with self._connection() as conn:
            cursor = await conn.execute(
                f"SELECT {SESSION_COLUMNS} FROM sessions WHERE id = %s", (digest(cookie),)
            )
            row = await cursor.fetchone()
        return self._live(row)

    async def renew(self, cookie: str, renew: Renew) -> Session | None:
        """The session with its tokens renewed by ``renew``, or None, and the session ended, if
        renewing fails. The row is locked meanwhile, so concurrent requests refresh once: a
        rotated refresh token works only once."""
        async with self._connection() as conn, conn.transaction():
            cursor = await conn.execute(
                f"SELECT {SESSION_COLUMNS} FROM sessions WHERE id = %s FOR UPDATE",
                (digest(cookie),),
            )
            session = self._live(await cursor.fetchone())
            if session is None:
                return None
            if not self.needs_refresh(session):
                return session
            tokens = await renew(session)
            if tokens is None:
                await conn.execute("DELETE FROM sessions WHERE id = %s", (session.id,))
                return None
            renewed = Session(
                id=session.id,
                subject=session.subject,
                name=session.name,
                access_token=tokens.access_token,
                # RFC 6749, 6: a new refresh token replaces the old; without one the old stays.
                refresh_token=tokens.refresh_token or session.refresh_token,
                id_token=tokens.id_token or session.id_token,
                expires_at=self._clock() + tokens.expires_in,
                csrf=session.csrf,
                created_at=session.created_at,
            )
            await conn.execute(
                "UPDATE sessions SET access_token = %s, refresh_token = %s, id_token = %s,"
                " expires_at = %s WHERE id = %s",
                (
                    self._seal(renewed.access_token),
                    self._seal_optional(renewed.refresh_token),
                    self._seal_optional(renewed.id_token),
                    _timestamp(renewed.expires_at),
                    session.id,
                ),
            )
            return renewed

    async def delete(self, cookie: str) -> Session | None:
        async with self._connection() as conn:
            cursor = await conn.execute(
                f"DELETE FROM sessions WHERE id = %s RETURNING {SESSION_COLUMNS}",
                (digest(cookie),),
            )
            return self._decoded(await cursor.fetchone())

    def needs_refresh(self, session: Session, margin_seconds: float = 60) -> bool:
        return session.expires_at - margin_seconds <= self._clock()

    def _live(self, row: tuple | None) -> Session | None:
        session = self._decoded(row)
        if session is None or session.created_at + SESSION_LIFETIME_SECONDS <= self._clock():
            return None
        return session

    def _decoded(self, row: tuple | None) -> Session | None:
        if row is None:
            return None
        access_token = self._open(row[3])
        if access_token is None:
            return None
        return Session(
            id=row[0],
            subject=row[1],
            name=row[2],
            access_token=access_token,
            refresh_token=self._open(row[4]) if row[4] else None,
            id_token=self._open(row[5]) if row[5] else None,
            expires_at=row[6].timestamp(),
            csrf=row[7],
            created_at=row[8].timestamp(),
        )

    @asynccontextmanager
    async def _connection(self) -> AsyncIterator[psycopg.AsyncConnection]:
        try:
            await asyncio.wait_for(self._slots.acquire(), CONNECTION_WAIT_SECONDS)
        except TimeoutError as error:
            raise psycopg.OperationalError("no database connection became free") from error
        try:
            async with await psycopg.AsyncConnection.connect(
                self._dsn, autocommit=True, connect_timeout=CONNECT_TIMEOUT_SECONDS
            ) as conn:
                yield conn
        finally:
            self._slots.release()

    def _at(self, offset_seconds: float) -> datetime:
        return _timestamp(self._clock() + offset_seconds)

    def _seal(self, value: str) -> str:
        return self._fernet.encrypt(value.encode()).decode("ascii")

    def _seal_optional(self, value: str | None) -> str | None:
        return None if value is None else self._seal(value)

    def _open(self, sealed: str) -> str | None:
        # A row sealed with another key (a rotated one) is no session: the user signs in again.
        try:
            return self._fernet.decrypt(sealed.encode("ascii")).decode()
        except InvalidToken:
            return None


def _timestamp(seconds: float) -> datetime:
    return datetime.fromtimestamp(seconds, UTC)
