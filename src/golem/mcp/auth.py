"""Who may call a platform MCP server: a verified run token, its grant, and a running run.

A run token is the only credential accepted (audience ``golem-mcp``, ASVS 10.3.1); the decision
rests on its claims (ASVS 10.3.2): the token must grant this server's tool group, and the run it
names must still be running, which the task service answers and this server caches briefly.
Anything that cannot be checked is refused.
"""

import time
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import quote

import httpx

from golem.jwks import SigningKeys, key_id_of
from golem.mcp.groups import Group
from golem.run_token import RunClaims, RunTokenError, verify

RUNNING = "running"
UNKNOWN = "unknown"


@dataclass(frozen=True)
class Refusal:
    status_code: int
    # RFC 6750 error code for 401 and 403; empty when the request carried no credentials.
    error: str
    reason: str


@dataclass(frozen=True)
class StatusUnavailable:
    reason: str


def run_token_verifier(
    keys: SigningKeys, clock: Callable[[], float] = time.time
) -> Callable[[str], RunClaims | RunTokenError]:
    def check(token: str) -> RunClaims | RunTokenError:
        current = keys.for_key_id(key_id_of(token))
        if current is None:
            return RunTokenError("signing keys unavailable")
        return verify(token, current, int(clock()))

    return check


def grant_refusal(claims: RunClaims, group: Group) -> Refusal | None:
    if group.name in claims.tools:
        return None
    return Refusal(
        403, "insufficient_scope", f"the run token does not grant tool group {group.name!r}"
    )


def status_refusal(status: str | StatusUnavailable) -> Refusal | None:
    if isinstance(status, StatusUnavailable):
        return Refusal(503, "temporarily_unavailable", f"run status unavailable: {status.reason}")
    if status != RUNNING:
        # RFC 6750 names a revoked token invalid_token; a finished run's token is revoked.
        return Refusal(401, "invalid_token", f"the run is {status}")
    return None


class RunStatuses:
    """Run statuses from the task service, each answer kept for ``ttl_seconds``.

    The TTL bounds how long a canceled run's token keeps working. Failed lookups are not kept,
    so the first call after the task service recovers is served.
    """

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        ttl_seconds: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._client = client
        self._ttl_seconds = ttl_seconds
        self._clock = clock
        self._answers: dict[str, tuple[str, float]] = {}

    async def status_of(self, run_id: str) -> str | StatusUnavailable:
        now = self._clock()
        cached = self._answers.get(run_id)
        if cached is not None and now - cached[1] < self._ttl_seconds:
            return cached[0]
        status = await fetch_status(self._client, run_id)
        if isinstance(status, str):
            self._answers = {
                run: answer
                for run, answer in self._answers.items()
                if now - answer[1] < self._ttl_seconds
            }
            self._answers[run_id] = (status, now)
        return status


async def fetch_status(client: httpx.AsyncClient, run_id: str) -> str | StatusUnavailable:
    try:
        response = await client.get(f"/internal/runs/{quote(run_id, safe='')}")
    except httpx.HTTPError as error:
        return StatusUnavailable(f"the task service is unreachable: {type(error).__name__}")
    if response.status_code == 404:
        return UNKNOWN
    if response.status_code != 200:
        return StatusUnavailable(f"the task service answered {response.status_code}")
    try:
        status = response.json().get("status")
    except (ValueError, AttributeError):
        status = None
    if not isinstance(status, str) or not status:
        return StatusUnavailable("the task service answered without a status")
    return status
