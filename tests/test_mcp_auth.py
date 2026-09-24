"""Run token checks of the platform MCP servers, against the real task service routes."""

from collections.abc import Iterator
from dataclasses import dataclass, field
from functools import partial

import httpx
import pytest
from starlette.testclient import TestClient
from test_tasks_service import FakeOrchestrator, read_listener

from golem.jwks import SigningKeys, fetch_jwks
from golem.mcp.auth import (
    Refusal,
    RunStatuses,
    StatusUnavailable,
    grant_refusal,
    run_token_verifier,
    status_refusal,
)
from golem.mcp.groups import GROUPS
from golem.run_token import RunClaims, RunTokenError, SigningKey, issue

KEY = SigningKey.generate(kid="run-key-1")
OTHER_KEY = SigningKey.generate(kid="run-key-2")
NOW = 1_800_000_000
CLAIMS = RunClaims(
    run_id="run-1",
    agent="discovery",
    caller="user:alice",
    root_run_id="run-0",
    tools=("tracker.read",),
    expires_at=NOW + 600,
)


@dataclass
class StatusOrchestrator(FakeOrchestrator):
    statuses: dict[str, str] = field(default_factory=dict)
    asked: list[str] = field(default_factory=list)
    broken: bool = False

    async def status(self, run_id: str) -> str | None:
        self.asked.append(run_id)
        if self.broken:
            raise RuntimeError("golem_runs unavailable")
        return self.statuses.get(run_id)


class Clock:
    def __init__(self) -> None:
        self.now = float(NOW)

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def orchestrator() -> StatusOrchestrator:
    return StatusOrchestrator(statuses={"run-1": "running", "run-2": "canceled"})


@pytest.fixture
def task_service(orchestrator: StatusOrchestrator) -> Iterator[TestClient]:
    app = read_listener(orchestrator, (KEY,))
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client


def keys_from(task_service: TestClient, clock: Clock) -> SigningKeys:
    return SigningKeys(partial(fetch_jwks, task_service, "/internal/run-keys"), clock=clock)


# Token verification


def test_a_token_signed_with_a_published_key_is_verified(
    task_service: TestClient, clock: Clock
) -> None:
    keys = keys_from(task_service, clock)
    keys.refresh()

    assert run_token_verifier(keys, clock)(issue(CLAIMS, KEY, NOW)) == CLAIMS


def test_an_expired_token_is_refused(task_service: TestClient, clock: Clock) -> None:
    keys = keys_from(task_service, clock)
    keys.refresh()
    clock.now = CLAIMS.expires_at

    assert run_token_verifier(keys, clock)(issue(CLAIMS, KEY, NOW)) == RunTokenError(
        "token expired"
    )


def test_a_token_signed_with_an_unpublished_key_is_refused(
    task_service: TestClient, clock: Clock
) -> None:
    keys = keys_from(task_service, clock)
    keys.refresh()

    verdict = run_token_verifier(keys, clock)(issue(CLAIMS, OTHER_KEY, NOW))

    assert verdict == RunTokenError("unknown signing key")


def test_without_ever_having_keys_every_token_is_refused(clock: Clock) -> None:
    def down() -> object:
        raise httpx.ConnectError("task service down")

    keys = SigningKeys(down, clock=clock)  # type: ignore[arg-type]
    keys.refresh()

    verdict = run_token_verifier(keys, clock)(issue(CLAIMS, KEY, NOW))

    assert verdict == RunTokenError("signing keys unavailable")


# Claims


def test_a_token_granting_the_group_passes() -> None:
    assert grant_refusal(CLAIMS, GROUPS["tracker.read"]) is None


def test_a_token_without_the_group_is_refused_with_insufficient_scope() -> None:
    refusal = grant_refusal(CLAIMS, GROUPS["wiki.read"])

    assert refusal == Refusal(
        status_code=403,
        error="insufficient_scope",
        reason="the run token does not grant tool group 'wiki.read'",
    )


# Run status


def statuses_for(task_service: TestClient, clock: Clock, ttl: float = 10) -> RunStatuses:
    transport = httpx.ASGITransport(app=task_service.app, raise_app_exceptions=False)
    client = httpx.AsyncClient(transport=transport, base_url="http://tasks")
    return RunStatuses(client, ttl_seconds=ttl, clock=clock)


async def test_status_is_read_from_the_task_service_and_cached_for_the_ttl(
    task_service: TestClient, orchestrator: StatusOrchestrator, clock: Clock
) -> None:
    statuses = statuses_for(task_service, clock, ttl=10)

    first = await statuses.status_of("run-1")
    clock.now += 9
    second = await statuses.status_of("run-1")
    clock.now += 1
    orchestrator.statuses["run-1"] = "canceled"
    third = await statuses.status_of("run-1")

    assert (first, second, third) == ("running", "running", "canceled")
    assert orchestrator.asked == ["run-1", "run-1"]


async def test_an_unknown_run_has_status_unknown(task_service: TestClient, clock: Clock) -> None:
    assert await statuses_for(task_service, clock).status_of("run-404") == "unknown"


async def test_a_failed_lookup_is_unavailable_and_not_cached(
    task_service: TestClient, orchestrator: StatusOrchestrator, clock: Clock
) -> None:
    statuses = statuses_for(task_service, clock)
    orchestrator.broken = True

    failed = await statuses.status_of("run-1")
    orchestrator.broken = False
    recovered = await statuses.status_of("run-1")

    assert failed == StatusUnavailable("the task service answered 500")
    assert recovered == "running"


async def test_an_unreachable_task_service_is_unavailable(clock: Clock) -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(refuse), base_url="http://tasks")
    statuses = RunStatuses(client, ttl_seconds=10, clock=clock)

    assert await statuses.status_of("run-1") == StatusUnavailable(
        "the task service is unreachable: ConnectError"
    )


def test_only_a_running_run_passes() -> None:
    assert status_refusal("running") is None
    assert status_refusal("canceled") == Refusal(401, "invalid_token", "the run is canceled")
    assert status_refusal("unknown") == Refusal(401, "invalid_token", "the run is unknown")


def test_an_unavailable_status_refuses_the_call() -> None:
    refusal = status_refusal(StatusUnavailable("the task service answered 500"))

    assert refusal == Refusal(
        503, "temporarily_unavailable", "run status unavailable: the task service answered 500"
    )
