import json
from dataclasses import dataclass, field
from decimal import Decimal

import psycopg
import pytest
from kubernetes.client.exceptions import ApiException

from golem.orchestrator.admission import Limits
from golem.orchestrator.jobs import JobSpec, JobStatus
from golem.orchestrator.reconcile import (
    LAUNCH_GRACE_SECONDS,
    MAX_REPORT_CHARS,
    Reporter,
    Settlement,
    SucceededRun,
    TaskOutcome,
    outcome_detail,
    reconcile_once,
    report_record,
    run_outcome,
)
from golem.orchestrator.runs import (
    RecordedRun,
    RunCreated,
    StartRequest,
    cancel_run_of_task,
    run_of_task,
    start_run,
)

LIMITS = Limits(max_runs_per_caller=5, max_runs_per_root=5, budget_per_root=Decimal("100"))


@dataclass
class StatusBoard:
    """Job statuses as the Kubernetes API would report them; the real launcher runs on k3s."""

    statuses: dict[str, JobStatus] = field(default_factory=dict)
    messages: dict[str, str] = field(default_factory=dict)
    # Runs whose Job the API server fails to read (a 403, a 500, a timeout).
    broken: set[str] = field(default_factory=set)

    def launch(self, spec: JobSpec) -> None: ...

    def status(self, run_id: str) -> JobStatus:
        if run_id in self.broken:
            raise ApiException(status=500, reason="Internal Server Error")
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


async def recorded(dsn: str, task_id: str) -> RecordedRun:
    """What the task service reads for a notified task: the run as golem_runs holds it."""
    async with await connect(dsn) as conn:
        run = await run_of_task(conn, task_id)
    assert run is not None
    return run


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
    assert inbox.received == [TaskOutcome(task_id="task-m-1", run_id=run_id)]
    assert await recorded(runs_db, "task-m-1") == RecordedRun(
        run_id=run_id,
        caller="user:alice",
        agent="discovery",
        status="succeeded",
        detail=f"Run {run_id} succeeded.",
        final=True,
    )


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
    assert [o.task_id for o in inbox.received] == ["task-m-1"]
    assert (await recorded(runs_db, "task-m-1")).status == "failed"


async def age(dsn: str, run_id: str, seconds: int) -> None:
    async with await connect(dsn) as conn:
        await conn.execute(
            "UPDATE runs SET created_at = now() - make_interval(secs => %s) WHERE id = %s",
            (seconds, run_id),
        )


async def test_a_vanished_job_fails_the_run_with_a_reason(
    runs_db: str, board: StatusBoard, inbox: Inbox
):
    run_id = await new_run(runs_db, "m-1")
    await age(runs_db, run_id, LAUNCH_GRACE_SECONDS + 1)
    board.statuses[run_id] = JobStatus.MISSING

    await reconcile(runs_db, board, inbox)

    assert await run_status(runs_db, run_id) == "failed"
    [outcome] = inbox.received
    assert "disappeared" in (await recorded(runs_db, outcome.task_id)).detail


async def test_an_api_error_on_one_run_does_not_hold_back_the_others(
    runs_db: str, board: StatusBoard, inbox: Inbox
):
    broken = await new_run(runs_db, "m-1")
    finished = await new_run(runs_db, "m-2")
    board.statuses[finished] = JobStatus.FAILED
    board.broken.add(broken)

    await reconcile(runs_db, board, inbox)

    assert await run_status(runs_db, broken) == "running"
    assert await run_status(runs_db, finished) == "failed"
    [outcome] = inbox.received
    assert outcome.run_id == finished


async def test_a_run_whose_job_is_still_being_created_is_not_missing(
    runs_db: str, board: StatusBoard, inbox: Inbox
):
    # The run is committed before its Job is created; a pass in between sees no Job.
    run_id = await new_run(runs_db, "m-1")
    board.statuses[run_id] = JobStatus.MISSING

    await reconcile(runs_db, board, inbox)

    assert await run_status(runs_db, run_id) == "running"


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
    detail = (await recorded(runs_db, outcome.task_id)).detail
    assert "rejected by validation" in detail
    assert "solutions/S-9.md is outside hypotheses/" in detail


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


# The task service reads the same rule the outbox delivers by: a notification can only ever
# lead to delivering an outcome the outbox itself would deliver.


async def test_an_unknown_task_has_no_recorded_run(runs_db: str) -> None:
    async with await connect(runs_db) as conn:
        assert await run_of_task(conn, "no-such-task") is None


async def test_only_what_the_outbox_would_deliver_is_final(runs_db: str) -> None:
    running = await new_run(runs_db, "m-running")
    unsettled = await new_run(runs_db, "m-unsettled")
    settled = await new_run(runs_db, "m-settled")
    failed = await new_run(runs_db, "m-failed")
    canceled = await new_run(runs_db, "m-canceled")
    async with await connect(runs_db) as conn:
        await cancel_run_of_task(conn, "task-m-canceled")
        await conn.execute(
            "UPDATE runs SET status = 'succeeded' WHERE id = ANY(%s)", ([unsettled, settled],)
        )
        await conn.execute("UPDATE runs SET proposal_settled_at = now() WHERE id = %s", (settled,))
        await conn.execute("UPDATE runs SET status = 'failed' WHERE id = %s", (failed,))
    inbox = Inbox()

    async def cannot_propose(run: SucceededRun) -> Settlement:
        raise RuntimeError("GitLab is down")

    async with await connect(runs_db) as conn:
        await reconcile_once(conn, StatusBoard(), inbox.notify, cannot_propose)

    delivered = sorted(o.run_id for o in inbox.received)
    finals = {
        run_id: (await recorded(runs_db, f"task-{m}")).final
        for run_id, m in [
            (running, "m-running"),
            (unsettled, "m-unsettled"),
            (settled, "m-settled"),
            (failed, "m-failed"),
            (canceled, "m-canceled"),
        ]
    }
    assert delivered == sorted([settled, failed])
    assert sorted(r for r, final in finals.items() if final) == delivered


# Goal runs that found nothing to propose (ADR 0017): the report, then the branch goes


@dataclass
class FakeReporter:
    """The target record at the branch head, as GitLab would serve it."""

    text: str | None = "# alert-0a1b2c3d4e5f\n\nThe spike was a deploy."
    read_runs: list[SucceededRun] = field(default_factory=list)
    discarded: list[SucceededRun] = field(default_factory=list)
    discard_fails: int = 0

    async def read(self, run: SucceededRun) -> str | None:
        self.read_runs.append(run)
        return self.text

    async def discard(self, run: SucceededRun) -> None:
        if self.discard_fails:
            self.discard_fails -= 1
            raise RuntimeError("GitLab is down")
        self.discarded.append(run)

    def reporter(self) -> Reporter:
        return Reporter(read=self.read, discard=self.discard)


def reported_message(record: str = "hypotheses/alert-0a1b2c3d4e5f.md") -> str:
    return (
        '{"outcome": "reported", "target_id": "alert-0a1b2c3d4e5f",'
        f' "record": "{record}", "summary": "nothing to act on"}}'
    )


async def never_propose(run: SucceededRun) -> Settlement:
    raise AssertionError("a reported run has nothing to propose")


async def reconcile_reported(dsn: str, board: StatusBoard, inbox: Inbox, fake: FakeReporter):
    async with await connect(dsn) as conn:
        await reconcile_once(conn, board, inbox.notify, never_propose, report=fake.reporter())


async def test_a_reported_run_settles_with_its_report_and_no_proposal(
    runs_db: str, board: StatusBoard, inbox: Inbox
):
    run_id = await new_run(runs_db, "m-1")
    board.statuses[run_id] = JobStatus.SUCCEEDED
    board.messages[run_id] = reported_message()
    fake = FakeReporter()

    await reconcile_reported(runs_db, board, inbox, fake)

    run = SucceededRun(run_id, "discovery", record="hypotheses/alert-0a1b2c3d4e5f.md")
    assert (fake.read_runs, fake.discarded) == ([run], [run])
    assert inbox.received == [TaskOutcome(task_id="task-m-1", run_id=run_id)]
    recorded_run = await recorded(runs_db, "task-m-1")
    assert (recorded_run.status, recorded_run.final, recorded_run.proposal) == (
        "succeeded",
        True,
        None,
    )
    assert recorded_run.report == fake.text
    assert "reported" in (recorded_run.detail or "")


async def test_a_report_is_read_once_though_discarding_the_branch_is_retried(
    runs_db: str, board: StatusBoard, inbox: Inbox
):
    run_id = await new_run(runs_db, "m-1")
    board.statuses[run_id] = JobStatus.SUCCEEDED
    board.messages[run_id] = reported_message()
    fake = FakeReporter(discard_fails=1)

    await reconcile_reported(runs_db, board, inbox, fake)
    assert inbox.received == []

    await reconcile_reported(runs_db, board, inbox, fake)

    assert len(fake.read_runs) == 1
    assert len(fake.discarded) == 1
    assert (await recorded(runs_db, "task-m-1")).report == fake.text


async def test_a_long_report_is_cut(runs_db: str, board: StatusBoard, inbox: Inbox):
    run_id = await new_run(runs_db, "m-1")
    board.statuses[run_id] = JobStatus.SUCCEEDED
    board.messages[run_id] = reported_message()

    await reconcile_reported(runs_db, board, inbox, FakeReporter(text="x" * 25_000))

    report = (await recorded(runs_db, "task-m-1")).report
    assert report is not None and len(report) == MAX_REPORT_CHARS


async def test_a_report_whose_branch_is_gone_says_so(
    runs_db: str, board: StatusBoard, inbox: Inbox
):
    run_id = await new_run(runs_db, "m-1")
    board.statuses[run_id] = JobStatus.SUCCEEDED
    board.messages[run_id] = reported_message()

    await reconcile_reported(runs_db, board, inbox, FakeReporter(text=None))

    report = (await recorded(runs_db, "task-m-1")).report
    assert report is not None and "could not be read" in report


async def test_a_report_naming_an_unsafe_record_is_not_read(
    runs_db: str, board: StatusBoard, inbox: Inbox
):
    run_id = await new_run(runs_db, "m-1")
    board.statuses[run_id] = JobStatus.SUCCEEDED
    board.messages[run_id] = reported_message(record="../../etc/passwd")
    fake = FakeReporter()

    await reconcile_reported(runs_db, board, inbox, fake)

    assert fake.read_runs == []
    assert len(fake.discarded) == 1
    report = (await recorded(runs_db, "task-m-1")).report
    assert report is not None and "could not be read" in report


async def test_a_proposing_run_is_not_reported(runs_db: str, board: StatusBoard, inbox: Inbox):
    run_id = await new_run(runs_db, "m-1")
    board.statuses[run_id] = JobStatus.SUCCEEDED
    board.messages[run_id] = '{"outcome": "proposed", "record": "hypotheses/H-2.md"}'
    fake = FakeReporter()
    proposed: list[SucceededRun] = []

    async def propose(run: SucceededRun) -> Settlement:
        proposed.append(run)
        return Settlement(f"Run {run.run_id} succeeded.")

    async with await connect(runs_db) as conn:
        await reconcile_once(conn, board, inbox.notify, propose, report=fake.reporter())

    assert [run.run_id for run in proposed] == [run_id]
    assert fake.read_runs == [] and fake.discarded == []
    assert (await recorded(runs_db, "task-m-1")).report is None


def test_a_reported_run_is_its_own_outcome():
    assert run_outcome(JobStatus.SUCCEEDED, reported_message()) == "reported"
    assert run_outcome(JobStatus.FAILED, reported_message()) == "failed"
    assert "reported" in outcome_detail("r-1", JobStatus.SUCCEEDED, reported_message())


@pytest.mark.parametrize(
    ("record", "safe"),
    [
        ("hypotheses/alert-1.md", "hypotheses/alert-1.md"),
        ("../x.md", None),
        ("/etc/x.md", None),
        ("hypotheses/../../x.md", None),
        ("hypotheses/alert-1.txt", None),
        ("a/" * 300 + "x.md", None),
        (None, None),
        (42, None),
    ],
)
def test_only_a_markdown_path_inside_the_repository_is_read_as_a_report(record, safe):
    assert report_record(json.dumps({"outcome": "reported", "record": record})) == safe
