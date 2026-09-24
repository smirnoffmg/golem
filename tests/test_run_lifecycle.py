"""A task's whole life: A2A task -> run in Postgres -> Job -> reconcile -> finished task."""

from collections.abc import AsyncIterator
from decimal import Decimal
from typing import Any

import httpx
import psycopg
import pytest
from test_reconcile import StatusBoard
from test_tasks_service import create_app, make_card
from test_tasks_to_runs import CATALOG, GRANTS, SIGNING_KEY, TEMPLATE

from golem.orchestrator.admission import Limits
from golem.orchestrator.jobs import JobStatus
from golem.orchestrator.notify import TaskServiceNotifier
from golem.orchestrator.reconcile import TaskOutcome, reconcile_once
from golem.orchestrator.service import PostgresOrchestrator


@pytest.fixture
def board() -> StatusBoard:
    return StatusBoard()


@pytest.fixture
async def task_service(runs_db: str, board: StatusBoard) -> AsyncIterator[httpx.AsyncClient]:
    orchestrator = PostgresOrchestrator(
        dsn=runs_db,
        limits=Limits(max_runs_per_caller=3, max_runs_per_root=3, budget_per_root=Decimal("10")),
        estimated_cost=Decimal("1"),
        launcher=board,
        template=TEMPLATE,
        catalogs={"discovery": CATALOG},
        signing_key=SIGNING_KEY,
        grants=GRANTS,
    )
    app = create_app(make_card(), orchestrator)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://tasks"
    ) as client:
        yield client


async def rpc(client: httpx.AsyncClient, method: str, params: dict[str, Any]) -> dict[str, Any]:
    response = await client.post(
        "/a2a",
        headers={"A2A-Version": "1.0", "X-Golem-Principal": "user:alice"},
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
    )
    body = response.json()
    assert "error" not in body, body
    return body["result"]


async def start_task(client: httpx.AsyncClient) -> dict[str, Any]:
    return (
        await rpc(
            client,
            "SendMessage",
            {
                "tenant": "discovery",
                "message": {"role": "ROLE_USER", "messageId": "m-1", "parts": [{"text": "go"}]},
            },
        )
    )["task"]


async def reconcile(dsn: str, board: StatusBoard, client: httpx.AsyncClient) -> None:
    async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as conn:
        await reconcile_once(conn, board, TaskServiceNotifier(client).notify)


async def test_a_finished_job_completes_the_a2a_task(
    runs_db: str, board: StatusBoard, task_service: httpx.AsyncClient
) -> None:
    task = await start_task(task_service)
    assert task["status"]["state"] == "TASK_STATE_WORKING"

    board.statuses[task["metadata"]["runId"]] = JobStatus.SUCCEEDED
    await reconcile(runs_db, board, task_service)

    done = await rpc(task_service, "GetTask", {"id": task["id"], "tenant": "discovery"})
    assert done["status"]["state"] == "TASK_STATE_COMPLETED"


async def test_a_vanished_job_fails_the_a2a_task_with_the_reason(
    runs_db: str, board: StatusBoard, task_service: httpx.AsyncClient
) -> None:
    task = await start_task(task_service)

    board.statuses[task["metadata"]["runId"]] = JobStatus.MISSING
    await reconcile(runs_db, board, task_service)

    failed = await rpc(task_service, "GetTask", {"id": task["id"], "tenant": "discovery"})
    assert failed["status"]["state"] == "TASK_STATE_FAILED"
    assert "disappeared" in failed["status"]["message"]["parts"][0]["text"]


async def test_an_unreachable_task_service_leaves_the_notification_pending() -> None:
    async def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("task service down", request=request)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(refuse), base_url="http://tasks"
    ) as client:
        delivered = await TaskServiceNotifier(client).notify(
            TaskOutcome("t", "discovery", "user:alice", "r", "succeeded", "done")
        )

    assert delivered is False
