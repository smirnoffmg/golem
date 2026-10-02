"""Whether a proposal still allows what its token was issued for, asked of the task service.

A proposal token is revoked with its proposal (ADR 0015), as a run token is with its run: a
write server asks ``GET /internal/proposals/{id}`` on the task service's internal read port
before serving it. An ``apply`` needs the proposal ``accepted``, a ``preview`` needs it
``pending`` or ``failed``.
"""

import time
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import quote

import httpx

from golem.proposal_token import APPLY, PREVIEW
from golem.run_status import StatusUnavailable

__all__ = ["ALLOWED_STATES", "ProposalState", "ProposalStates", "StatusUnavailable"]

ALLOWED_STATES = {
    APPLY: frozenset({"accepted"}),
    PREVIEW: frozenset({"pending", "failed"}),
}


@dataclass(frozen=True)
class ProposalState:
    state: str
    digest: str | None
    kind: str


class ProposalStates:
    """Proposal states from the task service, an answer that allows a scope kept for
    ``ttl_seconds``.

    Only an answer that allows is served from the cache: the TTL bounds how long a revoked
    proposal's token keeps working, and an answer that would refuse is asked again, so a
    preview just before an accept does not leave the apply refused on a stale ``pending``.
    Failed lookups are not kept.
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
        self._answers: dict[str, tuple[ProposalState, float]] = {}

    async def state_of(
        self, proposal_id: str, scope: str
    ) -> ProposalState | StatusUnavailable | None:
        now = self._clock()
        cached = self._answers.get(proposal_id)
        if (
            cached is not None
            and now - cached[1] < self._ttl_seconds
            and cached[0].state in ALLOWED_STATES.get(scope, frozenset())
        ):
            return cached[0]
        found = await fetch_state(self._client, proposal_id)
        if isinstance(found, ProposalState):
            self._answers = {
                key: answer
                for key, answer in self._answers.items()
                if now - answer[1] < self._ttl_seconds
            }
            self._answers[proposal_id] = (found, now)
        return found


async def fetch_state(
    client: httpx.AsyncClient, proposal_id: str
) -> ProposalState | StatusUnavailable | None:
    try:
        response = await client.get(f"/internal/proposals/{quote(proposal_id, safe='')}")
    except httpx.HTTPError as error:
        return StatusUnavailable(f"the task service is unreachable: {type(error).__name__}")
    if response.status_code == 404:
        return None
    if response.status_code != 200:
        return StatusUnavailable(f"the task service answered {response.status_code}")
    try:
        body = response.json()
    except ValueError:
        body = None
    state = body.get("state") if isinstance(body, dict) else None
    kind = body.get("kind") if isinstance(body, dict) else None
    digest = body.get("digest") if isinstance(body, dict) else None
    if not (isinstance(state, str) and state and isinstance(kind, str) and kind):
        return StatusUnavailable("the task service answered without a state")
    return ProposalState(state, digest if isinstance(digest, str) else None, kind)
