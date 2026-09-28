import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import PurePosixPath

from psycopg import AsyncConnection

from golem.metrics import ReconcilerMetrics
from golem.orchestrator.jobs import JobLauncher, JobStatus
from golem.orchestrator.process_runs import (
    NotifyProcess,
    ProcessPorts,
    advance_processes,
    deliver_views,
)
from golem.orchestrator.proposals import (
    MR_POLL_SECONDS,
    OpenedMergeRequest,
    PendingMergeRequest,
    Transition,
    due_merge_requests,
    mark_delivered,
    record_check,
    record_merge_request,
    undelivered_states,
)
from golem.orchestrator.runs import FINAL_OUTCOME, RETURNING_ENDED, EndedRun, ended_run


@dataclass(frozen=True)
class TaskOutcome:
    """A notification that a task's run has a final outcome, not the outcome itself: the task
    service reads the outcome, the caller and the agent from golem_runs (ADR 0009)."""

    task_id: str
    # A hint for logs; the task service reads the task's run itself.
    run_id: str


@dataclass(frozen=True)
class SucceededRun:
    run_id: str
    agent: str
    # A reported run's target record on its branch, from its report; checked, never trusted.
    record: str | None = None


@dataclass(frozen=True)
class Settlement:
    """What a succeeded run proposed: the outcome detail for its tasks, and its merge request."""

    detail: str
    merge_request: OpenedMergeRequest | None = None


@dataclass(frozen=True)
class Pushed:
    """A vanished run's branch: what survives of a run whose Job is gone."""

    # The report its head commit carries, for a goal run that reported (ADR 0017); a goal
    # run's report is otherwise lost with its Job, and a report is what tells the two apart.
    report: str | None = None


Notify = Callable[[TaskOutcome], Awaitable[bool]]
# Raises when the proposal could not be made.
Propose = Callable[[SucceededRun], Awaitable[Settlement]]
# The run's pushed branch, None when it pushed none; raises when that cannot be known now.
Proposed = Callable[[SucceededRun], Awaitable[Pushed | None]]
# The decision GitLab holds for a pending merge request, None while there is none; raises when
# GitLab cannot say.
Check = Callable[[PendingMergeRequest], Awaitable[Transition | None]]
NotifyProposal = Callable[[str], Awaitable[bool]]


@dataclass(frozen=True)
class Reporter:
    """A reported run's branch (ADR 0017): its record read at the head commit, then deleted."""

    # The record's text, or None when the branch is gone; raises when GitLab cannot say.
    read: Callable[[SucceededRun], Awaitable[str | None]]
    discard: Callable[[SucceededRun], Awaitable[None]]


log = logging.getLogger(__name__)

FINAL_STATUS = {
    JobStatus.SUCCEEDED: "succeeded",
    JobStatus.FAILED: "failed",
    JobStatus.MISSING: "failed",
}


# A run is committed before its Job is created, and creating it may take two API calls of up
# to 35 s each; a Job missing that soon is one still being made, not one that disappeared.
LAUNCH_GRACE_SECONDS = 120

# What a run's own report may say about its outcome, beyond the Job's status. The report comes
# from an untrusted Job, so only these words become label values.
REPORTED_OUTCOMES = frozenset({"idle", "invalid", "failed", "reported"})
REPORTED = "reported"
# The report a task shows; the record itself stays whole in the repository only if proposed.
MAX_REPORT_CHARS = 20_000
MAX_RECORD_PATH = 512


def run_outcome(job: JobStatus, report: str | None = None) -> str:
    """The outcome label of a finished run: the report's word where it has one of ours."""
    if job is JobStatus.MISSING:
        return FINAL_STATUS[job]
    reported = _parse_report(report).get("outcome")
    # A report is written only by a runtime that exited 0; a failed Job claiming it did not.
    if reported == REPORTED and job is not JobStatus.SUCCEEDED:
        return FINAL_STATUS[job]
    return reported if reported in REPORTED_OUTCOMES else FINAL_STATUS[job]


def outcome_detail(run_id: str, job: JobStatus, report: str | None = None) -> str:
    if job is JobStatus.MISSING:
        return f"Run {run_id} failed: its Job disappeared before reporting a result."
    status = FINAL_STATUS[job]
    parsed = _parse_report(report)
    reasons = "; ".join(str(r) for r in parsed.get("reasons") or [])
    match parsed.get("outcome"):
        case "invalid":
            return f"Run {run_id} failed: the change was rejected by validation: {reasons}."
        case "idle":
            return f"Run {run_id} succeeded and proposed no changes: {reasons}."
        case "reported" if status == "succeeded":
            return f"Run {run_id} succeeded and reported what it found; nothing to decide."
        case "failed" if parsed.get("summary"):
            return f"Run {run_id} failed: {parsed['summary']}."
    return f"Run {run_id} {status}."


def report_record(report: str | None) -> str | None:
    """The record a reported run names, if it is a Markdown file inside the repository."""
    record = _parse_report(report).get("record")
    if not isinstance(record, str) or len(record) > MAX_RECORD_PATH:
        return None
    path = PurePosixPath(record)
    if path.is_absolute() or ".." in path.parts or path.suffix != ".md":
        return None
    return record


def _parse_report(report: str | None) -> dict:
    # The report is written by code inside an untrusted Job: anything unreadable is ignored.
    if not report:
        return {}
    try:
        parsed = json.loads(report)
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


async def reconcile_once(
    conn: AsyncConnection,
    launcher: JobLauncher,
    notify: Notify,
    propose: Propose | None = None,
    metrics: ReconcilerMetrics | None = None,
    proposed: Proposed | None = None,
    check: Check | None = None,
    notify_proposal: NotifyProposal | None = None,
    poll_seconds: float = MR_POLL_SECONDS,
    report: Reporter | None = None,
    processes: ProcessPorts | None = None,
    notify_process: NotifyProcess | None = None,
) -> None:
    """Move finished Jobs' runs to their final status, settle what succeeded runs propose,
    deliver pending task outcomes, then follow open merge requests and deliver the changes;
    last, take each process a step on what its stage shows (ADR 0019)."""
    metrics = ReconcilerMetrics() if metrics is None else metrics
    cursor = await conn.execute(
        "SELECT id, agent, created_at < now() - make_interval(secs => %s) FROM runs"
        " WHERE status = 'running' AND kind = 'run'",
        (LAUNCH_GRACE_SECONDS,),
    )
    for run_id, agent, launched in await cursor.fetchall():
        run = SucceededRun(run_id=str(run_id), agent=agent)
        try:
            await _reconcile_run(conn, launcher, run, proposed, metrics, launched=launched)
        except Exception:
            # One Job the API cannot read now must not hold back the others, nor the delivery
            # of outcomes that are already final.
            log.exception("could not reconcile run %s", run_id)
    await _settle_reports(conn, report, metrics)
    await _settle_proposals(conn, propose, metrics)
    await _deliver(conn, notify)
    if check is not None:
        await _check_merge_requests(conn, check, poll_seconds)
    if notify_proposal is not None:
        await _deliver_proposal_states(conn, notify_proposal)
    if processes is not None:
        await advance_processes(conn, processes, launcher)
        # A process that ended, or a stage the platform withdrew, is told in this pass.
        await _deliver(conn, notify)
        if notify_proposal is not None:
            await _deliver_proposal_states(conn, notify_proposal)
    if notify_process is not None:
        await deliver_views(conn, notify_process)
    await _count_pending(conn, metrics)


async def _reconcile_run(
    conn: AsyncConnection,
    launcher: JobLauncher,
    run: SucceededRun,
    proposed: Proposed | None,
    metrics: ReconcilerMetrics,
    *,
    launched: bool,
) -> None:
    job: JobStatus | None = await asyncio.to_thread(launcher.status, run.run_id)
    if job is JobStatus.MISSING and not launched:
        return
    report = None
    if job is JobStatus.MISSING and proposed is not None:
        try:
            pushed = await proposed(run)
        except Exception:
            log.exception("could not tell whether vanished run %s pushed a branch", run.run_id)
            return
        # A finished Job is deleted by its TTL; a reconciler that was down longer finds it
        # gone. The runtime pushes a branch only when it succeeded, so the branch is what
        # survives, and its head commit what is left of the report.
        if pushed is not None:
            job, report = JobStatus.SUCCEEDED, pushed.report
    elif job in FINAL_STATUS and job is not JobStatus.MISSING:
        report = await asyncio.to_thread(launcher.termination_message, run.run_id)
    if job is None or job not in FINAL_STATUS:
        return
    outcome = run_outcome(job, report)
    ended = await _finish(
        conn,
        run.run_id,
        FINAL_STATUS[job],
        outcome_detail(run.run_id, job, report),
        outcome,
        report_record(report) if outcome == REPORTED else None,
    )
    if ended is not None:
        metrics.runs.run_ended(ended.agent, outcome, ended.seconds)


async def _finish(
    conn: AsyncConnection,
    run_id: str,
    status: str,
    detail: str,
    outcome: str,
    record: str | None,
) -> EndedRun | None:
    # The status guard makes the transition happen once even if a cancel or another
    # reconciler got there first; only the one that made it records the outcome.
    cursor = await conn.execute(
        "UPDATE runs SET status = %s, detail = %s, outcome = %s, record = %s"
        " WHERE id = %s AND status = 'running'" + RETURNING_ENDED,
        (status, detail, outcome, record, run_id),
    )
    return await ended_run(cursor)


def idle_detail(run_id: str) -> str:
    return f"Run {run_id} succeeded and proposed no changes."


def settled_detail(run_id: str, reported: str | None, proposed: str) -> str:
    # With no branch there is nothing to propose; the runtime's report says why, if it could.
    if proposed == idle_detail(run_id) and reported and "proposed no changes" in reported:
        return reported
    return proposed


async def _settle_reports(
    conn: AsyncConnection, reporter: Reporter | None, metrics: ReconcilerMetrics
) -> None:
    cursor = await conn.execute(
        "SELECT id, agent, record, report FROM runs"
        " WHERE status = 'succeeded' AND proposal_settled_at IS NULL AND outcome = %s",
        (REPORTED,),
    )
    for run_id, agent, record, report in await cursor.fetchall():
        run = SucceededRun(run_id=str(run_id), agent=agent, record=record)
        try:
            if report is None:
                report = await _read_report(run, reporter)
                # Kept before the branch goes, so a failed delete never loses what was read.
                await conn.execute("UPDATE runs SET report = %s WHERE id = %s", (report, run_id))
            if reporter is not None:
                await reporter.discard(run)
        except Exception:
            log.exception("could not settle the report of run %s", run_id)
            continue
        settled = await conn.execute(
            "UPDATE runs SET proposal_settled_at = now()"
            " WHERE id = %s AND proposal_settled_at IS NULL",
            (run_id,),
        )
        if settled.rowcount:
            metrics.proposal_settled()


async def _read_report(run: SucceededRun, reporter: Reporter | None) -> str:
    text = None
    if reporter is not None and run.record is not None:
        text = await reporter.read(run)
    if text is None:
        return f"The report of run {run.run_id} could not be read: its record or branch is gone."
    return text[:MAX_REPORT_CHARS]


async def _settle_proposals(
    conn: AsyncConnection, propose: Propose | None, metrics: ReconcilerMetrics
) -> None:
    cursor = await conn.execute(
        "SELECT id, agent, detail FROM runs"
        " WHERE status = 'succeeded' AND proposal_settled_at IS NULL"
        " AND outcome IS DISTINCT FROM %s",
        (REPORTED,),
    )
    for run_id, agent, detail in await cursor.fetchall():
        merge_request = None
        if propose is not None:
            try:
                proposed = await propose(SucceededRun(run_id=str(run_id), agent=agent))
            except Exception:
                # Left unsettled, the run is proposed again next pass and its tasks wait for it;
                # other runs must not wait behind it.
                log.exception("could not propose the result of run %s", run_id)
                metrics.merge_request_failed()
                continue
            detail = settled_detail(str(run_id), detail, proposed.detail)
            merge_request = proposed.merge_request
        # A run with a proposal is settled only together with its row (ADR 0015).
        async with conn.transaction():
            if merge_request is not None:
                await record_merge_request(conn, str(run_id), merge_request)
            settled = await conn.execute(
                "UPDATE runs SET detail = %s, proposal_settled_at = now()"
                " WHERE id = %s AND proposal_settled_at IS NULL",
                (detail, run_id),
            )
        if settled.rowcount:
            metrics.proposal_settled()


async def _deliver(conn: AsyncConnection, notify: Notify) -> None:
    cursor = await conn.execute(
        "SELECT t.task_id, r.id FROM run_tasks t JOIN runs r ON r.id = t.run_id"
        " WHERE t.notified_at IS NULL AND " + FINAL_OUTCOME
    )
    for task_id, run_id in await cursor.fetchall():
        if await notify(TaskOutcome(task_id=task_id, run_id=str(run_id))):
            await conn.execute(
                "UPDATE run_tasks SET notified_at = now() WHERE task_id = %s", (task_id,)
            )


async def _check_merge_requests(conn: AsyncConnection, check: Check, poll_seconds: float) -> None:
    for pending in await due_merge_requests(conn, poll_seconds):
        try:
            transition = await check(pending)
        except Exception:
            # Marked checked all the same: one merge request GitLab cannot read must not stay
            # first in line and crowd out the others.
            log.exception("could not read the merge request of proposal %s", pending.proposal_id)
            transition = None
        await record_check(conn, pending.proposal_id, transition)


async def _deliver_proposal_states(conn: AsyncConnection, notify_proposal: NotifyProposal) -> None:
    for proposal_id, state in await undelivered_states(conn):
        if await notify_proposal(proposal_id):
            await mark_delivered(conn, proposal_id, state)


async def _count_pending(conn: AsyncConnection, metrics: ReconcilerMetrics) -> None:
    cursor = await conn.execute(
        "SELECT"
        " (SELECT count(*) FROM run_tasks t JOIN runs r ON r.id = t.run_id"
        "  WHERE t.notified_at IS NULL AND " + FINAL_OUTCOME + "),"
        " (SELECT count(*) FROM runs WHERE status = 'succeeded' AND proposal_settled_at IS NULL)"
    )
    row = await cursor.fetchone()
    assert row is not None
    metrics.pending(outbox=row[0], proposals=row[1])
