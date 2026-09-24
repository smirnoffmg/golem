"""The task service wired to the Postgres-backed orchestrator, end to end over JSON-RPC."""

from collections.abc import Iterator
from decimal import Decimal
from typing import Any

import psycopg
import pytest
from starlette.testclient import TestClient
from test_tasks_service import make_card

from golem.orchestrator.admission import Limits
from golem.orchestrator.service import PostgresOrchestrator
from golem.tasks.app import create_app


@pytest.fixture
def client(runs_db: str) -> Iterator[TestClient]:
    orchestrator = PostgresOrchestrator(
        dsn=runs_db,
        limits=Limits(max_runs_per_caller=1, max_runs_per_root=3, budget_per_root=Decimal("10")),
        estimated_cost=Decimal("1"),
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


def send(client: TestClient, message_id: str) -> dict[str, Any]:
    return rpc(
        client,
        "SendMessage",
        {
            "tenant": "discovery",
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
