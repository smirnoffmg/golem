"""Whether a proposal still allows the call a proposal token was issued for, asked of the real
task service route (ADR 0015): the write servers' revocation check."""

from collections.abc import Iterator

import httpx
import pytest
from starlette.testclient import TestClient
from test_tasks_service import FakeOrchestrator, read_listener

from golem.proposal_status import ProposalState, ProposalStates, StatusUnavailable
from golem.proposal_token import APPLY, PREVIEW
from golem.run_token import SigningKey
from golem.tasks.ports import ProposalGate

KEY = SigningKey.generate(kid="run-key-1")
PROPOSAL = "0c6f0d4e-6c43-4a8e-9a55-0f6c7b0f4a11"
DIGEST = "a" * 64


class Clock:
    def __init__(self) -> None:
        self.now = 1_800_000_000.0

    def __call__(self) -> float:
        return self.now


class CountingOrchestrator(FakeOrchestrator):
    asked: int = 0
    broken: bool = False

    async def proposal_gate(self, proposal_id: str) -> ProposalGate | None:
        self.asked += 1
        if self.broken:
            raise RuntimeError("golem_runs unavailable")
        return await super().proposal_gate(proposal_id)


@pytest.fixture
def orchestrator() -> CountingOrchestrator:
    found = CountingOrchestrator()
    found.gates[PROPOSAL] = ProposalGate(PROPOSAL, "pending", DIGEST, "wiki_edit")
    return found


@pytest.fixture
def task_service(orchestrator: CountingOrchestrator) -> Iterator[TestClient]:
    with TestClient(read_listener(orchestrator, (KEY,)), raise_server_exceptions=False) as client:
        yield client


def states_for(task_service: TestClient, clock: Clock, ttl: float = 10) -> ProposalStates:
    transport = httpx.ASGITransport(app=task_service.app, raise_app_exceptions=False)
    client = httpx.AsyncClient(transport=transport, base_url="http://tasks")
    return ProposalStates(client, ttl_seconds=ttl, clock=clock)


def state_name(answer: ProposalState | StatusUnavailable | None) -> str | None:
    return answer.state if isinstance(answer, ProposalState) else None


def moved(orchestrator: CountingOrchestrator, state: str) -> None:
    orchestrator.gates[PROPOSAL] = ProposalGate(PROPOSAL, state, DIGEST, "wiki_edit")


async def test_an_answer_that_allows_the_call_is_kept_for_the_ttl(
    task_service: TestClient, orchestrator: CountingOrchestrator
) -> None:
    clock = Clock()
    states = states_for(task_service, clock)

    first = await states.state_of(PROPOSAL, PREVIEW)
    moved(orchestrator, "rejected")
    clock.now += 9
    cached = await states.state_of(PROPOSAL, PREVIEW)
    clock.now += 1
    expired = await states.state_of(PROPOSAL, PREVIEW)

    assert [state_name(answer) for answer in (first, cached, expired)] == [
        "pending",
        "pending",
        "rejected",
    ]
    assert orchestrator.asked == 2


async def test_an_answer_that_would_refuse_is_asked_again_at_once(
    task_service: TestClient, orchestrator: CountingOrchestrator
) -> None:
    # A person previews a page, then accepts it within the TTL: the apply must not be refused
    # on the pending state the preview left in the cache.
    clock = Clock()
    states = states_for(task_service, clock)

    await states.state_of(PROPOSAL, PREVIEW)
    moved(orchestrator, "accepted")
    applied = await states.state_of(PROPOSAL, APPLY)

    assert applied is not None and not isinstance(applied, StatusUnavailable)
    assert applied.state == "accepted"
    assert orchestrator.asked == 2


async def test_an_unknown_proposal_is_none(task_service: TestClient) -> None:
    states = states_for(task_service, Clock())

    assert await states.state_of("5d4f1a36-0c43-4a8e-9a55-0f6c7b0f4a11", APPLY) is None


async def test_a_failed_lookup_is_unavailable_and_not_kept(
    task_service: TestClient, orchestrator: CountingOrchestrator
) -> None:
    states = states_for(task_service, Clock())
    orchestrator.broken = True

    failed = await states.state_of(PROPOSAL, PREVIEW)
    orchestrator.broken = False
    recovered = await states.state_of(PROPOSAL, PREVIEW)

    assert failed == StatusUnavailable("the task service answered 500")
    assert recovered is not None and not isinstance(recovered, StatusUnavailable)


async def test_an_unreachable_task_service_is_unavailable() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(refuse), base_url="http://tasks")
    states = ProposalStates(client, ttl_seconds=10, clock=Clock())

    assert await states.state_of(PROPOSAL, APPLY) == StatusUnavailable(
        "the task service is unreachable: ConnectError"
    )
