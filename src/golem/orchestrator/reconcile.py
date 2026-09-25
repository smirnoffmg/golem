import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from psycopg import AsyncConnection

from golem.orchestrator.jobs import JobLauncher, JobStatus
from golem.orchestrator.runs import FINAL_OUTCOME


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

log = logging.getLogger(__name__)

FINAL_STATUS = {
    JobStatus.SUCCEEDED: "succeeded",
    JobStatus.FAILED: "failed",
    JobStatus.MISSING: "failed",
}


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
    conn: AsyncConnection, launcher: JobLauncher, notify: Notify, propose: Propose | None = None
) -> None:
    """Move finished Jobs' runs to their final status, settle what succeeded runs propose,
    then deliver pending task outcomes."""
    cursor = await conn.execute("SELECT id FROM runs WHERE status = 'running'")
    for (run_id,) in await cursor.fetchall():
        job = await asyncio.to_thread(launcher.status, str(run_id))
        if job in FINAL_STATUS:
            report = (
                None
                if job is JobStatus.MISSING
                else await asyncio.to_thread(launcher.termination_message, str(run_id))
            )
            await _finish(
                conn, str(run_id), FINAL_STATUS[job], outcome_detail(str(run_id), job, report)
            )
    await _settle_proposals(conn, propose)
    await _deliver(conn, notify)


async def _finish(conn: AsyncConnection, run_id: str, status: str, detail: str) -> None:
    # The status guard makes the transition happen once even if a cancel or another
    # reconciler got there first.
    await conn.execute(
        "UPDATE runs SET status = %s, detail = %s WHERE id = %s AND status = 'running'",
        (status, detail, run_id),
    )


def idle_detail(run_id: str) -> str:
    return f"Run {run_id} succeeded and proposed no changes."


def settled_detail(run_id: str, reported: str | None, proposed: str) -> str:
    # With no branch there is nothing to propose; the runtime's report says why, if it could.
    if proposed == idle_detail(run_id) and reported and "proposed no changes" in reported:
        return reported
    return proposed


async def _settle_proposals(conn: AsyncConnection, propose: Propose | None) -> None:
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
                continue
            detail = settled_detail(str(run_id), detail, proposed)
        await conn.execute(
            "UPDATE runs SET detail = %s, proposal_settled_at = now()"
            " WHERE id = %s AND proposal_settled_at IS NULL",
            (detail, run_id),
        )


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
