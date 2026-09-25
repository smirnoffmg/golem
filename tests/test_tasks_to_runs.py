"""The task service wired to the Postgres-backed orchestrator, end to end over JSON-RPC."""

from collections.abc import Iterator
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import jwt
import psycopg
import pytest
from starlette.testclient import TestClient
from test_tasks_service import create_app, make_card

from golem import call_token
from golem.call_token import CallClaims
from golem.orchestrator.admission import Limits
from golem.orchestrator.jobs import CatalogRef, JobSpec, JobStatus
from golem.orchestrator.service import JobTemplate, PostgresOrchestrator
from golem.run_token import RunClaims, SigningKey, public_jwks, verify

CATALOG = CatalogRef(url="https://git.example.com/agents/discovery.git", revision="v1")
TEMPLATE = JobTemplate(
    image="registry.example.com/golem/runtime:0.1.0",
    namespace="team-jobs",
    secret_name="golem-run-secrets",
    active_deadline_seconds=3600,
    ttl_seconds_after_finished=600,
    cpu="1",
    memory="1Gi",
)
SIGNING_KEY = SigningKey.generate(kid="test-key")
GRANTS = {"discovery": ("tracker.read", "wiki.read")}
NOW = 1_800_000_000


def fixed_clock() -> float:
    return NOW


@dataclass
class FakeLauncher:
    """The Kubernetes API is a boundary; the real launcher is tested against k3s."""

    launched: list[JobSpec] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    failure: Exception | None = None

    def launch(self, spec: JobSpec) -> None:
        if self.failure is not None:
            raise self.failure
        self.launched.append(spec)

    def status(self, run_id: str) -> JobStatus:
        return JobStatus.RUNNING

    def delete(self, run_id: str) -> None:
        self.deleted.append(run_id)

    def termination_message(self, run_id: str) -> str | None:
        return None


@pytest.fixture
def launcher() -> FakeLauncher:
    return FakeLauncher()


@pytest.fixture
def client(runs_db: str, launcher: FakeLauncher) -> Iterator[TestClient]:
    orchestrator = PostgresOrchestrator(
        dsn=runs_db,
        limits=Limits(max_runs_per_caller=1, max_runs_per_root=3, budget_per_root=Decimal("10")),
        estimated_cost=Decimal("1"),
        launcher=launcher,
        template=TEMPLATE,
        catalogs={"discovery": CATALOG, "reviewer": CATALOG},
        signing_key=SIGNING_KEY,
        grants=GRANTS,
        clock=fixed_clock,
    )
    with TestClient(create_app(make_card(), orchestrator)) as test_client:
        yield test_client


def rpc(client: TestClient, method: str, params: dict[str, Any]) -> dict[str, Any]:
    body = client.post(
        "/a2a",
        headers={"A2A-Version": "1.0"},
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
    ).json()
    assert "error" not in body, body
    return body["result"]


def send(client: TestClient, message_id: str, tenant: str = "discovery") -> dict[str, Any]:
    return rpc(
        client,
        "SendMessage",
        {
            "tenant": tenant,
            "message": {"role": "ROLE_USER", "messageId": message_id, "parts": [{"text": "go"}]},
        },
    )["task"]


def run_rows(dsn: str) -> list[tuple[str, str]]:
    with psycopg.connect(dsn) as conn:
        return [
            (str(run_id), status)
            for run_id, status in conn.execute("SELECT id, status FROM runs").fetchall()
        ]


def test_a_task_starts_a_recorded_run(client: TestClient, runs_db: str) -> None:
    task = send(client, "m-1")

    assert task["status"]["state"] == "TASK_STATE_WORKING"
    assert run_rows(runs_db) == [(task["metadata"]["runId"], "running")]


def test_a_retried_message_gets_a_new_task_but_the_same_run(
    client: TestClient, runs_db: str
) -> None:
    first = send(client, "m-1")
    retry = send(client, "m-1")

    assert retry["id"] != first["id"]
    assert retry["metadata"]["runId"] == first["metadata"]["runId"]
    assert len(run_rows(runs_db)) == 1


def test_a_run_over_the_limit_is_rejected_with_the_reason(client: TestClient) -> None:
    send(client, "m-1")

    rejected = send(client, "m-2")

    assert rejected["status"]["state"] == "TASK_STATE_REJECTED"
    reason = " ".join(p["text"] for p in rejected["status"]["message"]["parts"])
    assert "limit" in reason


def test_canceling_the_task_cancels_the_run(client: TestClient, runs_db: str) -> None:
    task = send(client, "m-1")

    rpc(client, "CancelTask", {"id": task["id"], "tenant": "discovery"})

    assert run_rows(runs_db) == [(task["metadata"]["runId"], "canceled")]


def test_canceling_a_retry_task_cancels_the_shared_run(client: TestClient, runs_db: str) -> None:
    send(client, "m-1")
    retry = send(client, "m-1")

    rpc(client, "CancelTask", {"id": retry["id"], "tenant": "discovery"})

    assert [status for _, status in run_rows(runs_db)] == ["canceled"]


def test_a_retry_of_a_canceled_run_is_rejected_not_left_working(client: TestClient) -> None:
    task = send(client, "m-1")
    rpc(client, "CancelTask", {"id": task["id"], "tenant": "discovery"})

    retry = send(client, "m-1")

    assert retry["status"]["state"] == "TASK_STATE_REJECTED"
    reason = " ".join(p["text"] for p in retry["status"]["message"]["parts"])
    assert task["metadata"]["runId"] in reason
    assert "canceled" in reason


def test_a_started_run_launches_its_job(client: TestClient, launcher: FakeLauncher) -> None:
    task = send(client, "m-1")

    [spec] = launcher.launched
    assert spec.run_id == task["metadata"]["runId"]
    assert spec.agent == "discovery"
    assert spec.catalog_ref == CATALOG
    assert spec.goal == "go"
    assert spec.namespace == TEMPLATE.namespace


def test_a_retry_relaunches_the_same_job_so_a_crash_before_launch_heals(
    client: TestClient, launcher: FakeLauncher
) -> None:
    send(client, "m-1")
    send(client, "m-1")

    assert len({spec.run_id for spec in launcher.launched}) == 1


def test_an_agent_without_a_catalog_is_rejected_before_any_run(
    client: TestClient, launcher: FakeLauncher, runs_db: str
) -> None:
    task = send(client, "m-1", tenant="unknown")

    assert task["status"]["state"] == "TASK_STATE_REJECTED"
    assert run_rows(runs_db) == []
    assert launcher.launched == []


def test_a_failed_launch_rejects_the_task_and_fails_the_run(
    client: TestClient, launcher: FakeLauncher, runs_db: str
) -> None:
    launcher.failure = RuntimeError("quota exceeded")

    task = send(client, "m-1")

    assert task["status"]["state"] == "TASK_STATE_REJECTED"
    assert "quota exceeded" in " ".join(p["text"] for p in task["status"]["message"]["parts"])
    assert [status for _, status in run_rows(runs_db)] == ["failed"]


def test_canceling_the_task_deletes_the_job(client: TestClient, launcher: FakeLauncher) -> None:
    task = send(client, "m-1")

    rpc(client, "CancelTask", {"id": task["id"], "tenant": "discovery"})

    assert launcher.deleted == [task["metadata"]["runId"]]


def verified(token: str) -> object:
    return verify(token, jwt.PyJWKSet.from_dict(public_jwks([SIGNING_KEY])), now=NOW)


def test_a_launched_job_carries_a_run_token_with_the_agents_grants(
    client: TestClient, launcher: FakeLauncher
) -> None:
    client.headers["X-Golem-Principal"] = "user:alice"

    task = send(client, "m-1")

    [spec] = launcher.launched
    claims = verified(spec.run_token)
    run_id = task["metadata"]["runId"]
    assert claims == RunClaims(
        run_id=run_id,
        agent="discovery",
        caller="user:alice",
        root_run_id=run_id,
        tools=("tracker.read", "wiki.read"),
        expires_at=NOW + TEMPLATE.active_deadline_seconds + 60,
    )


def test_an_agent_the_platform_granted_nothing_gets_a_token_without_tools(
    client: TestClient, launcher: FakeLauncher
) -> None:
    send(client, "m-1", tenant="reviewer")

    [spec] = launcher.launched
    claims = verified(spec.run_token)
    assert isinstance(claims, RunClaims)
    assert claims.tools == ()


def test_the_internal_run_route_reads_the_status_from_golem_runs(
    client: TestClient, runs_db: str
) -> None:
    task = send(client, "m-status")
    [(run_id, _)] = run_rows(runs_db)

    running = client.get(f"/internal/runs/{run_id}").json()
    rpc(client, "CancelTask", {"tenant": "discovery", "id": task["id"]})
    canceled = client.get(f"/internal/runs/{run_id}").json()

    assert running == {"run_id": run_id, "status": "running"}
    assert canceled == {"run_id": run_id, "status": "canceled"}
    assert client.get("/internal/runs/not-a-run").status_code == 404


# Delegation (ADR 0014): call tokens, and a child run admitted under its chain's root


def call_claims(token: str) -> object:
    return call_token.verify(token, jwt.PyJWKSet.from_dict(public_jwks([SIGNING_KEY])), now=NOW)


def send_delegated(
    client: TestClient, message_id: str, *, chain: str, root: str, tenant: str = "reviewer"
) -> dict[str, Any]:
    """A message as the edge forwards a verified call token's delegation."""
    body = client.post(
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
                "tenant": tenant,
                "message": {
                    "role": "ROLE_USER",
                    "messageId": message_id,
                    "parts": [{"text": "go"}],
                },
            },
        },
    ).json()
    assert "error" not in body, body
    return body["result"]["task"]


def root_rows(dsn: str) -> dict[str, str]:
    with psycopg.connect(dsn) as conn:
        rows = conn.execute("SELECT id, root_run_id FROM runs").fetchall()
    return {str(run_id): str(root) for run_id, root in rows}


@pytest.fixture
def chain_client(runs_db: str, launcher: FakeLauncher) -> Iterator[TestClient]:
    """Two runs' worth of budget per chain, and room for many runs per caller and chain."""
    orchestrator = PostgresOrchestrator(
        dsn=runs_db,
        limits=Limits(max_runs_per_caller=10, max_runs_per_root=10, budget_per_root=Decimal("2")),
        estimated_cost=Decimal("1"),
        launcher=launcher,
        template=TEMPLATE,
        catalogs={"discovery": CATALOG, "reviewer": CATALOG},
        signing_key=SIGNING_KEY,
        grants=GRANTS,
        clock=fixed_clock,
    )
    with TestClient(create_app(make_card(), orchestrator)) as test_client:
        yield test_client


def test_a_launched_job_carries_a_call_token_for_its_subject_and_agent(
    client: TestClient, launcher: FakeLauncher
) -> None:
    client.headers["X-Golem-Principal"] = "user:alice"

    task = send(client, "m-1")

    [spec] = launcher.launched
    run_id = task["metadata"]["runId"]
    assert call_claims(spec.call_token) == CallClaims(
        subject="user:alice",
        agent="discovery",
        chain=("discovery",),
        root_run_id=run_id,
        run_id=run_id,
        expires_at=NOW + TEMPLATE.active_deadline_seconds + 60,
    )


def test_a_delegated_run_is_admitted_under_the_root_and_extends_the_chain(
    chain_client: TestClient, launcher: FakeLauncher, runs_db: str
) -> None:
    chain_client.headers["X-Golem-Principal"] = "user:alice"
    parent = send(chain_client, "m-parent")
    root = parent["metadata"]["runId"]

    child = send_delegated(chain_client, "m-child", chain="discovery", root=root)

    child_run = child["metadata"]["runId"]
    assert child["status"]["state"] == "TASK_STATE_WORKING"
    assert child["metadata"]["chain"] == ["discovery"]
    assert root_rows(runs_db) == {root: root, child_run: root}
    _, spec = launcher.launched
    claims = call_claims(spec.call_token)
    assert isinstance(claims, CallClaims)
    assert (claims.subject, claims.agent, claims.chain, claims.root_run_id, claims.run_id) == (
        "user:alice",
        "reviewer",
        ("discovery", "reviewer"),
        root,
        child_run,
    )
    run_claims = verified(spec.run_token)
    assert isinstance(run_claims, RunClaims)
    assert (run_claims.caller, run_claims.root_run_id) == ("user:alice", root)


def test_a_chain_over_its_roots_budget_is_rejected(
    chain_client: TestClient, launcher: FakeLauncher, runs_db: str
) -> None:
    chain_client.headers["X-Golem-Principal"] = "user:alice"
    root = send(chain_client, "m-parent")["metadata"]["runId"]
    send_delegated(chain_client, "m-child-1", chain="discovery", root=root)

    over = send_delegated(chain_client, "m-child-2", chain="discovery", root=root)

    assert over["status"]["state"] == "TASK_STATE_REJECTED"
    assert "budget" in " ".join(p["text"] for p in over["status"]["message"]["parts"])
    assert len(root_rows(runs_db)) == 2
    assert len(launcher.launched) == 2


def test_a_root_that_is_not_a_run_id_is_refused_before_any_run(
    chain_client: TestClient, launcher: FakeLauncher, runs_db: str
) -> None:
    task = send_delegated(chain_client, "m-1", chain="discovery", root="not-a-run")

    assert task["status"]["state"] == "TASK_STATE_REJECTED"
    assert root_rows(runs_db) == {}
    assert launcher.launched == []
