from dataclasses import dataclass, field
from decimal import Decimal

import psycopg
import pytest

from golem.orchestrator.admission import Limits
from golem.orchestrator.jobs import JobSpec, JobStatus
from golem.orchestrator.reconcile import TaskOutcome, outcome_detail, reconcile_once
from golem.orchestrator.runs import RunCreated, StartRequest, cancel_run_of_task, start_run

LIMITS = Limits(max_runs_per_caller=5, max_runs_per_root=5, budget_per_root=Decimal("100"))


@dataclass
class StatusBoard:
    """Job statuses as the Kubernetes API would report them; the real launcher runs on k3s."""

    statuses: dict[str, JobStatus] = field(default_factory=dict)
    messages: dict[str, str] = field(default_factory=dict)

    def launch(self, spec: JobSpec) -> None: ...

    def status(self, run_id: str) -> JobStatus:
        return self.statuses.get(run_id, JobStatus.RUNNING)

    def delete(self, run_id: str) -> None: ...

    def termination_message(self, run_id: str) -> str | None:
        return self.messages.get(run_id)


@dataclass
class Inbox:
    received: list[TaskOutcome] = field(default_factory=list)
    accepting: bool = True

    async def notify(self, outcome: TaskOutcome) -> bool:
        if self.accepting:
            self.received.append(outcome)
        return self.accepting


async def connect(dsn: str) -> psycopg.AsyncConnection:
    return await psycopg.AsyncConnection.connect(dsn, autocommit=True)


async def new_run(dsn: str, message_id: str, task_id: str | None = None) -> str:
    async with await connect(dsn) as conn:
        outcome = await start_run(
            conn,
            StartRequest(
                caller="user:alice",
                message_id=message_id,
                task_id=task_id or f"task-{message_id}",
                agent="discovery",
                estimated_cost=Decimal("1"),
            ),
            LIMITS,
        )
    assert isinstance(outcome, RunCreated)
    return outcome.run_id


async def run_status(dsn: str, run_id: str) -> str:
    async with await connect(dsn) as conn:
        row = await (
            await conn.execute("SELECT status FROM runs WHERE id = %s", (run_id,))
        ).fetchone()
    assert row is not None
    return row[0]


async def reconcile(dsn: str, board: StatusBoard, inbox: Inbox) -> None:
    async with await connect(dsn) as conn:
        await reconcile_once(conn, board, inbox.notify)


@pytest.fixture
def board() -> StatusBoard:
    return StatusBoard()


@pytest.fixture
def inbox() -> Inbox:
    return Inbox()


async def test_a_running_job_changes_nothing(runs_db: str, board: StatusBoard, inbox: Inbox):
    run_id = await new_run(runs_db, "m-1")

    await reconcile(runs_db, board, inbox)

    assert await run_status(runs_db, run_id) == "running"
    assert inbox.received == []


async def test_a_succeeded_job_finishes_the_run_and_notifies_its_task_once(
    runs_db: str, board: StatusBoard, inbox: Inbox
):
    run_id = await new_run(runs_db, "m-1")
    board.statuses[run_id] = JobStatus.SUCCEEDED

    await reconcile(runs_db, board, inbox)
    await reconcile(runs_db, board, inbox)

    assert await run_status(runs_db, run_id) == "succeeded"
    assert inbox.received == [
        TaskOutcome(
            task_id="task-m-1",
            tenant="discovery",
            caller="user:alice",
            run_id=run_id,
            status="succeeded",
            detail=f"Run {run_id} succeeded.",
        )
    ]


async def test_every_task_of_a_retried_run_is_notified(
    runs_db: str, board: StatusBoard, inbox: Inbox
):
    run_id = await new_run(runs_db, "m-1", task_id="task-first")
    await new_run_retry(runs_db, "m-1", task_id="task-retry")
    board.statuses[run_id] = JobStatus.SUCCEEDED

    await reconcile(runs_db, board, inbox)

    assert sorted(o.task_id for o in inbox.received) == ["task-first", "task-retry"]


async def new_run_retry(dsn: str, message_id: str, task_id: str) -> None:
    async with await connect(dsn) as conn:
        await start_run(
            conn,
            StartRequest(
                caller="user:alice",
                message_id=message_id,
                task_id=task_id,
                agent="discovery",
                estimated_cost=Decimal("1"),
            ),
            LIMITS,
        )


async def test_a_failed_job_fails_the_run(runs_db: str, board: StatusBoard, inbox: Inbox):
    run_id = await new_run(runs_db, "m-1")
    board.statuses[run_id] = JobStatus.FAILED

    await reconcile(runs_db, board, inbox)

    assert await run_status(runs_db, run_id) == "failed"
    assert [o.status for o in inbox.received] == ["failed"]


async def test_a_vanished_job_fails_the_run_with_a_reason(
    runs_db: str, board: StatusBoard, inbox: Inbox
):
    run_id = await new_run(runs_db, "m-1")
    board.statuses[run_id] = JobStatus.MISSING

    await reconcile(runs_db, board, inbox)

    assert await run_status(runs_db, run_id) == "failed"
    [outcome] = inbox.received
    assert "disappeared" in outcome.detail


async def test_an_undelivered_notification_is_retried_until_delivered(
    runs_db: str, board: StatusBoard, inbox: Inbox
):
    run_id = await new_run(runs_db, "m-1")
    board.statuses[run_id] = JobStatus.SUCCEEDED
    inbox.accepting = False

    await reconcile(runs_db, board, inbox)
    assert await run_status(runs_db, run_id) == "succeeded"
    assert inbox.received == []

    inbox.accepting = True
    await reconcile(runs_db, board, inbox)
    await reconcile(runs_db, board, inbox)

    assert len(inbox.received) == 1


async def test_a_canceled_run_is_left_alone(runs_db: str, board: StatusBoard, inbox: Inbox):
    run_id = await new_run(runs_db, "m-1")
    async with await connect(runs_db) as conn:
        await cancel_run_of_task(conn, "task-m-1")
    board.statuses[run_id] = JobStatus.MISSING

    await reconcile(runs_db, board, inbox)

    assert await run_status(runs_db, run_id) == "canceled"
    assert inbox.received == []


async def test_the_runtime_report_explains_a_failed_run(
    runs_db: str, board: StatusBoard, inbox: Inbox
):
    run_id = await new_run(runs_db, "m-1")
    board.statuses[run_id] = JobStatus.FAILED
    board.messages[run_id] = (
        '{"outcome": "invalid", "reasons": ["solutions/S-9.md is outside hypotheses/"]}'
    )

    await reconcile(runs_db, board, inbox)

    [outcome] = inbox.received
    assert "rejected by validation" in outcome.detail
    assert "solutions/S-9.md is outside hypotheses/" in outcome.detail


def test_an_idle_report_says_why_nothing_was_proposed():
    detail = outcome_detail(
        "r-1", JobStatus.SUCCEEDED, '{"outcome": "idle", "reasons": ["researcher: all pending"]}'
    )

    assert detail == "Run r-1 succeeded and proposed no changes: researcher: all pending."


def test_a_runner_failure_report_carries_its_summary():
    detail = outcome_detail(
        "r-1", JobStatus.FAILED, '{"outcome": "failed", "summary": "model gateway timed out"}'
    )

    assert detail == "Run r-1 failed: model gateway timed out."


def test_an_unreadable_report_falls_back_to_the_job_status():
    assert outcome_detail("r-1", JobStatus.FAILED, "not json") == "Run r-1 failed."
    assert outcome_detail("r-1", JobStatus.FAILED, None) == "Run r-1 failed."
