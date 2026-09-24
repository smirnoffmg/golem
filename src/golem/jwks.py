"""Signing keys fetched from a JWKS URL: the edge's identity provider, the MCP servers' run keys."""

import logging
import time
from collections.abc import Callable

import httpx
import jwt

MIN_REFRESH_SECONDS = 60.0

log = logging.getLogger("golem.jwks")


def fetch_jwks(client: httpx.Client, url: str) -> jwt.PyJWKSet:
    response = client.get(url)
    response.raise_for_status()
    return jwt.PyJWKSet.from_dict(response.json())


class SigningKeys:
    """Signing keys from a JWKS URL, refetched when a token names an unknown key id.

    Refetching is rate limited so that tokens with made-up key ids cannot turn a verifier into
    a request flood against the key publisher; a failed fetch keeps the keys already known, and
    a verifier that never got any has none, so it refuses every token. Fetching is synchronous:
    callers verify off the event loop, so a refetch blocks one worker thread for at most the
    client's timeout, at most once per interval.
    """

    def __init__(
        self,
        fetch: Callable[[], jwt.PyJWKSet],
        *,
        clock: Callable[[], float] = time.monotonic,
        min_refresh_seconds: float = MIN_REFRESH_SECONDS,
    ) -> None:
        self._fetch = fetch
        self._clock = clock
        self._min_refresh_seconds = min_refresh_seconds
        self._keys: jwt.PyJWKSet | None = None
        self._fetched_at: float | None = None

    def refresh(self) -> None:
        self._fetched_at = self._clock()
        try:
            self._keys = self._fetch()
        except (httpx.HTTPError, jwt.PyJWKSetError, ValueError) as error:
            log.warning("could not fetch signing keys: %s", error)

    def for_key_id(self, kid: str | None) -> jwt.PyJWKSet | None:
        if kid is not None and not self._knows(kid) and self._may_refresh():
            self.refresh()
        return self._keys

    def _knows(self, kid: str) -> bool:
        try:
            return self._keys is not None and self._keys[kid] is not None
        except KeyError:
            return False

    def _may_refresh(self) -> bool:
        return (
            self._fetched_at is None
            or self._clock() - self._fetched_at >= self._min_refresh_seconds
        )


def key_id_of(token: str) -> str | None:
    try:
        kid = jwt.get_unverified_header(token).get("kid")
    except jwt.DecodeError:
        return None
    return kid if isinstance(kid, str) else None
