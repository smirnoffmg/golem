"""A task's whole life: A2A task -> run in Postgres -> Job -> reconcile -> finished task."""

import json
from collections.abc import AsyncIterator
from decimal import Decimal
from typing import Any

import httpx
import psycopg
import pytest
from test_reconcile import StatusBoard, age
from test_tasks_service import create_app, make_card
from test_tasks_to_runs import CATALOG, GRANTS, SIGNING_KEY, TEMPLATE

from golem.orchestrator.admission import Limits
from golem.orchestrator.jobs import JobStatus
from golem.orchestrator.notify import TaskServiceNotifier
from golem.orchestrator.proposals import OpenedMergeRequest, PendingMergeRequest, Transition
from golem.orchestrator.reconcile import (
    LAUNCH_GRACE_SECONDS,
    Propose,
    Settlement,
    SucceededRun,
    TaskOutcome,
    reconcile_once,
)
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


async def rpc(
    client: httpx.AsyncClient,
    method: str,
    params: dict[str, Any],
    principal: str = "user:alice",
) -> dict[str, Any]:
    response = await client.post(
        "/a2a",
        headers={"A2A-Version": "1.0", "X-Golem-Principal": principal},
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
    )
    body = response.json()
    assert "error" not in body, body
    return body["result"]


async def start_task(
    client: httpx.AsyncClient, principal: str = "user:alice", message_id: str = "m-1"
) -> dict[str, Any]:
    return (
        await rpc(
            client,
            "SendMessage",
            {
                "tenant": "discovery",
                "message": {
                    "role": "ROLE_USER",
                    "messageId": message_id,
                    "parts": [{"text": "go"}],
                },
            },
            principal,
        )
    )["task"]


async def get_task(
    client: httpx.AsyncClient, task_id: str, principal: str = "user:alice"
) -> dict[str, Any]:
    return await rpc(client, "GetTask", {"id": task_id, "tenant": "discovery"}, principal)


async def reconcile(
    dsn: str, board: StatusBoard, client: httpx.AsyncClient, propose: Propose | None = None
) -> None:
    async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as conn:
        await reconcile_once(conn, board, TaskServiceNotifier(client).notify, propose)


async def reconcile_undelivered(dsn: str, board: StatusBoard, propose: Propose) -> None:
    """Finish and settle runs as the reconciler would, but leave every notification pending."""

    async def unreachable(_: TaskOutcome) -> bool:
        return False

    async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as conn:
        await reconcile_once(conn, board, unreachable, propose)


async def open_merge_request(run: SucceededRun) -> Settlement:
    url = f"https://gitlab.example.test/p/-/merge_requests/{run.run_id}"
    return Settlement(f"Merge request: {url}", OpenedMergeRequest(url=url, iid=7, target="H-7"))


async def test_a_finished_job_completes_the_a2a_task(
    runs_db: str, board: StatusBoard, task_service: httpx.AsyncClient
) -> None:
    task = await start_task(task_service)
    assert task["status"]["state"] == "TASK_STATE_WORKING"

    board.statuses[task["metadata"]["runId"]] = JobStatus.SUCCEEDED
    await reconcile(runs_db, board, task_service)

    done = await get_task(task_service, task["id"])
    assert done["status"]["state"] == "TASK_STATE_COMPLETED"


async def test_a_vanished_job_fails_the_a2a_task_with_the_reason(
    runs_db: str, board: StatusBoard, task_service: httpx.AsyncClient
) -> None:
    task = await start_task(task_service)

    await age(runs_db, task["metadata"]["runId"], LAUNCH_GRACE_SECONDS + 1)
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
        delivered = await TaskServiceNotifier(client).notify(TaskOutcome("t", "r"))

    assert delivered is False


async def test_the_notification_names_only_the_task_and_its_run() -> None:
    sent: list[httpx.Request] = []

    async def accept(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, json={"task_id": "t"})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(accept), base_url="http://tasks"
    ) as client:
        delivered = await TaskServiceNotifier(client).notify(TaskOutcome("t", "r"))

    [request] = sent
    assert delivered is True
    assert request.url.path == "/internal/run-outcome"
    assert json.loads(request.content) == {"task_id": "t", "run_id": "r"}


async def test_a_run_not_yet_final_keeps_the_notification_pending() -> None:
    async def not_final(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={"error": "the task's run has no final outcome yet"})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(not_final), base_url="http://tasks"
    ) as client:
        assert await TaskServiceNotifier(client).notify(TaskOutcome("t", "r")) is False


# The write port against the system of record: a notification names a task, and the task service
# reads that task's run from golem_runs; whatever else the body claims is ignored.


async def test_a_forged_success_for_a_running_run_leaves_the_task_working(
    task_service: httpx.AsyncClient,
) -> None:
    task = await start_task(task_service)

    response = await task_service.post(
        "/internal/run-outcome",
        json={
            "task_id": task["id"],
            "run_id": task["metadata"]["runId"],
            "tenant": "discovery",
            "caller": "user:alice",
            "status": "succeeded",
            "detail": "Merge request: https://evil.example.test/mr/1",
        },
    )

    assert response.status_code == 409
    working = await get_task(task_service, task["id"])
    assert working["status"]["state"] == "TASK_STATE_WORKING"
    assert "evil" not in json.dumps(working)


async def test_a_forged_failure_of_a_settled_run_delivers_its_merge_request(
    runs_db: str, board: StatusBoard, task_service: httpx.AsyncClient
) -> None:
    task = await start_task(task_service)
    run_id = task["metadata"]["runId"]
    board.statuses[run_id] = JobStatus.SUCCEEDED
    await reconcile_undelivered(runs_db, board, open_merge_request)

    response = await task_service.post(
        "/internal/run-outcome",
        json={"task_id": task["id"], "status": "failed", "detail": "validators red"},
    )

    assert response.status_code == 200
    done = await get_task(task_service, task["id"])
    assert done["status"]["state"] == "TASK_STATE_COMPLETED"
    text = done["status"]["message"]["parts"][0]["text"]
    assert text == f"Merge request: https://gitlab.example.test/p/-/merge_requests/{run_id}"


async def test_a_notification_naming_another_callers_run_delivers_the_tasks_own_outcome(
    runs_db: str, board: StatusBoard, task_service: httpx.AsyncClient
) -> None:
    alices = await start_task(task_service, "user:alice", "m-alice")
    bobs = await start_task(task_service, "user:bob", "m-bob")
    board.statuses[alices["metadata"]["runId"]] = JobStatus.SUCCEEDED
    await age(runs_db, bobs["metadata"]["runId"], LAUNCH_GRACE_SECONDS + 1)
    board.statuses[bobs["metadata"]["runId"]] = JobStatus.MISSING
    await reconcile_undelivered(runs_db, board, open_merge_request)

    response = await task_service.post(
        "/internal/run-outcome",
        json={
            "task_id": bobs["id"],
            "run_id": alices["metadata"]["runId"],
            "tenant": "discovery",
            "caller": "user:alice",
            "status": "succeeded",
        },
    )

    assert response.status_code == 200
    bob_sees = await get_task(task_service, bobs["id"], "user:bob")
    assert bob_sees["status"]["state"] == "TASK_STATE_FAILED"
    assert "disappeared" in bob_sees["status"]["message"]["parts"][0]["text"]
    alice_sees = await get_task(task_service, alices["id"], "user:alice")
    assert alice_sees["status"]["state"] == "TASK_STATE_WORKING"


async def test_a_notification_for_an_unknown_task_is_not_found(
    task_service: httpx.AsyncClient,
) -> None:
    response = await task_service.post(
        "/internal/run-outcome", json={"task_id": "no-such-task", "status": "succeeded"}
    )

    assert response.status_code == 404


async def test_a_canceled_run_is_not_delivered_over_its_canceled_task(
    task_service: httpx.AsyncClient,
) -> None:
    task = await start_task(task_service)
    await rpc(task_service, "CancelTask", {"id": task["id"], "tenant": "discovery"})

    response = await task_service.post(
        "/internal/run-outcome", json={"task_id": task["id"], "status": "succeeded"}
    )

    assert response.status_code == 409
    canceled = await get_task(task_service, task["id"])
    assert canceled["status"]["state"] == "TASK_STATE_CANCELED"


async def test_a_repeated_notification_changes_nothing(
    runs_db: str, board: StatusBoard, task_service: httpx.AsyncClient
) -> None:
    task = await start_task(task_service)
    board.statuses[task["metadata"]["runId"]] = JobStatus.SUCCEEDED
    await reconcile(runs_db, board, task_service, open_merge_request)
    done = await get_task(task_service, task["id"])

    again = await task_service.post("/internal/run-outcome", json={"task_id": task["id"]})

    assert again.status_code == 200
    assert await get_task(task_service, task["id"]) == done


# A proposal's life after its task completed: GitLab's decision reaches the stored task.


async def test_a_merged_merge_request_shows_as_applied_on_the_completed_task(
    runs_db: str, board: StatusBoard, task_service: httpx.AsyncClient
) -> None:
    task = await start_task(task_service)
    board.statuses[task["metadata"]["runId"]] = JobStatus.SUCCEEDED
    await reconcile(runs_db, board, task_service, open_merge_request)
    done = await get_task(task_service, task["id"])

    async def merged(_: PendingMergeRequest) -> Transition:
        return Transition(state="applied", decided_by="gitlab:bob", decided_at=None)

    notifier = TaskServiceNotifier(task_service)
    async with await psycopg.AsyncConnection.connect(runs_db, autocommit=True) as conn:
        await reconcile_once(
            conn,
            board,
            notifier.notify,
            open_merge_request,
            check=merged,
            notify_proposal=notifier.notify_proposal,
            poll_seconds=0,
        )

    after = await get_task(task_service, task["id"])
    assert done["metadata"]["golemProposal"]["state"] == "pending"
    assert after["metadata"]["golemProposal"] == {
        **done["metadata"]["golemProposal"],
        "state": "applied",
    }
    assert after["status"] == done["status"]
