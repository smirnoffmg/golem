"""Whether a run is still running, asked of the task service and cached briefly.

A Golem token (a run token at the MCP servers, a call token at the edge) is revoked when its
run stops running (ASVS 10.4.9): the verifier asks ``GET /internal/runs/{run_id}`` on the task
service's internal read port before serving it.
"""

import time
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import quote

import httpx

RUNNING = "running"
UNKNOWN = "unknown"


@dataclass(frozen=True)
class StatusUnavailable:
    reason: str


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
