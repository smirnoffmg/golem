import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from psycopg import AsyncConnection

from golem.orchestrator.jobs import JobLauncher, JobStatus


@dataclass(frozen=True)
class TaskOutcome:
    task_id: str
    tenant: str
    caller: str
    run_id: str
    status: str
    detail: str


Notify = Callable[[TaskOutcome], Awaitable[bool]]

FINAL_STATUS = {
    JobStatus.SUCCEEDED: "succeeded",
    JobStatus.FAILED: "failed",
    JobStatus.MISSING: "failed",
}


def outcome_detail(run_id: str, job: JobStatus) -> str:
    if job is JobStatus.MISSING:
        return f"Run {run_id} failed: its Job disappeared before reporting a result."
    return f"Run {run_id} {FINAL_STATUS[job]}."


async def reconcile_once(conn: AsyncConnection, launcher: JobLauncher, notify: Notify) -> None:
    """Move finished Jobs' runs to their final status, then deliver pending task outcomes."""
    cursor = await conn.execute("SELECT id FROM runs WHERE status = 'running'")
    for (run_id,) in await cursor.fetchall():
        job = await asyncio.to_thread(launcher.status, str(run_id))
        if job in FINAL_STATUS:
            await _finish(conn, str(run_id), FINAL_STATUS[job], outcome_detail(str(run_id), job))
    await _deliver(conn, notify)


async def _finish(conn: AsyncConnection, run_id: str, status: str, detail: str) -> None:
    # The status guard makes the transition happen once even if a cancel or another
    # reconciler got there first.
    await conn.execute(
        "UPDATE runs SET status = %s, detail = %s WHERE id = %s AND status = 'running'",
        (status, detail, run_id),
    )


async def _deliver(conn: AsyncConnection, notify: Notify) -> None:
    cursor = await conn.execute(
        "SELECT t.task_id, r.agent, r.caller, r.id, r.status, r.detail"
        " FROM run_tasks t JOIN runs r ON r.id = t.run_id"
        " WHERE t.notified_at IS NULL AND r.status IN ('succeeded', 'failed')"
    )
    for task_id, agent, caller, run_id, status, detail in await cursor.fetchall():
        outcome = TaskOutcome(
            task_id=task_id,
            tenant=agent,
            caller=caller,
            run_id=str(run_id),
            status=status,
            detail=detail,
        )
        if await notify(outcome):
            await conn.execute(
                "UPDATE run_tasks SET notified_at = now() WHERE task_id = %s", (task_id,)
            )
