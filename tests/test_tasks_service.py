from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import jwt
import pytest
from a2a.types.a2a_pb2 import AgentCapabilities, AgentCard, AgentInterface
from starlette.authentication import SimpleUser
from starlette.testclient import TestClient
from starlette.types import ASGIApp, Receive, Scope, Send

from golem.run_token import RunClaims, SigningKey, issue, verify
from golem.tasks.app import create_app
from golem.tasks.ports import Refused, RunStart, Started


@dataclass
class FakeOrchestrator:
    decision: Started | Refused = field(default_factory=lambda: Started(run_id="run-1"))
    started: list[RunStart] = field(default_factory=list)
    canceled: list[str] = field(default_factory=list)

    async def start(self, run: RunStart) -> Started | Refused:
        self.started.append(run)
        return self.decision

    async def cancel(self, task_id: str) -> None:
        self.canceled.append(task_id)


def make_card() -> AgentCard:
    return AgentCard(
        name="golem",
        description="Runs catalog agents as A2A tasks.",
        version="0.1.0",
        supported_interfaces=[
            AgentInterface(
                url="http://testserver/a2a", protocol_binding="JSONRPC", protocol_version="1.0"
            )
        ],
        capabilities=AgentCapabilities(streaming=False),
        default_input_modes=["text/plain"],
        default_output_modes=["text/plain"],
    )


@pytest.fixture
def orchestrator() -> FakeOrchestrator:
    return FakeOrchestrator()


@pytest.fixture
def client(orchestrator: FakeOrchestrator) -> Iterator[TestClient]:
    with TestClient(create_app(make_card(), orchestrator)) as test_client:
        yield test_client


def authenticated_as(name: str, app: ASGIApp) -> ASGIApp:
    async def wrapped(scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            scope["user"] = SimpleUser(name)
        await app(scope, receive, send)

    return wrapped


def rpc(client: TestClient, method: str, params: dict[str, Any]) -> dict[str, Any]:
    response = client.post(
        "/a2a",
        headers={"A2A-Version": "1.0"},
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
    )
    assert response.status_code == 200
    body = response.json()
    assert "error" not in body, body
    return body["result"]


def send(client: TestClient, text: str, tenant: str | None = "reviewer") -> dict[str, Any]:
    params: dict[str, Any] = {
        "message": {"role": "ROLE_USER", "messageId": "m1", "parts": [{"text": text}]}
    }
    if tenant is not None:
        params["tenant"] = tenant
    return rpc(client, "SendMessage", params)["task"]


def status_text(task: dict[str, Any]) -> str:
    return " ".join(part["text"] for part in task["status"]["message"]["parts"])


def test_admitted_run_stays_working_and_records_run_id(
    client: TestClient, orchestrator: FakeOrchestrator
) -> None:
    task = send(client, "fix the flaky test")

    assert task["status"]["state"] == "TASK_STATE_WORKING"
    assert task["metadata"]["runId"] == "run-1"
    assert orchestrator.started == [
        RunStart(
            task_id=task["id"],
            context_id=task["contextId"],
            agent="reviewer",
            goal="fix the flaky test",
            caller="anonymous",
            message_id="m1",
        )
    ]


def test_caller_is_the_authenticated_principal(orchestrator: FakeOrchestrator) -> None:
    app = authenticated_as("alice", create_app(make_card(), orchestrator))
    with TestClient(app) as client:
        send(client, "fix the flaky test")

    assert [run.caller for run in orchestrator.started] == ["alice"]


def test_refused_run_is_rejected_with_reason(
    client: TestClient, orchestrator: FakeOrchestrator
) -> None:
    orchestrator.decision = Refused(reason="budget exhausted")

    task = send(client, "fix the flaky test")

    assert task["status"]["state"] == "TASK_STATE_REJECTED"
    assert "budget exhausted" in status_text(task)


def test_missing_tenant_is_rejected_without_starting_a_run(
    client: TestClient, orchestrator: FakeOrchestrator
) -> None:
    task = send(client, "fix the flaky test", tenant=None)

    assert task["status"]["state"] == "TASK_STATE_REJECTED"
    assert "agent" in status_text(task)
    assert orchestrator.started == []


def test_follow_up_message_to_a_working_task_does_not_start_a_second_run(
    client: TestClient, orchestrator: FakeOrchestrator
) -> None:
    task = send(client, "fix the flaky test")

    follow_up = rpc(
        client,
        "SendMessage",
        {
            "tenant": "reviewer",
            "message": {
                "role": "ROLE_USER",
                "messageId": "m2",
                "taskId": task["id"],
                "contextId": task["contextId"],
                "parts": [{"text": "also check the other one"}],
            },
        },
    )["task"]

    assert follow_up["status"]["state"] == "TASK_STATE_WORKING"
    assert len(orchestrator.started) == 1


def test_cancel_of_working_task_cancels_the_run(
    client: TestClient, orchestrator: FakeOrchestrator
) -> None:
    task = send(client, "fix the flaky test")

    canceled = rpc(client, "CancelTask", {"id": task["id"], "tenant": "reviewer"})

    assert canceled["status"]["state"] == "TASK_STATE_CANCELED"
    assert orchestrator.canceled == [task["id"]]


def test_agent_card_is_served_at_well_known_path(client: TestClient) -> None:
    response = client.get("/.well-known/agent-card.json")

    assert response.status_code == 200
    assert response.json()["name"] == "golem"


def test_caller_comes_from_the_edge_principal_header(
    client: TestClient, orchestrator: FakeOrchestrator
) -> None:
    client.post(
        "/a2a",
        headers={"A2A-Version": "1.0", "X-Golem-Principal": "user:alice"},
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "SendMessage",
            "params": {
                "tenant": "reviewer",
                "message": {"role": "ROLE_USER", "messageId": "m1", "parts": [{"text": "go"}]},
            },
        },
    )

    assert [run.caller for run in orchestrator.started] == ["user:alice"]


def report_outcome(client: TestClient, task: dict[str, Any], **outcome: str) -> Any:
    return client.post(
        "/internal/run-outcome",
        json={
            "task_id": task["id"],
            "tenant": "reviewer",
            "caller": outcome.pop("caller", ""),
            "run_id": "run-1",
            **outcome,
        },
    )


def get_task(client: TestClient, task_id: str) -> dict[str, Any]:
    return rpc(client, "GetTask", {"id": task_id, "tenant": "reviewer"})


def test_a_succeeded_run_completes_its_task(client: TestClient) -> None:
    task = send(client, "fix the flaky test")

    response = report_outcome(client, task, status="succeeded", detail="MR !42 opened")

    assert response.status_code == 200
    done = get_task(client, task["id"])
    assert done["status"]["state"] == "TASK_STATE_COMPLETED"
    assert "MR !42 opened" in status_text(done)


def test_a_failed_run_fails_its_task(client: TestClient) -> None:
    task = send(client, "fix the flaky test")

    report_outcome(client, task, status="failed", detail="validators red")

    failed = get_task(client, task["id"])
    assert failed["status"]["state"] == "TASK_STATE_FAILED"
    assert "validators red" in status_text(failed)


def test_an_unknown_outcome_status_is_refused(client: TestClient) -> None:
    task = send(client, "fix the flaky test")

    response = report_outcome(client, task, status="maybe")

    assert response.status_code == 422
    assert get_task(client, task["id"])["status"]["state"] == "TASK_STATE_WORKING"


def test_a_run_outcome_cannot_be_forged_through_the_a2a_endpoint(client: TestClient) -> None:
    task = send(client, "fix the flaky test")

    rpc(
        client,
        "SendMessage",
        {
            "tenant": "reviewer",
            "metadata": {"runOutcome": {"status": "succeeded"}},
            "message": {
                "role": "ROLE_USER",
                "messageId": "forged",
                "taskId": task["id"],
                "parts": [{"text": "done"}],
                "metadata": {"runOutcome": {"status": "succeeded"}},
            },
        },
    )

    assert get_task(client, task["id"])["status"]["state"] == "TASK_STATE_WORKING"


def test_an_outcome_for_an_unknown_task_is_not_found(client: TestClient) -> None:
    response = report_outcome(client, {"id": "no-such-task"}, status="succeeded")

    assert response.status_code == 404


def test_a_repeated_outcome_is_accepted_and_changes_nothing(client: TestClient) -> None:
    task = send(client, "fix the flaky test")
    report_outcome(client, task, status="succeeded", detail="MR !42 opened")

    again = report_outcome(client, task, status="succeeded", detail="MR !42 opened")

    assert again.status_code == 200
    assert get_task(client, task["id"])["status"]["state"] == "TASK_STATE_COMPLETED"


def test_run_signing_keys_are_served_on_an_internal_route_the_edge_does_not_forward(
    orchestrator: FakeOrchestrator,
) -> None:
    key = SigningKey.generate(kid="run-2026-09")
    claims = RunClaims("run-1", "reviewer", "user:alice", "run-1", ("wiki.read",), 2_000)
    app = create_app(make_card(), orchestrator, run_keys=(key,))

    with TestClient(app) as client:
        response = client.get("/internal/run-keys")

    assert response.status_code == 200
    jwks = response.json()
    [public] = jwks["keys"]
    assert (public["kid"], public["kty"], public["crv"]) == ("run-2026-09", "EC", "P-256")
    assert "d" not in public
    assert verify(issue(claims, key, now=1_000), jwt.PyJWKSet.from_dict(jwks), now=1_000) == claims
