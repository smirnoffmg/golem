import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from psycopg import AsyncConnection

from golem.metrics import ReconcilerMetrics
from golem.orchestrator.jobs import JobLauncher, JobStatus
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


Notify = Callable[[TaskOutcome], Awaitable[bool]]
# Returns the outcome detail for the run's tasks; raises when the proposal could not be made.
Propose = Callable[[SucceededRun], Awaitable[str]]
# Whether the run pushed its proposal branch; raises when that cannot be known now.
Proposed = Callable[[SucceededRun], Awaitable[bool]]

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
REPORTED_OUTCOMES = frozenset({"idle", "invalid", "failed"})


def run_outcome(job: JobStatus, report: str | None = None) -> str:
    """The outcome label of a finished run: the report's word where it has one of ours."""
    if job is JobStatus.MISSING:
        return FINAL_STATUS[job]
    reported = _parse_report(report).get("outcome")
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
        case "failed" if parsed.get("summary"):
            return f"Run {run_id} failed: {parsed['summary']}."
    return f"Run {run_id} {status}."


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
) -> None:
    """Move finished Jobs' runs to their final status, settle what succeeded runs propose,
    then deliver pending task outcomes."""
    metrics = ReconcilerMetrics() if metrics is None else metrics
    cursor = await conn.execute(
        "SELECT id, agent, created_at < now() - make_interval(secs => %s) FROM runs"
        " WHERE status = 'running'",
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
    await _settle_proposals(conn, propose, metrics)
    await _deliver(conn, notify)
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
    if job is JobStatus.MISSING and proposed is not None:
        job = await _vanished(run, proposed)
    if job is None or job not in FINAL_STATUS:
        return
    report = (
        None
        if job is JobStatus.MISSING
        else await asyncio.to_thread(launcher.termination_message, run.run_id)
    )
    ended = await _finish(
        conn, run.run_id, FINAL_STATUS[job], outcome_detail(run.run_id, job, report)
    )
    if ended is not None:
        metrics.runs.run_ended(ended.agent, run_outcome(job, report), ended.seconds)


async def _vanished(run: SucceededRun, proposed: Proposed) -> JobStatus | None:
    # A finished Job is deleted by its TTL; a reconciler that was down longer finds it gone.
    # The runtime pushes a branch only when it succeeded, so the branch is what survives.
    try:
        return JobStatus.SUCCEEDED if await proposed(run) else JobStatus.MISSING
    except Exception:
        log.exception("could not tell whether vanished run %s pushed a branch", run.run_id)
        return None


async def _finish(conn: AsyncConnection, run_id: str, status: str, detail: str) -> EndedRun | None:
    # The status guard makes the transition happen once even if a cancel or another
    # reconciler got there first; only the one that made it records the outcome.
    cursor = await conn.execute(
        "UPDATE runs SET status = %s, detail = %s WHERE id = %s AND status = 'running'"
        + RETURNING_ENDED,
        (status, detail, run_id),
    )
    return await ended_run(cursor)


def idle_detail(run_id: str) -> str:
    return f"Run {run_id} succeeded and proposed no changes."


def settled_detail(run_id: str, reported: str | None, proposed: str) -> str:
    # With no branch there is nothing to propose; the runtime's report says why, if it could.
    if proposed == idle_detail(run_id) and reported and "proposed no changes" in reported:
        return reported
    return proposed


async def _settle_proposals(
    conn: AsyncConnection, propose: Propose | None, metrics: ReconcilerMetrics
) -> None:
    cursor = await conn.execute(
        "SELECT id, agent, detail FROM runs"
        " WHERE status = 'succeeded' AND proposal_settled_at IS NULL"
    )
    for run_id, agent, detail in await cursor.fetchall():
        if propose is not None:
            try:
                proposed = await propose(SucceededRun(run_id=str(run_id), agent=agent))
            except Exception:
                # Left unsettled, the run is proposed again next pass and its tasks wait for it;
                # other runs must not wait behind it.
                log.exception("could not propose the result of run %s", run_id)
                metrics.merge_request_failed()
                continue
            detail = settled_detail(str(run_id), detail, proposed)
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
