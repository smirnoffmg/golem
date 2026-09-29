from collections.abc import Iterator
from dataclasses import dataclass, field, replace
from typing import Any

import jwt
import pytest
from a2a.server.tasks import TaskStore
from a2a.types.a2a_pb2 import AgentCapabilities, AgentCard, AgentInterface
from starlette.authentication import SimpleUser
from starlette.testclient import TestClient
from starlette.types import ASGIApp, Receive, Scope, Send

from golem.run_token import RunClaims, SigningKey, issue, verify
from golem.tasks.app import (
    EDGE_TOKEN_HEADER,
    OUTCOME_PATH,
    PROPOSAL_STATE_PATH,
    PushDelivery,
    create_listeners,
)
from golem.tasks.ports import (
    Orchestrator,
    ProposalGate,
    ProposalRecord,
    ProposalView,
    Refused,
    RunOutcome,
    RunStart,
    Started,
    TaskRun,
)

TEST_EDGE_TOKEN = "test-edge-token"


@dataclass
class FakeOrchestrator:
    """The orchestrator's records: every started task's run, and its outcome once final."""

    decision: Started | Refused = field(default_factory=lambda: Started(run_id="run-1"))
    started: list[RunStart] = field(default_factory=list)
    canceled: list[str] = field(default_factory=list)
    runs: dict[str, TaskRun] = field(default_factory=dict)
    proposals: dict[str, ProposalRecord] = field(default_factory=dict)
    gates: dict[str, ProposalGate] = field(default_factory=dict)

    async def start(self, run: RunStart) -> Started | Refused:
        self.started.append(run)
        if isinstance(self.decision, Started):
            self.runs[run.task_id] = TaskRun(self.decision.run_id, run.caller, run.agent, None)
        return self.decision

    def finish(
        self,
        task_id: str,
        *,
        succeeded: bool,
        detail: str,
        proposal: ProposalView | None = None,
        report: str | None = None,
        proposal_payload: dict | None = None,
    ) -> None:
        run = self.runs[task_id]
        outcome = RunOutcome(
            run.run_id, succeeded, detail, proposal, report, proposal_payload=proposal_payload
        )
        self.runs[task_id] = replace(run, outcome=outcome)

    async def run_of_task(self, task_id: str) -> TaskRun | None:
        return self.runs.get(task_id)

    async def cancel(self, task_id: str) -> None:
        self.canceled.append(task_id)

    async def status(self, run_id: str) -> str | None:
        return None

    async def proposal(self, proposal_id: str) -> ProposalRecord | None:
        return self.proposals.get(proposal_id)

    async def accepted_proposal(self, proposal_id: str) -> None:
        # Merge requests only here: none is ever accepted in Golem.
        return None

    async def agents_of_tasks(self, task_ids: tuple[str, ...]) -> dict[str, str]:
        return {t: run.agent for t, run in self.runs.items() if t in task_ids}

    async def proposal_gate(self, proposal_id: str) -> ProposalGate | None:
        return self.gates.get(proposal_id)


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


def create_app(
    card: AgentCard,
    orchestrator: Orchestrator,
    task_store: TaskStore | None = None,
    push: PushDelivery | None = None,
    run_keys: tuple[SigningKey, ...] = (),
) -> ASGIApp:
    """The three listeners behind one test client, each path routed to the port serving it.

    Public requests get the edge token, as if the edge had forwarded them; the listeners on
    their own ports are tested in test_tasks_listeners.py.
    """
    listeners = create_listeners(
        card,
        orchestrator,
        edge_token=TEST_EDGE_TOKEN,
        task_store=task_store,
        push=push,
        run_keys=run_keys,
    )

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await listeners.public(scope, receive, send)
        elif scope["path"] in (OUTCOME_PATH, PROPOSAL_STATE_PATH):
            await listeners.internal_write(scope, receive, send)
        elif scope["path"].startswith("/internal/"):
            await listeners.internal_read(scope, receive, send)
        else:
            token = (EDGE_TOKEN_HEADER.encode(), TEST_EDGE_TOKEN.encode())
            await listeners.public({**scope, "headers": [*scope["headers"], token]}, receive, send)

    return app


def read_listener(orchestrator: Orchestrator, run_keys: tuple[SigningKey, ...]) -> ASGIApp:
    """The port the MCP servers are admitted to: run keys and run status."""
    return create_listeners(
        make_card(), orchestrator, edge_token=TEST_EDGE_TOKEN, run_keys=run_keys
    ).internal_read


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


def send_with_target(client: TestClient, target: Any) -> None:
    rpc(
        client,
        "SendMessage",
        {
            "tenant": "reviewer",
            "message": {
                "role": "ROLE_USER",
                "messageId": "m1",
                "parts": [{"text": "disk usage alert"}],
                "metadata": {"golemTarget": target},
            },
        },
    )


def test_the_starter_names_a_goal_runs_target_in_the_message(
    client: TestClient, orchestrator: FakeOrchestrator
) -> None:
    send_with_target(client, "alert-0a1b2c3d4e5f")

    assert orchestrator.started[0].target == "alert-0a1b2c3d4e5f"


@pytest.mark.parametrize("target", ["../etc", "Alert-1", 42, "a" * 65])
def test_a_malformed_target_is_dropped(
    client: TestClient, orchestrator: FakeOrchestrator, target: Any
) -> None:
    send_with_target(client, target)

    assert orchestrator.started[0].target == ""


def test_a_new_task_records_the_agent_the_edge_forwarded(client: TestClient) -> None:
    task = send(client, "fix the flaky test")

    assert task["metadata"]["golemAgent"] == "reviewer"


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


def send_delegated(client: TestClient, chain: str, root: str) -> dict[str, Any]:
    """A message as the edge forwards a delegated call: the subject, the chain and the root."""
    response = client.post(
        "/a2a",
        headers={
            "A2A-Version": "1.0",
            "X-Golem-Principal": "user:alice",
            "X-Golem-Chain": chain,
            "X-Golem-Root-Run": root,
        },
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
    return response.json()["result"]["task"]


def test_a_delegated_run_starts_with_the_chain_and_root_the_edge_forwarded(
    client: TestClient, orchestrator: FakeOrchestrator
) -> None:
    task = send_delegated(client, "discovery,planner", "root-run")

    [run] = orchestrator.started
    assert (run.caller, run.chain, run.root_run_id) == (
        "user:alice",
        ("discovery", "planner"),
        "root-run",
    )
    assert task["metadata"] == {
        "golemAgent": "reviewer",
        "runId": "run-1",
        "chain": ["discovery", "planner"],
    }


def test_a_run_started_by_a_person_has_no_chain(
    client: TestClient, orchestrator: FakeOrchestrator
) -> None:
    task = send(client, "fix the flaky test")

    [run] = orchestrator.started
    assert (run.chain, run.root_run_id) == ((), "")
    assert "chain" not in task["metadata"]


def notify(client: TestClient, task_id: str, **forged: str) -> Any:
    """The reconciler's notification, plus whatever a forger adds to it."""
    return client.post("/internal/run-outcome", json={"task_id": task_id, **forged})


def get_task(client: TestClient, task_id: str, principal: str = "") -> dict[str, Any]:
    if principal:
        client.headers["X-Golem-Principal"] = principal
    try:
        return rpc(client, "GetTask", {"id": task_id, "tenant": "reviewer"})
    finally:
        client.headers.pop("X-Golem-Principal", None)


def send_as(client: TestClient, principal: str, message_id: str) -> dict[str, Any]:
    client.headers["X-Golem-Principal"] = principal
    try:
        return rpc(
            client,
            "SendMessage",
            {
                "tenant": "reviewer",
                "message": {
                    "role": "ROLE_USER",
                    "messageId": message_id,
                    "parts": [{"text": "go"}],
                },
            },
        )["task"]
    finally:
        client.headers.pop("X-Golem-Principal")


def test_a_succeeded_run_completes_its_task(
    client: TestClient, orchestrator: FakeOrchestrator
) -> None:
    task = send(client, "fix the flaky test")
    orchestrator.finish(task["id"], succeeded=True, detail="MR !42 opened")

    response = notify(client, task["id"], run_id="run-1")

    assert response.status_code == 200
    done = get_task(client, task["id"])
    assert done["status"]["state"] == "TASK_STATE_COMPLETED"
    assert "MR !42 opened" in status_text(done)


MERGE_REQUEST = ProposalView(
    id="p-1",
    kind="merge_request",
    state="pending",
    url="https://gitlab.example.test/p/-/merge_requests/7",
)


def finished_with_a_proposal(client: TestClient, orchestrator: FakeOrchestrator) -> str:
    task = send(client, "fix the flaky test")
    orchestrator.finish(task["id"], succeeded=True, detail="MR !7", proposal=MERGE_REQUEST)
    assert notify(client, task["id"]).status_code == 200
    return task["id"]


def test_a_succeeded_run_shows_its_proposal_on_the_task(
    client: TestClient, orchestrator: FakeOrchestrator
) -> None:
    task_id = finished_with_a_proposal(client, orchestrator)

    done = get_task(client, task_id)
    assert done["status"]["state"] == "TASK_STATE_COMPLETED"
    assert done["metadata"]["golemProposal"] == {
        "id": "p-1",
        "kind": "merge_request",
        "state": "pending",
        "url": "https://gitlab.example.test/p/-/merge_requests/7",
    }


def test_a_reported_run_completes_its_task_with_the_report(
    client: TestClient, orchestrator: FakeOrchestrator
) -> None:
    task = send(client, "disk usage alert on node-3")
    orchestrator.finish(
        task["id"], succeeded=True, detail="Run run-1 reported.", report="# Seen\n\nA deploy."
    )

    assert notify(client, task["id"]).status_code == 200

    done = get_task(client, task["id"])
    assert done["status"]["state"] == "TASK_STATE_COMPLETED"
    assert done["metadata"]["golemOutcome"] == "reported"
    assert "golemProposal" not in done["metadata"]
    [artifact] = done["artifacts"]
    assert artifact["name"] == "report"
    assert artifact["parts"] == [{"text": "# Seen\n\nA deploy."}]


def test_a_proposal_the_platform_applies_is_the_tasks_proposal_artifact(
    client: TestClient, orchestrator: FakeOrchestrator
) -> None:
    task = send(client, "answer SD-12")
    reply = ProposalView(id="p-2", kind="desk_reply", state="pending", url="")
    payload = {"request": "SD-12", "public": True, "text": "The export works again."}
    orchestrator.finish(
        task["id"], succeeded=True, detail="waits", proposal=reply, proposal_payload=payload
    )

    assert notify(client, task["id"]).status_code == 200

    done = get_task(client, task["id"])
    assert done["metadata"]["golemProposal"]["kind"] == "desk_reply"
    [artifact] = done["artifacts"]
    assert artifact["name"] == "proposal"
    assert artifact["parts"] == [{"data": payload}]


def test_a_proposal_state_change_moves_only_the_status_timestamp(
    client: TestClient, orchestrator: FakeOrchestrator
) -> None:
    task_id = finished_with_a_proposal(client, orchestrator)
    before = get_task(client, task_id)
    orchestrator.proposals["p-1"] = ProposalRecord(
        view=replace(MERGE_REQUEST, state="applied"),
        caller="anonymous",
        agent="reviewer",
        task_ids=(task_id,),
    )

    response = client.post(PROPOSAL_STATE_PATH, json={"proposal_id": "p-1"})

    after = get_task(client, task_id)
    assert response.status_code == 200
    assert after["metadata"]["golemProposal"]["state"] == "applied"
    assert after["metadata"]["runId"] == "run-1"
    # The board's delta reads by status timestamp, so a proposal's change must move it; the
    # state and the message stay what the run's outcome made them.
    assert after["status"]["timestamp"] > before["status"]["timestamp"]
    assert {k: v for k, v in after["status"].items() if k != "timestamp"} == {
        k: v for k, v in before["status"].items() if k != "timestamp"
    }


def test_a_state_change_that_changes_nothing_keeps_the_timestamp(
    client: TestClient, orchestrator: FakeOrchestrator
) -> None:
    task_id = finished_with_a_proposal(client, orchestrator)
    client.post(PROPOSAL_STATE_PATH, json={"proposal_id": "p-1"})
    before = get_task(client, task_id)

    client.post(PROPOSAL_STATE_PATH, json={"proposal_id": "p-1"})

    assert get_task(client, task_id)["status"] == before["status"]


def test_a_state_change_of_an_unknown_proposal_is_not_found(client: TestClient) -> None:
    response = client.post(PROPOSAL_STATE_PATH, json={"proposal_id": "p-404"})

    assert response.status_code == 404


def test_a_state_change_without_a_proposal_id_is_refused(client: TestClient) -> None:
    response = client.post(PROPOSAL_STATE_PATH, json={"state": "applied"})

    assert response.status_code == 422


def test_a_failed_run_fails_its_task(client: TestClient, orchestrator: FakeOrchestrator) -> None:
    task = send(client, "fix the flaky test")
    orchestrator.finish(task["id"], succeeded=False, detail="validators red")

    notify(client, task["id"])

    failed = get_task(client, task["id"])
    assert failed["status"]["state"] == "TASK_STATE_FAILED"
    assert "validators red" in status_text(failed)


def test_a_forged_success_for_a_running_run_leaves_the_task_working(client: TestClient) -> None:
    task = send(client, "fix the flaky test")

    response = notify(
        client, task["id"], run_id="run-1", status="succeeded", detail="MR !666 opened"
    )

    assert response.status_code == 409
    working = get_task(client, task["id"])
    assert working["status"]["state"] == "TASK_STATE_WORKING"
    assert "MR !666" not in str(working)


def test_a_forged_failure_delivers_the_true_outcome_instead(
    client: TestClient, orchestrator: FakeOrchestrator
) -> None:
    task = send(client, "fix the flaky test")
    orchestrator.finish(task["id"], succeeded=True, detail="MR !42 opened")

    notify(client, task["id"], status="failed", detail="validators red")

    done = get_task(client, task["id"])
    assert done["status"]["state"] == "TASK_STATE_COMPLETED"
    assert "MR !42 opened" in status_text(done)
    assert "validators red" not in str(done)


def test_a_notification_naming_another_callers_run_reaches_only_the_tasks_own_run(
    client: TestClient, orchestrator: FakeOrchestrator
) -> None:
    alices = send_as(client, "user:alice", "m-alice")
    bobs = send_as(client, "user:bob", "m-bob")
    orchestrator.finish(bobs["id"], succeeded=False, detail="Bob's run failed")

    # Alice's caller and tenant in the body, even Alice's run: Bob's task is still addressed
    # as Bob, the owner the orchestrator recorded, and gets Bob's outcome.
    response = notify(
        client,
        bobs["id"],
        run_id="run-alice",
        caller="user:alice",
        tenant="other-agent",
        status="succeeded",
        detail="Alice's MR",
    )

    assert response.status_code == 200
    bob_sees = get_task(client, bobs["id"], principal="user:bob")
    assert bob_sees["status"]["state"] == "TASK_STATE_FAILED"
    assert "Bob's run failed" in status_text(bob_sees)
    assert "Alice's MR" not in str(bob_sees)
    assert get_task(client, alices["id"], principal="user:alice")["status"]["state"] == (
        "TASK_STATE_WORKING"
    )


def test_a_notification_without_a_task_id_is_refused(client: TestClient) -> None:
    task = send(client, "fix the flaky test")

    response = client.post("/internal/run-outcome", json={"run_id": "run-1", "status": "succeeded"})

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
    response = notify(client, "no-such-task", status="succeeded")

    assert response.status_code == 404


def test_a_repeated_notification_is_accepted_and_changes_nothing(
    client: TestClient, orchestrator: FakeOrchestrator
) -> None:
    task = send(client, "fix the flaky test")
    orchestrator.finish(task["id"], succeeded=True, detail="MR !42 opened")
    notify(client, task["id"])

    again = notify(client, task["id"])

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


@dataclass
class StatusOrchestrator(FakeOrchestrator):
    statuses: dict[str, str] = field(default_factory=dict)
    asked: list[str] = field(default_factory=list)

    async def status(self, run_id: str) -> str | None:
        self.asked.append(run_id)
        return self.statuses.get(run_id)


def test_a_runs_status_is_served_on_an_internal_route() -> None:
    orchestrator = StatusOrchestrator(statuses={"run-1": "canceled"})

    with TestClient(create_app(make_card(), orchestrator)) as client:
        found = client.get("/internal/runs/run-1")
        missing = client.get("/internal/runs/run-2")

    assert found.status_code == 200
    assert found.json() == {"run_id": "run-1", "status": "canceled"}
    assert missing.status_code == 404
    assert orchestrator.asked == ["run-1", "run-2"]
