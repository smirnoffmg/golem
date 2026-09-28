"""The web UI's backend-for-frontend, end to end: browser -> BFF -> real edge -> real task
service with its Postgres store, with the identity provider faked at its HTTP boundary.

The identity provider is tests/support/idp.py. The board's JSON API is ADR 0018.
"""

import asyncio
import base64
import hashlib
import re
import secrets
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from decimal import Decimal
from functools import partial
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import jwt
import psycopg
import pytest
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from support.idp import (
    AUTHORIZE_PATH,
    CLIENT_ID,
    CLIENT_SECRET,
    DISCOVERY_PATH,
    EDGE_AUDIENCE,
    END_SESSION_PATH,
    IDP_KEY,
    ISSUER,
    JWKS_PATH,
    PUBLIC_URL,
    Clock,
    FakeIdP,
)
from test_edge_app import EDGE_TOKEN, agent_card
from test_tasks_service import FakeOrchestrator, make_card
from test_tasks_to_runs import CATALOG, SIGNING_KEY, TEMPLATE, FakeLauncher
from testcontainers.community.postgres import PostgresContainer

from golem.edge.__main__ import authenticator
from golem.edge.app import create_edge_app
from golem.edge.policy import ChainLimits, Registry
from golem.jwks import SigningKeys, fetch_jwks
from golem.metrics import Metrics
from golem.orchestrator.admission import Limits
from golem.orchestrator.service import PostgresOrchestrator
from golem.ratelimit import Limiter, Rate, parse_networks
from golem.settings import SettingsError, ui_settings
from golem.tasks.app import OUTCOME_PATH, PROPOSAL_STATE_PATH, create_listeners
from golem.tasks.ports import Orchestrator, ProposalRecord, ProposalView
from golem.tasks.store import tasks_engine, tasks_store
from golem.ui.__main__ import prepare
from golem.ui.app import CSRF_HEADER, SESSION_COOKIE, create_ui_app
from golem.ui.oidc import CLOCK_SKEW_SECONDS, OidcClient, Tokens
from golem.ui.store import SessionStore, apply_schema

DISCOVERY_URL = f"{ISSUER}{DISCOVERY_PATH}"
AUTHORIZE_URL = f"{ISSUER}{AUTHORIZE_PATH}"
JWKS_URL = f"{ISSUER}{JWKS_PATH}"
END_SESSION_URL = f"{ISSUER}{END_SESSION_PATH}"
REDIRECT_URL = f"{PUBLIC_URL}/callback"
# Everyone may call discovery, only bob reviewer; evaluator has a card but no caller.
REGISTRY = Registry(
    allowed_callers={
        "discovery": frozenset({"user:*"}),
        "reviewer": frozenset({"user:bob"}),
    }
)
CARDS = {
    name: agent_card(name, *skills)
    for name, skills in (
        ("discovery", ("research",)),
        ("reviewer", ("review",)),
        ("evaluator", ("evaluate",)),
    )
}
ROGUE_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
SESSION_KEY = Fernet.generate_key().decode()
CSP = "default-src 'self'; frame-ancestors 'none'; form-action 'self'; base-uri 'none'"
PKCE_ALPHABET = re.compile(r"^[A-Za-z0-9._~-]{43,128}$")
MR_URL = "https://gitlab.example.test/golem/discovery/-/merge_requests/7"


def ui_dsn_of(postgres: PostgresContainer, user: str = "golem_ui") -> str:
    host = postgres.get_container_host_ip()
    port = postgres.get_exposed_port(5432)
    password = f"dev-only-{user.replace('_', '-')}"
    return f"host={host} port={port} dbname=golem_ui user={user} password={password}"


@pytest.fixture
async def ui_db(postgres: PostgresContainer) -> AsyncIterator[str]:
    dsn = ui_dsn_of(postgres)
    async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as conn:
        await apply_schema(conn)
        await conn.execute("TRUNCATE sessions, logins")
    yield dsn


@pytest.fixture
async def engine(postgres: PostgresContainer) -> AsyncIterator[AsyncEngine]:
    # The task service's own store, so ListTasks filters by agent as in production.
    host = postgres.get_container_host_ip()
    port = postgres.get_exposed_port(5432)
    engine = tasks_engine(
        f"postgresql+asyncpg://golem_tasks:dev-only-golem-tasks@{host}:{port}/golem_tasks"
    )
    await tasks_store(engine).initialize()
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM tasks"))
    yield engine
    await engine.dispose()


class Recording(httpx.AsyncBaseTransport):
    """The UI's calls to the edge, by path and A2A method, passed on to the real edge."""

    def __init__(self, inner: httpx.AsyncBaseTransport) -> None:
        self.inner = inner
        self.calls: list[str] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        method = re.search(rb'"method":\s*"(\w+)"', request.content or b"")
        self.calls.append(method[1].decode() if method else request.url.path)
        return await self.inner.handle_async_request(request)


@dataclass
class Stack:
    app: Any
    idp: FakeIdP
    clock: Clock
    orchestrator: Any
    dsn: str
    edge: Recording
    internal: httpx.AsyncClient
    names: dict[str, str] = field(default_factory=dict)

    def browser(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url=PUBLIC_URL)


def build_stack(
    audit_dsn: str,
    ui_dsn: str,
    orchestrator: Orchestrator,
    engine: AsyncEngine | None = None,
    edge_callers: Limiter | None = None,
    **ui_options: Any,
) -> Stack:
    clock = Clock()
    idp = FakeIdP(clock)
    idp_sync = httpx.Client(transport=httpx.MockTransport(idp.handle))
    edge_keys = SigningKeys(partial(fetch_jwks, idp_sync, JWKS_URL))
    edge_keys.refresh()
    listeners = create_listeners(
        make_card(),
        orchestrator,
        edge_token=EDGE_TOKEN,
        task_store=tasks_store(engine) if engine is not None else None,
    )
    edge = create_edge_app(
        authenticate=authenticator(edge_keys, issuer=ISSUER, audience=EDGE_AUDIENCE),
        registry=REGISTRY,
        limits=ChainLimits(max_depth=3),
        audit_dsn=audit_dsn,
        forward=httpx.AsyncClient(
            transport=httpx.ASGITransport(app=listeners.public), base_url="http://tasks"
        ),
        edge_token=EDGE_TOKEN,
        cards=CARDS,
        callers=edge_callers,
    )
    recording = Recording(httpx.ASGITransport(app=edge))
    app = create_ui_app(
        oidc=OidcClient(
            http=httpx.AsyncClient(transport=httpx.MockTransport(idp.handle)),
            keys_http=idp_sync,
            issuer=ISSUER,
            discovery_url=DISCOVERY_URL,
            client_id=CLIENT_ID,
            client_secret=CLIENT_SECRET,
            redirect_url=REDIRECT_URL,
            clock=clock,
        ),
        store=SessionStore(ui_dsn, SESSION_KEY, clock=clock),
        edge=httpx.AsyncClient(transport=recording, base_url="http://edge"),
        public_base_url=PUBLIC_URL,
        **({"clock": clock} | ui_options),
    )
    internal = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=listeners.internal_write), base_url="http://tasks"
    )
    return Stack(app, idp, clock, orchestrator, ui_dsn, recording, internal)


@pytest.fixture
async def stack(audit_dsn: str, audit_admin_dsn: str, ui_db: str, engine: AsyncEngine) -> Stack:
    return build_stack(audit_dsn, ui_db, FakeOrchestrator(), engine)


@pytest.fixture
async def browser(stack: Stack) -> AsyncIterator[httpx.AsyncClient]:
    async with stack.browser() as client:
        yield client


def query_of(location: str) -> dict[str, str]:
    return {k: v for k, [v] in parse_qs(urlsplit(location).query).items()}


async def start_login(browser: httpx.AsyncClient) -> str:
    response = await browser.get("/login")
    assert response.status_code == 303, response.text
    return response.headers["location"]


async def login(stack: Stack, browser: httpx.AsyncClient, user: str = "alice") -> httpx.Response:
    location = await start_login(browser)
    code = stack.idp.authorize(location, user)
    response = await browser.get(
        "/callback", params={"code": code, "state": query_of(location)["state"]}
    )
    assert response.status_code == 303, response.text
    return response


async def csrf_of(browser: httpx.AsyncClient) -> str:
    response = await browser.get("/api/session")
    assert response.status_code == 200, response.text
    return str(response.json()["csrf"])


async def post(
    browser: httpx.AsyncClient,
    path: str,
    body: dict[str, Any] | None = None,
    csrf: str | None = "",
) -> httpx.Response:
    """A JSON POST with the session's CSRF token, or the given one; None sends none."""
    headers = {"Content-Type": "application/json"}
    token = await csrf_of(browser) if csrf == "" else csrf
    if token is not None:
        headers[CSRF_HEADER] = token
    return await browser.post(path, json=body or {}, headers=headers)


def nonce() -> str:
    return secrets.token_urlsafe(24)


async def start_task(
    browser: httpx.AsyncClient,
    goal: str = "fix the test",
    agent: str = "discovery",
    **options: Any,
) -> httpx.Response:
    return await post(
        browser, f"/api/agents/{agent}/tasks", {"goal": goal, "nonce": nonce()}, **options
    )


async def started_id(browser: httpx.AsyncClient, goal: str = "fix the test") -> str:
    response = await start_task(browser, goal)
    assert response.status_code == 201, response.text
    return str(response.json()["task"]["id"])


async def board(browser: httpx.AsyncClient, agent: str = "discovery", **params: str) -> Any:
    response = await browser.get(f"/api/agents/{agent}/board", params=params)
    assert response.status_code == 200, response.text
    return response.json()


async def session_rows(dsn: str) -> list[tuple[Any, ...]]:
    async with await psycopg.AsyncConnection.connect(dsn) as conn:
        cursor = await conn.execute("SELECT * FROM sessions")
        return await cursor.fetchall()


def signin_code(response: httpx.Response) -> str:
    assert response.status_code == 303, response.text
    location = response.headers["location"]
    assert location.startswith("/?signin="), location
    return query_of(location)["signin"]


# --- Login ---------------------------------------------------------------------------------------


async def test_login_redirects_to_the_idp_with_state_nonce_and_an_s256_challenge(
    stack: Stack, browser: httpx.AsyncClient
) -> None:
    location = await start_login(browser)

    params = query_of(location)
    assert location.startswith(f"{AUTHORIZE_URL}?")
    assert params["response_type"] == "code"
    assert params["scope"].split()[0] == "openid"
    assert params["client_id"] == CLIENT_ID
    assert params["redirect_uri"] == REDIRECT_URL
    assert params["code_challenge_method"] == "S256"
    assert len(params["state"]) >= 22 and len(params["nonce"]) >= 22
    assert len(params["code_challenge"]) == 43
    assert "code_verifier" not in params


async def test_the_verifier_sent_to_the_token_endpoint_hashes_to_the_challenge(
    stack: Stack, browser: httpx.AsyncClient
) -> None:
    location = await start_login(browser)
    challenge = query_of(location)["code_challenge"]

    code = stack.idp.authorize(location, "alice")
    await browser.get("/callback", params={"code": code, "state": query_of(location)["state"]})

    [request] = stack.idp.token_requests
    verifier = request["code_verifier"][0]
    assert PKCE_ALPHABET.fullmatch(verifier)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    assert base64.urlsafe_b64encode(digest).rstrip(b"=").decode() == challenge
    assert request["grant_type"] == ["authorization_code"]
    assert request["redirect_uri"] == [REDIRECT_URL]


async def test_a_successful_login_sets_only_an_opaque_session_cookie(
    stack: Stack, browser: httpx.AsyncClient
) -> None:
    response = await login(stack, browser)

    assert response.headers["location"] == "/"
    [session_header] = [
        h for h in response.headers.get_list("set-cookie") if h.startswith(f"{SESSION_COOKIE}=")
    ]
    name_value, *attributes = [part.strip() for part in session_header.split(";")]
    assert sorted(attributes) == sorted(["Path=/", "Secure", "HttpOnly", "SameSite=Lax"])
    value = name_value.split("=", 1)[1]
    assert re.fullmatch(r"[A-Za-z0-9_-]{43}", value)
    assert "." not in value, "a JWT or any token must not reach the browser"
    assert SESSION_COOKIE == "__Host-golem-session"


async def test_the_login_transaction_cookie_is_host_bound_and_cleared_after_login(
    stack: Stack, browser: httpx.AsyncClient
) -> None:
    started = await browser.get("/login")
    [login_header] = started.headers.get_list("set-cookie")
    name, *attributes = [part.strip() for part in login_header.split(";")]
    assert name.startswith("__Host-")
    assert {"Path=/", "Secure", "HttpOnly", "SameSite=Lax"} <= set(attributes)
    assert "Max-Age=600" in attributes

    location = started.headers["location"]
    code = stack.idp.authorize(location, "alice")
    done = await browser.get(
        "/callback", params={"code": code, "state": query_of(location)["state"]}
    )
    cleared = [h for h in done.headers.get_list("set-cookie") if h.startswith(name.split("=")[0])]
    assert cleared and "Max-Age=0" in cleared[0]


async def test_a_state_is_single_use(stack: Stack, browser: httpx.AsyncClient) -> None:
    location = await start_login(browser)
    state = query_of(location)["state"]
    await browser.get(
        "/callback", params={"code": stack.idp.authorize(location, "alice"), "state": state}
    )
    browser.cookies.clear()
    await start_login(browser)

    replay = await browser.get(
        "/callback", params={"code": stack.idp.authorize(location, "alice"), "state": state}
    )

    assert signin_code(replay) == "expired"
    assert len(stack.idp.token_requests) == 1


async def test_a_state_expires_after_ten_minutes(stack: Stack, browser: httpx.AsyncClient) -> None:
    location = await start_login(browser)
    code = stack.idp.authorize(location, "alice")
    stack.clock.now += 601

    response = await browser.get(
        "/callback", params={"code": code, "state": query_of(location)["state"]}
    )

    assert signin_code(response) == "expired"
    assert stack.idp.token_requests == []
    assert await session_rows(stack.dsn) == []


async def test_a_state_from_another_browser_is_refused(stack: Stack) -> None:
    async with stack.browser() as victim, stack.browser() as attacker:
        location = await start_login(attacker)
        code = stack.idp.authorize(location, "mallory")

        response = await victim.get(
            "/callback", params={"code": code, "state": query_of(location)["state"]}
        )

    assert signin_code(response) == "expired"
    assert stack.idp.token_requests == []


async def test_an_unknown_state_is_refused(stack: Stack, browser: httpx.AsyncClient) -> None:
    await start_login(browser)

    response = await browser.get("/callback", params={"code": "x", "state": "made-up"})

    assert signin_code(response) == "expired"
    assert stack.idp.token_requests == []


async def test_an_error_from_the_idp_is_not_echoed_and_starts_no_session(
    stack: Stack, browser: httpx.AsyncClient
) -> None:
    location = await start_login(browser)

    response = await browser.get(
        "/callback",
        params={"error": "access_denied<script>", "state": query_of(location)["state"]},
    )

    assert signin_code(response) == "refused"
    assert "access_denied" not in response.headers["location"]
    assert await session_rows(stack.dsn) == []


@pytest.mark.parametrize(
    ("claims", "key"),
    [
        ({"nonce": "not-the-one-sent"}, IDP_KEY),
        ({"iss": "https://evil.example.test/realms/golem"}, IDP_KEY),
        ({"aud": "another-client"}, IDP_KEY),
        ({"aud": [CLIENT_ID, "another-client"], "azp": "another-client"}, IDP_KEY),
        ({}, ROGUE_KEY),
    ],
    ids=["nonce", "issuer", "audience", "azp", "signature"],
)
async def test_an_id_token_that_fails_validation_starts_no_session(
    stack: Stack, browser: httpx.AsyncClient, claims: dict[str, Any], key: Any
) -> None:
    stack.idp.id_token_claims = claims
    stack.idp.id_token_key = key
    location = await start_login(browser)

    response = await browser.get(
        "/callback",
        params={
            "code": stack.idp.authorize(location, "alice"),
            "state": query_of(location)["state"],
        },
    )

    assert signin_code(response) == "failed"
    assert SESSION_COOKIE not in response.headers.get("set-cookie", "")
    assert await session_rows(stack.dsn) == []


async def test_an_expired_id_token_starts_no_session(
    stack: Stack, browser: httpx.AsyncClient
) -> None:
    stack.idp.id_token_claims = {"exp": int(stack.clock()) - CLOCK_SKEW_SECONDS - 1}
    location = await start_login(browser)

    response = await browser.get(
        "/callback",
        params={
            "code": stack.idp.authorize(location, "alice"),
            "state": query_of(location)["state"],
        },
    )

    assert signin_code(response) == "failed"


async def test_a_callback_without_a_code_starts_no_session(
    stack: Stack, browser: httpx.AsyncClient
) -> None:
    location = await start_login(browser)

    response = await browser.get("/callback", params={"state": query_of(location)["state"]})

    assert signin_code(response) == "failed"
    assert stack.idp.token_requests == []


async def test_a_login_replaces_the_session_the_browser_had(
    stack: Stack, browser: httpx.AsyncClient
) -> None:
    await login(stack, browser)
    first = browser.cookies[SESSION_COOKIE]

    await login(stack, browser)

    assert browser.cookies[SESSION_COOKIE] != first
    assert len(await session_rows(stack.dsn)) == 1


# --- Sessions ------------------------------------------------------------------------------------


async def test_the_session_names_the_person_and_carries_the_csrf_token(
    stack: Stack, browser: httpx.AsyncClient
) -> None:
    await login(stack, browser)

    response = await browser.get("/api/session")

    assert response.status_code == 200
    body = response.json()
    assert body["name"] == "alice"
    assert re.fullmatch(r"[A-Za-z0-9_-]{43}", body["csrf"])
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", body["expiresAt"])


async def test_tokens_are_encrypted_at_rest_and_the_session_id_is_not_stored(
    stack: Stack, browser: httpx.AsyncClient
) -> None:
    await login(stack, browser)
    [token_request] = stack.idp.token_requests
    [refresh_token] = stack.idp.refresh_tokens

    [row] = await session_rows(stack.dsn)

    stored = " ".join(str(column) for column in row)
    assert refresh_token not in stored
    # A JWT is dot-separated base64url starting with "eyJ"; Fernet text has no dots, so a bare
    # "eyJ" can turn up in it by chance.
    assert not re.search(r"eyJ[\w-]*\.eyJ", stored), "a JWT is stored in the clear"
    assert browser.cookies[SESSION_COOKIE] not in stored
    assert token_request["code"][0] not in stored


async def test_the_api_needs_a_session_and_never_redirects(
    stack: Stack, browser: httpx.AsyncClient
) -> None:
    for path in (
        "/api/session",
        "/api/agents",
        "/api/agents/discovery/board",
        "/api/agents/discovery/tasks",
        "/api/agents/discovery/tasks/some-task",
    ):
        response = await browser.get(path)
        assert response.status_code == 401, path
        assert response.json()["error"] == "unauthenticated", path


async def test_a_post_without_a_session_is_unauthenticated(browser: httpx.AsyncClient) -> None:
    response = await post(browser, "/api/agents/discovery/tasks", {"goal": "go"}, csrf="x")

    assert response.status_code == 401


async def test_a_forged_session_cookie_is_no_session(
    stack: Stack, browser: httpx.AsyncClient
) -> None:
    browser.cookies.set(SESSION_COOKIE, secrets.token_urlsafe(32), domain="golem-ui.example.test")

    response = await browser.get("/api/session")

    assert response.status_code == 401


async def test_the_access_token_is_refreshed_near_expiry(
    stack: Stack, browser: httpx.AsyncClient
) -> None:
    await login(stack, browser)
    stack.clock.now += stack.idp.expires_in - 30

    response = await browser.get("/api/agents")

    assert response.status_code == 200
    assert [r["grant_type"] for r in stack.idp.token_requests] == [
        ["authorization_code"],
        ["refresh_token"],
    ]
    # The refresh token was rotated; the new one is the one the session keeps.
    stack.clock.now += stack.idp.expires_in - 30
    assert (await browser.get("/api/session")).status_code == 200
    assert len(stack.idp.token_requests) == 3


async def test_a_token_far_from_expiry_is_not_refreshed(
    stack: Stack, browser: httpx.AsyncClient
) -> None:
    await login(stack, browser)
    stack.clock.now += 60

    await browser.get("/api/session")

    assert len(stack.idp.token_requests) == 1


async def test_a_failed_refresh_ends_the_session(stack: Stack, browser: httpx.AsyncClient) -> None:
    await login(stack, browser)
    stack.idp.refuse_refresh = True
    stack.clock.now += stack.idp.expires_in

    response = await browser.get("/api/session")

    assert response.status_code == 401
    assert await session_rows(stack.dsn) == []
    assert "Max-Age=0" in response.headers["set-cookie"]


async def test_a_session_ends_after_its_absolute_lifetime(
    stack: Stack, browser: httpx.AsyncClient
) -> None:
    await login(stack, browser)
    stack.clock.now += 12 * 3600 + 1

    response = await browser.get("/api/session")

    assert response.status_code == 401


async def test_a_token_the_edge_refuses_ends_the_session(audit_dsn: str, ui_db: str) -> None:
    stack = build_stack(audit_dsn, ui_db, FakeOrchestrator())
    refusing = create_ui_app(
        oidc=OidcClient(
            http=httpx.AsyncClient(transport=httpx.MockTransport(stack.idp.handle)),
            keys_http=httpx.Client(transport=httpx.MockTransport(stack.idp.handle)),
            issuer=ISSUER,
            discovery_url=DISCOVERY_URL,
            client_id=CLIENT_ID,
            client_secret=CLIENT_SECRET,
            redirect_url=REDIRECT_URL,
            clock=stack.clock,
        ),
        store=SessionStore(ui_db, SESSION_KEY, clock=stack.clock),
        edge=httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _: httpx.Response(401, json={"error": "no"})),
            base_url="http://edge",
        ),
        public_base_url=PUBLIC_URL,
        clock=stack.clock,
    )
    stack.app = refusing
    async with stack.browser() as browser:
        await login(stack, browser)

        response = await browser.get("/api/agents")

    assert response.status_code == 401
    assert response.json()["error"] == "unauthenticated"
    assert "Max-Age=0" in response.headers["set-cookie"]
    assert await session_rows(ui_db) == []


async def test_an_unreachable_edge_is_a_bad_gateway(audit_dsn: str, ui_db: str) -> None:
    def unreachable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    stack = build_stack(audit_dsn, ui_db, FakeOrchestrator())
    stack.app = create_ui_app(
        oidc=OidcClient(
            http=httpx.AsyncClient(transport=httpx.MockTransport(stack.idp.handle)),
            keys_http=httpx.Client(transport=httpx.MockTransport(stack.idp.handle)),
            issuer=ISSUER,
            discovery_url=DISCOVERY_URL,
            client_id=CLIENT_ID,
            client_secret=CLIENT_SECRET,
            redirect_url=REDIRECT_URL,
            clock=stack.clock,
        ),
        store=SessionStore(ui_db, SESSION_KEY, clock=stack.clock),
        edge=httpx.AsyncClient(transport=httpx.MockTransport(unreachable), base_url="http://edge"),
        public_base_url=PUBLIC_URL,
        clock=stack.clock,
    )
    async with stack.browser() as browser:
        await login(stack, browser)

        response = await browser.get("/api/agents")

    assert response.status_code == 502
    assert response.json()["error"] == "edge_failed"


# --- Logout --------------------------------------------------------------------------------------


async def test_logout_ends_the_session_and_names_the_idps_end_session_url(
    stack: Stack, browser: httpx.AsyncClient
) -> None:
    await login(stack, browser)

    response = await post(browser, "/logout")

    assert response.status_code == 200
    location = response.json()["redirect"]
    assert location.startswith(f"{END_SESSION_URL}?")
    params = query_of(location)
    assert params["client_id"] == CLIENT_ID
    assert params["post_logout_redirect_uri"] == f"{PUBLIC_URL}/"
    assert jwt.decode(params["id_token_hint"], options={"verify_signature": False})["aud"] == (
        CLIENT_ID
    )
    assert "Max-Age=0" in response.headers["set-cookie"]
    assert await session_rows(stack.dsn) == []
    assert (await browser.get("/api/session")).status_code == 401


async def test_logout_without_an_end_session_endpoint_returns_home(
    stack: Stack, browser: httpx.AsyncClient
) -> None:
    stack.idp.end_session = False
    await login(stack, browser)

    response = await post(browser, "/logout")

    assert response.json() == {"redirect": "/"}
    assert await session_rows(stack.dsn) == []


async def test_logout_needs_the_csrf_token(stack: Stack, browser: httpx.AsyncClient) -> None:
    await login(stack, browser)

    response = await post(browser, "/logout", csrf="wrong")

    assert response.status_code == 403
    assert len(await session_rows(stack.dsn)) == 1


# --- CSRF and bodies -----------------------------------------------------------------------------


@pytest.mark.parametrize("csrf", [None, "wrong"])
async def test_a_start_without_the_sessions_csrf_token_is_refused(
    stack: Stack, browser: httpx.AsyncClient, csrf: str | None
) -> None:
    await login(stack, browser)

    response = await start_task(browser, csrf=csrf)

    assert response.status_code == 403
    assert response.json()["error"] == "csrf"
    assert stack.orchestrator.started == []


async def test_an_empty_csrf_header_is_refused(stack: Stack, browser: httpx.AsyncClient) -> None:
    await login(stack, browser)

    response = await browser.post(
        "/api/agents/discovery/tasks",
        json={"goal": "go", "nonce": nonce()},
        headers={CSRF_HEADER: ""},
    )

    assert response.status_code == 403
    assert stack.orchestrator.started == []


async def test_the_csrf_token_of_another_session_is_refused(stack: Stack) -> None:
    async with stack.browser() as alice, stack.browser() as bob:
        await login(stack, alice, "alice")
        await login(stack, bob, "bob")

        response = await start_task(alice, csrf=await csrf_of(bob))

    assert response.status_code == 403
    assert stack.orchestrator.started == []


async def test_a_form_post_is_refused_even_with_the_token(
    stack: Stack, browser: httpx.AsyncClient
) -> None:
    await login(stack, browser)

    response = await browser.post(
        "/api/agents/discovery/tasks",
        data={"goal": "go", "nonce": nonce()},
        headers={CSRF_HEADER: await csrf_of(browser)},
    )

    assert response.status_code == 415
    assert stack.orchestrator.started == []


async def test_a_body_over_16_kib_is_refused(stack: Stack, browser: httpx.AsyncClient) -> None:
    await login(stack, browser)

    response = await start_task(browser, goal="x" * 17_000)

    assert response.status_code == 413
    assert stack.orchestrator.started == []


@pytest.mark.parametrize("body", [b"not json", b"[1, 2]", b'"goal"', b"\xff"])
async def test_a_body_that_is_not_a_json_object_is_malformed(
    stack: Stack, browser: httpx.AsyncClient, body: bytes
) -> None:
    await login(stack, browser)

    response = await browser.post(
        "/api/agents/discovery/tasks",
        content=body,
        headers={CSRF_HEADER: await csrf_of(browser), "Content-Type": "application/json"},
    )

    assert response.status_code == 400
    assert response.json()["error"] == "malformed"


# --- The directory -------------------------------------------------------------------------------


async def test_agents_are_those_the_edge_lets_the_person_call(stack: Stack) -> None:
    async with stack.browser() as alice, stack.browser() as bob:
        await login(stack, alice, "alice")
        await login(stack, bob, "bob")

        alices = (await alice.get("/api/agents")).json()["agents"]
        bobs = (await bob.get("/api/agents")).json()["agents"]

    assert alices == [
        {"name": "discovery", "description": CARDS["discovery"].description, "skills": ["research"]}
    ]
    assert [agent["name"] for agent in bobs] == ["discovery", "reviewer"]


async def test_the_directory_is_kept_for_a_minute_per_session(
    stack: Stack, browser: httpx.AsyncClient
) -> None:
    await login(stack, browser)

    await browser.get("/api/agents")
    await browser.get("/api/agents")
    await board(browser)
    stack.clock.now += 61
    await browser.get("/api/agents")

    assert stack.edge.calls.count("/agents") == 2


async def test_an_agent_outside_the_directory_costs_no_edge_call(
    stack: Stack, browser: httpx.AsyncClient
) -> None:
    await login(stack, browser)

    for path in (
        "/api/agents/reviewer/board",
        "/api/agents/evaluator/tasks",
        "/api/agents/ghost/tasks/t-1",
    ):
        response = await browser.get(path)
        assert response.status_code == 404, path
        assert response.json()["error"] == "not_found"
    started = await start_task(browser, agent="reviewer")

    assert started.status_code == 404
    assert stack.edge.calls == ["/agents"]
    assert stack.orchestrator.started == []


# --- Starting, replying, canceling --------------------------------------------------------------


async def test_starting_a_task_goes_through_the_edge_as_the_user(
    stack: Stack, browser: httpx.AsyncClient
) -> None:
    await login(stack, browser, user="alice")

    response = await start_task(browser, goal="write the H-2 evidence")

    assert response.status_code == 201
    task = response.json()["task"]
    assert (task["state"], task["column"], task["goal"]) == (
        "working",
        "in_progress",
        "write the H-2 evidence",
    )
    [run] = stack.orchestrator.started
    assert (run.caller, run.agent, run.goal) == (
        "user:alice",
        "discovery",
        "write the H-2 evidence",
    )
    assert run.message_id.startswith("ui:")


async def test_a_double_submit_starts_one_run(
    audit_dsn: str, audit_admin_dsn: str, ui_db: str, runs_db: str, engine: AsyncEngine
) -> None:
    launcher = FakeLauncher()
    orchestrator = PostgresOrchestrator(
        dsn=runs_db,
        limits=Limits(max_runs_per_caller=5, max_runs_per_root=5, budget_per_root=Decimal("10")),
        estimated_cost=Decimal("1"),
        launcher=launcher,
        template=TEMPLATE,
        catalogs={"discovery": CATALOG},
        signing_key=SIGNING_KEY,
        grants={"discovery": ()},
    )
    stack = build_stack(audit_dsn, ui_db, orchestrator, engine)
    async with stack.browser() as browser:
        await login(stack, browser)
        body = {"goal": "once", "nonce": nonce()}

        first = await post(browser, "/api/agents/discovery/tasks", body)
        second = await post(browser, "/api/agents/discovery/tasks", body)

    assert first.status_code == second.status_code == 201
    # A retry relaunches the same Job (the launcher treats the name conflict as launched), so a
    # crash between recording and launching heals; what counts is one run.
    assert len({spec.run_id for spec in launcher.launched}) == 1
    async with await psycopg.AsyncConnection.connect(runs_db) as conn:
        cursor = await conn.execute("SELECT caller FROM runs")
        assert await cursor.fetchall() == [("user:alice",)]


@pytest.mark.parametrize(
    "body",
    [
        {"goal": "", "nonce": "n" * 32},
        {"goal": "   ", "nonce": "n" * 32},
        {"goal": "g" * 4001, "nonce": "n" * 32},
        {"goal": 7, "nonce": "n" * 32},
        {"goal": "go", "nonce": "short"},
        {"goal": "go", "nonce": "not a nonce at all, with spaces"},
        {"goal": "go"},
    ],
)
async def test_a_malformed_start_starts_nothing(
    stack: Stack, browser: httpx.AsyncClient, body: dict[str, Any]
) -> None:
    await login(stack, browser)

    response = await post(browser, "/api/agents/discovery/tasks", body)

    assert response.status_code == 400
    assert response.json()["error"] == "malformed"
    assert stack.orchestrator.started == []


async def test_a_reply_goes_into_the_same_task(stack: Stack, browser: httpx.AsyncClient) -> None:
    await login(stack, browser)
    task_id = await started_id(browser)

    response = await post(
        browser,
        f"/api/agents/discovery/tasks/{task_id}/messages",
        {"text": "use the second option", "nonce": nonce()},
    )

    assert response.status_code == 200, response.text
    assert response.json()["task"]["id"] == task_id
    assert stack.edge.calls[-2:] == ["GetTask", "SendMessage"]
    # One task is one run: the reply starts nothing new. What a run does with it belongs to
    # the ADR that lets a run ask (ADR 0018).
    assert len(stack.orchestrator.started) == 1


async def test_a_reply_to_a_final_task_is_a_conflict(
    stack: Stack, browser: httpx.AsyncClient
) -> None:
    await login(stack, browser)
    task_id = await started_id(browser)
    await post(browser, f"/api/agents/discovery/tasks/{task_id}/cancel")

    response = await post(
        browser,
        f"/api/agents/discovery/tasks/{task_id}/messages",
        {"text": "too late", "nonce": nonce()},
    )

    assert response.status_code == 409
    assert response.json()["error"] == "conflict"


@pytest.mark.parametrize("body", [{"text": "", "nonce": "n" * 32}, {"text": "hi", "nonce": "x"}])
async def test_a_malformed_reply_sends_nothing(
    stack: Stack, browser: httpx.AsyncClient, body: dict[str, Any]
) -> None:
    await login(stack, browser)
    task_id = await started_id(browser)
    calls = len(stack.edge.calls)

    response = await post(browser, f"/api/agents/discovery/tasks/{task_id}/messages", body)

    assert response.status_code == 400
    assert len(stack.edge.calls) == calls


async def test_cancel_cancels_the_task_through_the_edge(
    stack: Stack, browser: httpx.AsyncClient
) -> None:
    await login(stack, browser)
    task_id = await started_id(browser)

    response = await post(browser, f"/api/agents/discovery/tasks/{task_id}/cancel")

    assert response.status_code == 200
    assert (response.json()["task"]["state"], response.json()["task"]["column"]) == (
        "canceled",
        "archive",
    )
    assert stack.orchestrator.canceled == [task_id]
    again = await post(browser, f"/api/agents/discovery/tasks/{task_id}/cancel")
    assert again.status_code == 409


async def test_cancel_without_the_csrf_token_cancels_nothing(
    stack: Stack, browser: httpx.AsyncClient
) -> None:
    await login(stack, browser)
    task_id = await started_id(browser)

    response = await post(browser, f"/api/agents/discovery/tasks/{task_id}/cancel", csrf=None)

    assert response.status_code == 403
    assert stack.orchestrator.canceled == []


async def test_another_users_task_is_not_found(stack: Stack) -> None:
    async with stack.browser() as alice, stack.browser() as bob:
        await login(stack, alice, "alice")
        await login(stack, bob, "bob")
        task_id = await started_id(alice)
        path = f"/api/agents/discovery/tasks/{task_id}"

        read = await bob.get(path)
        canceled = await post(bob, f"{path}/cancel")
        replied = await post(bob, f"{path}/messages", {"text": "mine now", "nonce": nonce()})

    assert (read.status_code, canceled.status_code, replied.status_code) == (404, 404, 404)
    assert stack.orchestrator.canceled == []


async def test_a_task_shows_its_goal_state_and_conversation(
    stack: Stack, browser: httpx.AsyncClient
) -> None:
    await login(stack, browser)
    task_id = await started_id(browser, goal="look closer")

    response = await browser.get(f"/api/agents/discovery/tasks/{task_id}")

    assert response.status_code == 200
    task = response.json()
    assert (task["id"], task["state"], task["goal"]) == (task_id, "working", "look closer")
    assert task["history"][0] == {"role": "user", "text": "look closer"}
    assert task["artifacts"] == []


async def test_a_goal_with_markup_comes_back_as_json_text(
    stack: Stack, browser: httpx.AsyncClient
) -> None:
    await login(stack, browser)
    goal = "<script>alert(1)</script>"
    task_id = await started_id(browser, goal=goal)

    response = await browser.get(f"/api/agents/discovery/tasks/{task_id}")

    assert response.headers["content-type"] == "application/json"
    assert response.json()["goal"] == goal


# --- The board -----------------------------------------------------------------------------------


async def complete(stack: Stack, task_id: str, proposal: ProposalView | None) -> None:
    """The run ends; the reconciler delivers its outcome to the task service."""
    stack.orchestrator.finish(
        task_id, succeeded=True, detail=f"Run succeeded; merge request: {MR_URL}", proposal=proposal
    )
    response = await stack.internal.post(OUTCOME_PATH, json={"task_id": task_id})
    assert response.status_code == 200, response.text


async def test_a_snapshot_shows_my_tasks_of_one_agent(stack: Stack) -> None:
    async with stack.browser() as alice, stack.browser() as bob:
        await login(stack, alice, "alice")
        await login(stack, bob, "bob")
        mine = await started_id(alice, "alice's goal")
        await started_id(bob, "bob's goal")
        response = await post(bob, "/api/agents/reviewer/tasks", {"goal": "r", "nonce": nonce()})
        assert response.status_code == 201

        alices = await board(alice)
        bobs = await board(bob, "reviewer")

    assert alices["agent"] == "discovery"
    assert alices["complete"] is True
    assert [(t["id"], t["goal"], t["column"]) for t in alices["tasks"]] == [
        (mine, "alice's goal", "in_progress")
    ]
    assert [t["goal"] for t in bobs["tasks"]] == ["r"]
    assert "proposals" not in alices


async def test_a_completed_task_with_an_open_proposal_waits_for_review(
    stack: Stack, browser: httpx.AsyncClient
) -> None:
    await login(stack, browser)
    task_id = await started_id(browser)
    pending = ProposalView(id="p-1", kind="merge_request", state="pending", url=MR_URL)
    await complete(stack, task_id, pending)

    [task] = (await board(browser))["tasks"]

    assert (task["state"], task["column"]) == ("completed", "review")
    assert task["proposal"] == {
        "id": "p-1",
        "kind": "merge_request",
        "state": "pending",
        "url": MR_URL,
    }


async def test_a_decided_proposal_moves_its_task_to_the_archive(
    stack: Stack, browser: httpx.AsyncClient
) -> None:
    await login(stack, browser)
    task_id = await started_id(browser)
    pending = ProposalView(id="p-1", kind="merge_request", state="pending", url=MR_URL)
    await complete(stack, task_id, pending)
    applied = ProposalView(id="p-1", kind="merge_request", state="applied", url=MR_URL)
    stack.orchestrator.proposals["p-1"] = ProposalRecord(
        view=applied, caller="user:alice", agent="discovery", task_ids=(task_id,)
    )
    assert (
        await stack.internal.post(PROPOSAL_STATE_PATH, json={"proposal_id": "p-1"})
    ).status_code == 200

    [task] = (await board(browser))["tasks"]

    assert (task["column"], task["proposal"]["state"]) == ("archive", "applied")


async def test_a_completed_task_without_a_proposal_is_archived(
    stack: Stack, browser: httpx.AsyncClient
) -> None:
    await login(stack, browser)
    task_id = await started_id(browser)
    await complete(stack, task_id, None)

    [task] = (await board(browser))["tasks"]

    assert (task["column"], task["proposal"]) == ("archive", None)


async def test_a_delta_brings_what_changed_since_the_cursor(
    stack: Stack, browser: httpx.AsyncClient
) -> None:
    await login(stack, browser)
    first = await started_id(browser, "first")
    snapshot = await board(browser)

    second = await started_id(browser, "second")
    delta = await board(browser, since=snapshot["cursor"])

    assert delta["complete"] is False
    ids = [task["id"] for task in delta["tasks"]]
    assert second in ids
    # Within the 30 s overlap the first task comes again; the client replaces it by id.
    assert first in ids
    assert delta["cursor"] >= snapshot["cursor"]


async def test_a_delta_that_fills_its_page_is_answered_as_a_snapshot(
    audit_dsn: str, audit_admin_dsn: str, ui_db: str, engine: AsyncEngine
) -> None:
    stack = build_stack(audit_dsn, ui_db, FakeOrchestrator(), engine, board_size=2)
    async with stack.browser() as browser:
        await login(stack, browser)
        await started_id(browser, "one")
        cursor = (await board(browser))["cursor"]
        for goal in ("two", "three"):
            await started_id(browser, goal)

        answer = await board(browser, since=cursor)

    assert answer["complete"] is True
    assert [task["goal"] for task in answer["tasks"]] == ["three", "two"]


async def test_an_empty_board_has_a_cursor_from_now(
    stack: Stack, browser: httpx.AsyncClient
) -> None:
    await login(stack, browser)

    answer = await board(browser)

    assert answer["tasks"] == [] and answer["complete"] is True
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{6}Z", answer["cursor"])


@pytest.mark.parametrize("since", ["yesterday", "2026-09-28T10:00:00", "<script>"])
async def test_a_malformed_cursor_is_refused(
    stack: Stack, browser: httpx.AsyncClient, since: str
) -> None:
    await login(stack, browser)

    response = await browser.get("/api/agents/discovery/board", params={"since": since})

    assert response.status_code == 400


async def test_the_archive_follows_the_next_page_token(
    audit_dsn: str, audit_admin_dsn: str, ui_db: str, engine: AsyncEngine
) -> None:
    stack = build_stack(audit_dsn, ui_db, FakeOrchestrator(), engine, page_size=2)
    async with stack.browser() as browser:
        await login(stack, browser)
        for goal in ("goal one", "goal two", "goal three"):
            await started_id(browser, goal)

        first = (await browser.get("/api/agents/discovery/tasks")).json()
        second = (
            await browser.get("/api/agents/discovery/tasks", params={"page": first["next"]})
        ).json()

    assert [t["goal"] for t in first["tasks"]] == ["goal three", "goal two"]
    assert [t["goal"] for t in second["tasks"]] == ["goal one"]
    assert second["next"] is None


@pytest.mark.parametrize("token", ["not a token", "x" * 300, "<script>"])
async def test_a_malformed_page_token_is_refused(
    stack: Stack, browser: httpx.AsyncClient, token: str
) -> None:
    await login(stack, browser)

    response = await browser.get("/api/agents/discovery/tasks", params={"page": token})

    assert response.status_code == 400


# --- Headers -------------------------------------------------------------------------------------


async def test_every_response_carries_the_security_headers(
    stack: Stack, browser: httpx.AsyncClient
) -> None:
    responses = [
        await browser.get("/"),
        await browser.get("/login"),
        await browser.get("/healthz"),
        await browser.get("/api/session"),
        await browser.get("/nowhere"),
        await browser.get("/callback", params={"state": "x", "code": "y"}),
    ]
    await login(stack, browser)
    responses += [
        await browser.get("/api/agents"),
        await browser.post("/api/agents/discovery/tasks", data={}),
        await start_task(browser),
    ]

    for response in responses:
        headers = response.headers
        assert headers["content-security-policy"].startswith(CSP), response.url
        assert "object-src 'none'" in headers["content-security-policy"]
        assert headers["x-content-type-options"] == "nosniff"
        assert headers["referrer-policy"] == "same-origin"
        assert headers["cross-origin-opener-policy"] == "same-origin"
        assert headers["strict-transport-security"] == "max-age=31536000; includeSubDomains"
        assert headers["cache-control"] == "no-store"
        assert "access-control-allow-origin" not in headers


async def test_the_bff_serves_no_pages(stack: Stack, browser: httpx.AsyncClient) -> None:
    await login(stack, browser)

    for path in ("/", "/agents", "/tasks", "/static/golem.css"):
        response = await browser.get(path)
        assert response.status_code == 404, path
        assert response.headers["content-type"] == "application/json", path


async def test_the_health_check_needs_no_session(browser: httpx.AsyncClient) -> None:
    response = await browser.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


# --- Settings ------------------------------------------------------------------------------------

UI_ENV = {
    "GOLEM_OIDC_ISSUER": ISSUER,
    "GOLEM_OIDC_DISCOVERY_URL": DISCOVERY_URL,
    "GOLEM_OIDC_CLIENT_ID": CLIENT_ID,
    "GOLEM_OIDC_CLIENT_SECRET": CLIENT_SECRET,
    "GOLEM_OIDC_REDIRECT_URL": REDIRECT_URL,
    "GOLEM_EDGE_URL": "http://edge.golem-system.svc:8000",
    "GOLEM_UI_DSN": "host=db dbname=golem_ui user=golem_ui",
    "GOLEM_UI_SESSION_KEY": SESSION_KEY,
    "GOLEM_PUBLIC_BASE_URL": PUBLIC_URL,
}


def test_ui_settings_are_parsed() -> None:
    settings = ui_settings(UI_ENV | {"GOLEM_PORT": "8080"})

    assert settings.redirect_url == REDIRECT_URL
    assert settings.port == 8080
    assert CLIENT_SECRET not in repr(settings)
    assert SESSION_KEY not in repr(settings)


def test_the_ui_no_longer_needs_a_list_of_agents() -> None:
    settings = ui_settings(UI_ENV | {"GOLEM_UI_AGENTS": "discovery"})

    assert not hasattr(settings, "agents")


def test_every_missing_ui_variable_is_reported_at_once() -> None:
    with pytest.raises(SettingsError) as error:
        ui_settings({})

    for name in UI_ENV:
        assert name in str(error.value)


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("GOLEM_UI_SESSION_KEY", "not-a-fernet-key"),
        ("GOLEM_OIDC_REDIRECT_URL", "https://elsewhere.example.test/callback"),
        ("GOLEM_OIDC_REDIRECT_URL", f"{PUBLIC_URL}/other"),
        ("GOLEM_PUBLIC_BASE_URL", "http://golem-ui.example.test"),
    ],
)
def test_bad_ui_settings_are_refused(name: str, value: str) -> None:
    with pytest.raises(SettingsError, match=name):
        ui_settings(UI_ENV | {name: value})


def test_plain_http_is_allowed_for_localhost_only() -> None:
    settings = ui_settings(
        UI_ENV
        | {
            "GOLEM_PUBLIC_BASE_URL": "http://localhost:8090",
            "GOLEM_OIDC_REDIRECT_URL": "http://localhost:8090/callback",
        }
    )

    assert settings.public_base_url == "http://localhost:8090"


async def test_the_ui_role_owns_its_database_and_nobody_else_reaches_it(
    postgres: PostgresContainer, ui_db: str
) -> None:
    with pytest.raises(psycopg.OperationalError):
        await psycopg.AsyncConnection.connect(ui_dsn_of(postgres, "golem_edge"))


# --- Rate limits (ADR 0012) ----------------------------------------------------------------------


async def login_rows(dsn: str) -> int:
    async with await psycopg.AsyncConnection.connect(dsn) as conn:
        cursor = await conn.execute("SELECT count(*) FROM logins")
        row = await cursor.fetchone()
        assert row is not None
        return int(row[0])


def behind(stack: Stack, peer: str) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=stack.app, client=(peer, 40000)), base_url=PUBLIC_URL
    )


async def test_login_is_limited_per_client_address(
    audit_dsn: str, audit_admin_dsn: str, ui_db: str
) -> None:
    logins = Limiter(Rate(per_minute=60, burst=2), clock=Clock())
    stack = build_stack(audit_dsn, ui_db, FakeOrchestrator(), logins=logins)

    async with behind(stack, "203.0.113.5") as flood, behind(stack, "203.0.113.6") as other:
        started = [await flood.get("/login") for _ in range(2)]
        refused = await flood.get("/login")
        spoofed = await flood.get("/login", headers={"X-Forwarded-For": "198.51.100.1"})
        elsewhere = await other.get("/login")

    assert [r.status_code for r in started] == [303, 303]
    assert refused.status_code == 429 and spoofed.status_code == 429
    assert int(refused.headers["retry-after"]) >= 1
    assert refused.json()["error"] == "rate_limited"
    assert refused.headers["content-security-policy"].startswith("default-src 'self'")
    assert elsewhere.status_code == 303
    assert await login_rows(ui_db) == 3


async def test_behind_a_trusted_proxy_login_is_limited_per_forwarded_client(
    audit_dsn: str, audit_admin_dsn: str, ui_db: str
) -> None:
    logins = Limiter(Rate(per_minute=60, burst=1), clock=Clock())
    stack = build_stack(
        audit_dsn,
        ui_db,
        FakeOrchestrator(),
        logins=logins,
        trusted_proxies=parse_networks("10.0.0.0/8"),
    )

    async with behind(stack, "10.0.0.9") as ingress:
        first = await ingress.get("/login", headers={"X-Forwarded-For": "203.0.113.5"})
        again = await ingress.get("/login", headers={"X-Forwarded-For": "203.0.113.5"})
        other = await ingress.get("/login", headers={"X-Forwarded-For": "203.0.113.6"})

    assert (first.status_code, again.status_code, other.status_code) == (303, 429, 303)


async def test_starting_and_replying_share_a_limit_per_session(
    audit_dsn: str, audit_admin_dsn: str, ui_db: str, engine: AsyncEngine
) -> None:
    starts = Limiter(Rate(per_minute=60, burst=2), clock=Clock())
    stack = build_stack(audit_dsn, ui_db, FakeOrchestrator(), engine, starts=starts)

    async with stack.browser() as alice, stack.browser() as bob:
        await login(stack, alice, "alice")
        await login(stack, bob, "bob")
        task_id = await started_id(alice, "one")
        reply = await post(
            alice,
            f"/api/agents/discovery/tasks/{task_id}/messages",
            {"text": "more", "nonce": nonce()},
        )
        refused = await start_task(alice, goal="two")
        bobs = await start_task(bob, goal="three")

    assert reply.status_code == 200
    assert refused.status_code == 429 and int(refused.headers["retry-after"]) >= 1
    assert refused.json()["error"] == "rate_limited"
    assert bobs.status_code == 201
    assert sorted(run.goal for run in stack.orchestrator.started) == ["one", "three"]


async def test_a_limit_at_the_edge_is_passed_on_with_its_retry_after(
    audit_dsn: str, audit_admin_dsn: str, ui_db: str, engine: AsyncEngine
) -> None:
    callers = Limiter(Rate(per_minute=60, burst=1), clock=Clock())
    stack = build_stack(audit_dsn, ui_db, FakeOrchestrator(), engine, edge_callers=callers)

    async with stack.browser() as browser:
        await login(stack, browser)
        assert (await browser.get("/api/agents")).status_code == 200

        response = await browser.get("/api/agents/discovery/board")

    assert response.status_code == 429
    assert response.json()["error"] == "rate_limited"
    assert int(response.headers["retry-after"]) >= 1


# --- Metrics (ADR 0013) --------------------------------------------------------------------------


async def test_ui_requests_are_counted_by_route_template_and_refusals_by_kind(
    audit_dsn: str, audit_admin_dsn: str, ui_db: str, engine: AsyncEngine
) -> None:
    metrics = Metrics("ui")
    logins = Limiter(Rate(per_minute=60, burst=2), clock=Clock())
    starts = Limiter(Rate(per_minute=60, burst=1), clock=Clock())
    stack = build_stack(
        audit_dsn,
        ui_db,
        FakeOrchestrator(),
        engine,
        logins=logins,
        starts=starts,
        metrics=metrics,
    )

    async with stack.browser() as browser:
        await login(stack, browser)
        await start_task(browser, goal="one")
        await start_task(browser, goal="two")
        for task_id in ("t-1", "t-2", "t-3"):
            await browser.get(f"/api/agents/discovery/tasks/{task_id}")
        await browser.get("/admin")
        await browser.get("/login")
        await browser.get("/login")
        await browser.get("/callback", params={"code": "x", "state": "made-up"})

    routes = {
        s.labels["route"]
        for family in metrics.registry.collect()
        for s in family.samples
        if s.name == "golem_http_requests_total"
    }
    assert routes == {
        "/login",
        "/callback",
        "/api/session",
        "/api/agents/{agent}/tasks",
        "/api/agents/{agent}/tasks/{task_id}",
        "unmatched",
    }
    value = metrics.registry.get_sample_value
    assert value("golem_rate_limit_refusals_total", {"process": "ui", "limit": "start"}) == 1
    assert value("golem_rate_limit_refusals_total", {"process": "ui", "limit": "login"}) == 1
    assert value("golem_authentication_failures_total", {"process": "ui"}) == 1


# --- The session database ------------------------------------------------------------------------


async def test_replicas_starting_together_on_an_empty_database_all_start(
    postgres: PostgresContainer,
) -> None:
    # Concurrent CREATE TABLE IF NOT EXISTS can still collide in the catalog
    # (pg_type_typname_nsp_index); the UI runs two replicas.
    dsn = ui_dsn_of(postgres)
    for _ in range(5):
        async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as conn:
            await conn.execute("DROP TABLE IF EXISTS sessions, logins")

        await asyncio.gather(*(prepare(dsn) for _ in range(8)))


async def test_concurrent_requests_share_the_roles_few_connections_instead_of_failing(
    ui_db: str,
) -> None:
    # golem_ui has CONNECTION LIMIT 10 (deploy/postgres/init.sql) across both replicas; here
    # the other replica holds all but one.
    store = SessionStore(ui_db, SESSION_KEY, connections=1)
    tokens = Tokens(access_token="a", refresh_token="r", id_token=None, expires_in=300)
    cookie, _ = await store.create(subject="alice", name="Alice", tokens=tokens, replacing=None)
    others = [await psycopg.AsyncConnection.connect(ui_db) for _ in range(9)]
    try:
        sessions = await asyncio.gather(*(store.get(cookie) for _ in range(20)))
    finally:
        for conn in others:
            await conn.close()

    assert all(session is not None for session in sessions)
