"""Signing keys fetched from a JWKS URL: the edge's identity provider, the MCP servers' run keys."""

import logging
import time
from collections.abc import Callable

import httpx
import jwt

MIN_REFRESH_SECONDS = 60.0
# How long a fetched key set is trusted before it is fetched again, as PyJWKClient's lifespan:
# a key the publisher deleted (a leaked one, say) keeps verifying tokens that name it until then.
MAX_AGE_SECONDS = 300.0
# The first retry of a verifier that has never had keys; each failure doubles it, up to the
# refresh interval.
FIRST_RETRY_SECONDS = 1.0

log = logging.getLogger("golem.jwks")


def fetch_jwks(client: httpx.Client, url: str) -> jwt.PyJWKSet:
    response = client.get(url)
    response.raise_for_status()
    return jwt.PyJWKSet.from_dict(response.json())


class SigningKeys:
    """Signing keys from a JWKS URL, refetched when a token names an unknown key id or they age.

    Refetching is rate limited so that tokens with made-up key ids cannot turn a verifier into
    a request flood against the key publisher; a failed fetch keeps the keys already known, and
    a verifier that never got any has none, so it refuses every token. Until it has keys, it
    retries sooner, doubling the wait from ``first_retry_seconds`` up to the interval: a verifier
    that starts before the key publisher would otherwise refuse every token for a whole interval.
    Keys older than ``max_age_seconds`` are fetched again on the next lookup, so a key deleted at
    the publisher stops verifying within that time.
    Fetching is synchronous: callers verify off the event loop, so a refetch blocks one worker
    thread for at most the client's timeout, at most once per wait.
    """

    def __init__(
        self,
        fetch: Callable[[], jwt.PyJWKSet],
        *,
        clock: Callable[[], float] = time.monotonic,
        min_refresh_seconds: float = MIN_REFRESH_SECONDS,
        first_retry_seconds: float = FIRST_RETRY_SECONDS,
        max_age_seconds: float = MAX_AGE_SECONDS,
    ) -> None:
        self._fetch = fetch
        self._clock = clock
        self._min_refresh_seconds = min_refresh_seconds
        self._first_retry_seconds = first_retry_seconds
        self._max_age_seconds = max_age_seconds
        self._failures = 0
        self._keys: jwt.PyJWKSet | None = None
        self._fetched_at: float | None = None

    def refresh(self) -> None:
        self._fetched_at = self._clock()
        try:
            self._keys = self._fetch()
        except (httpx.HTTPError, jwt.PyJWKSetError, ValueError) as error:
            log.warning("could not fetch signing keys: %s", error)
            self._failures += 1

    def for_key_id(self, kid: str | None) -> jwt.PyJWKSet | None:
        unknown = kid is not None and not self._knows(kid)
        if (unknown and self._may_refresh()) or self._aged():
            self.refresh()
        return self._keys

    def _knows(self, kid: str) -> bool:
        try:
            return self._keys is not None and self._keys[kid] is not None
        except KeyError:
            return False

    def _aged(self) -> bool:
        return (
            self._keys is not None
            and self._fetched_at is not None
            and self._clock() - self._fetched_at >= self._max_age_seconds
        )

    def _may_refresh(self) -> bool:
        if self._fetched_at is None:
            return True
        wait = self._min_refresh_seconds
        if self._keys is None:
            backoff = self._first_retry_seconds * 2 ** min(self._failures - 1, 32)
            wait = min(backoff, wait)
        return self._clock() - self._fetched_at >= wait


def key_id_of(token: str) -> str | None:
    try:
        kid = jwt.get_unverified_header(token).get("kid")
    except jwt.DecodeError:
        return None
    return kid if isinstance(kid, str) else None
