"""Processes in golem_runs (ADR 0019): the reconciler's pass over them, and what the task
service reads and changes of them.

Every change of a ``process_stages`` row is a compare-and-set on where the row stood, so two
reconcilers, or a reconciler and an owner's resolution, move a process once. A stage's message
id makes a repeated start the same start.
"""

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from typing import Any

from psycopg import AsyncConnection
from psycopg.types.json import Jsonb

from golem.catalog import ProcessCatalog
from golem.orchestrator.jobs import JobLauncher
from golem.orchestrator.processes import (
    Advance,
    Complete,
    Decided,
    Ended,
    Fail,
    NeedsReason,
    Position,
    Rejection,
    Rerun,
    Seen,
    StaleRerun,
    Step,
    Waiting,
    next_step,
    stage_message_id,
    stage_target,
    stage_text,
)
from golem.orchestrator.runs import WITHDRAWN
from golem.resolution import MAX_REASON_CHARS, RERUN
from golem.tasks.ports import NOT_FOUND, NOT_WAITING, RESOLVED, ProcessRecord

log = logging.getLogger(__name__)

LIVE = ("running", "needs_reason")
ENDED_BY_OWNER = "ended_by_owner"
PROCESS_CANCELED = "process_canceled"
INVALID_GOAL = "invalid_goal"
MERGE_REQUEST = "merge_request"
REPORTED = "reported"
# A proposal in these states still waits for a person or for its apply.
UNDECIDED = ("pending", "accepted", "failed")
# What a canceled process withdraws: an accepted proposal is not among them, its apply may be
# writing already, and it ends as that apply says (ADR 0019).
WITHDRAWN_STATES = ("pending", "failed")


@dataclass(frozen=True)
class StageStart:
    process_run_id: str
    process: str
    owner: str
    agent: str
    message_id: str
    text: str
    target: str


@dataclass(frozen=True)
class StageRefused:
    """The edge or admission refused the stage: the process cannot go on."""

    reason: str


@dataclass(frozen=True)
class ProcessPorts:
    # The stage task's id, or a refusal; raises when the edge cannot say now.
    start: Callable[[StageStart], Awaitable[str | StageRefused]]
    # Why a person closed a stage's merge request, None if they wrote nothing; raises on outage.
    closing_reason: Callable[[str, int], Awaitable[str | None]]
    close_merge_request: Callable[[str, int], Awaitable[None]]


NotifyProcess = Callable[[str], Awaitable[bool]]


@dataclass(frozen=True)
class Row:
    process_run_id: str
    owner: str
    process: ProcessCatalog
    person_input: str
    position: Position
    run_id: str | None
    task_id: str | None
    rejection: Rejection | None
    state: str
    status: str

    @property
    def message_id(self) -> str:
        p = self.position
        return stage_message_id(self.process_run_id, p.index, p.attempt, p.stale_reruns)

    @property
    def agent(self) -> str:
        return self.process.stages[self.position.index].agent


@dataclass(frozen=True)
class StageRun:
    status: str
    outcome: str | None
    detail: str | None
    settled: bool
    agent: str
    proposal_id: str | None
    kind: str | None
    proposal_state: str | None
    decided_by: str | None
    proposal_detail: str | None
    iid: int | None


ROW_COLUMNS = (
    "s.process_run_id, r.caller, s.definition, s.input, s.stage, s.attempt, s.stale_reruns,"
    " s.run_id, s.task_id, s.reason, s.decided_by, s.state, r.status"
)


def row_of(values: tuple[Any, ...]) -> Row:
    (process_run_id, owner, definition, person_input, stage, attempt, stale) = values[:7]
    (run_id, task_id, reason, decided_by, state, status) = values[7:]
    process = ProcessCatalog.model_validate(definition)
    return Row(
        process_run_id=str(process_run_id),
        owner=owner,
        process=process,
        person_input=person_input,
        position=Position(
            index=stage,
            count=len(process.stages),
            attempt=attempt,
            stale_reruns=stale,
            return_limit=process.return_limit,
        ),
        run_id=None if run_id is None else str(run_id),
        task_id=task_id,
        rejection=None if reason is None else Rejection(reason, decided_by),
        state=state,
        status=status,
    )


async def advance_processes(
    conn: AsyncConnection, ports: ProcessPorts, launcher: JobLauncher
) -> None:
    """One step for every live process, then the view each one's task should show."""
    cursor = await conn.execute(
        f"SELECT {ROW_COLUMNS} FROM process_stages s JOIN runs r ON r.id = s.process_run_id"
        " WHERE s.state = ANY(%s)",
        (list(LIVE),),
    )
    for values in await cursor.fetchall():
        row = row_of(values)
        try:
            if row.status == "canceled":
                await _withdraw(conn, row, ports, launcher)
            elif row.status == "running" and row.state == "running":
                await _advance(conn, row, ports)
        except Exception:
            # One process the edge or GitLab cannot serve now must not hold back the others.
            log.exception("could not advance process %s", row.process_run_id)
        await refresh_view(conn, row.process_run_id)


async def _advance(conn: AsyncConnection, row: Row, ports: ProcessPorts) -> None:
    if row.run_id is None:
        row = await _started(conn, row, ports)
        if row.run_id is None:
            return
    stage_run = await _stage_run(conn, row.run_id)
    if stage_run is None:
        return
    await _take(conn, row, next_step(row.position, await _seen(stage_run, ports)))


async def _started(conn: AsyncConnection, row: Row, ports: ProcessPorts) -> Row:
    """The row with its stage's run once the stage started, starting it if it has not."""
    found = await _run_of_message(conn, row.owner, row.message_id)
    if found is None and row.task_id is None:
        position = row.position
        try:
            text = stage_text(
                row.process, row.process_run_id, position, row.person_input, row.rejection
            )
        except ValueError:
            # Checked at the process's start; a goal that still cannot be written would
            # otherwise be tried on every pass while the process shows it is running.
            await _take(conn, row, Fail(INVALID_GOAL))
            return row
        start = StageStart(
            process_run_id=row.process_run_id,
            process=row.process.name,
            owner=row.owner,
            agent=row.agent,
            message_id=row.message_id,
            text=text,
            target=stage_target(row.process_run_id, row.process.stages[position.index].name),
        )
        started = await ports.start(start)
        if isinstance(started, StageRefused):
            await _take(conn, row, Fail(started.reason))
            return row
        await conn.execute(
            "UPDATE process_stages SET task_id = %s, updated_at = now()"
            " WHERE process_run_id = %s AND task_id IS NULL",
            (started, row.process_run_id),
        )
        found = await _run_of_message(conn, row.owner, row.message_id)
    if found is None:
        return row
    run_id, task_id = found
    await conn.execute(
        "UPDATE process_stages SET run_id = %s, task_id = coalesce(task_id, %s),"
        " updated_at = now() WHERE process_run_id = %s AND run_id IS NULL",
        (run_id, task_id, row.process_run_id),
    )
    return replace(row, run_id=run_id, task_id=row.task_id or task_id)


async def _run_of_message(
    conn: AsyncConnection, owner: str, message_id: str
) -> tuple[str, str] | None:
    # The stage run is the owner's: the edge forwards a delegated call as its subject.
    cursor = await conn.execute(
        "SELECT id, task_id FROM runs WHERE caller = %s AND message_id = %s AND kind = 'run'",
        (owner, message_id),
    )
    found = await cursor.fetchone()
    return None if found is None else (str(found[0]), found[1])


async def _stage_run(conn: AsyncConnection, run_id: str) -> StageRun | None:
    cursor = await conn.execute(
        "SELECT r.status, r.outcome, r.detail, r.proposal_settled_at IS NOT NULL, r.agent,"
        " p.id, p.kind, p.state, p.decided_by, coalesce(p.reason, p.detail),"
        " (p.payload->>'iid')::bigint"
        " FROM runs r LEFT JOIN proposals p ON p.run_id = r.id WHERE r.id = %s",
        (run_id,),
    )
    found = await cursor.fetchone()
    if found is None:
        return None
    status, outcome, detail, settled, agent, proposal_id, kind, state, by, why, iid = found
    return StageRun(
        status=status,
        outcome=outcome,
        detail=detail,
        settled=settled,
        agent=agent,
        proposal_id=None if proposal_id is None else str(proposal_id),
        kind=kind,
        proposal_state=state,
        decided_by=by,
        proposal_detail=why,
        iid=iid,
    )


async def _seen(run: StageRun, ports: ProcessPorts) -> Seen:
    if run.status == "running" or (run.status == "succeeded" and not run.settled):
        return Waiting()
    if run.status == "canceled":
        return Ended("stage_canceled")
    if run.status == "failed":
        return Ended(run.outcome or "failed")
    if run.outcome == "reported":
        return Ended("reported")
    if run.proposal_state is None:
        return Ended("no_proposal")
    if run.proposal_state in UNDECIDED:
        return Waiting()
    if run.proposal_state != "rejected":
        return Decided(run.proposal_state)
    reason = run.proposal_detail
    if run.kind == MERGE_REQUEST and run.iid is not None:
        # Decided in GitLab, where a close carries no reason: the closer's comment is it.
        reason = await ports.closing_reason(run.agent, run.iid)
    return Decided("rejected", None if not reason else Rejection(reason, run.decided_by))


async def _take(conn: AsyncConnection, row: Row, step: Step) -> None:
    p = row.position
    where = (
        " WHERE process_run_id = %(id)s AND stage = %(stage)s AND attempt = %(attempt)s"
        " AND stale_reruns = %(stale)s AND state = 'running'"
    )
    at = {"id": row.process_run_id, "stage": p.index, "attempt": p.attempt, "stale": p.stale_reruns}
    fresh = (
        " run_id = NULL, task_id = NULL, updated_at = now(),"
        " reason = %(reason)s, decided_by = %(by)s"
    )
    match step:
        case Advance(index=index):
            await conn.execute(
                "UPDATE process_stages SET stage = %(next)s, attempt = 0, stale_reruns = 0,"
                + fresh
                + where,
                {**at, "next": index, "reason": None, "by": None},
            )
        case Rerun(attempt=attempt, rejection=rejection):
            await conn.execute(
                "UPDATE process_stages SET attempt = %(next)s," + fresh + where,
                {
                    **at,
                    "next": attempt,
                    "reason": rejection.reason[:MAX_REASON_CHARS],
                    "by": rejection.decided_by,
                },
            )
        case StaleRerun():
            await conn.execute(
                "UPDATE process_stages SET stale_reruns = stale_reruns + 1," + fresh + where,
                {**at, "reason": None, "by": None},
            )
        case NeedsReason():
            await conn.execute(
                "UPDATE process_stages SET state = 'needs_reason', updated_at = now()" + where,
                at,
            )
        case Complete():
            detail = f"Process {row.process.name} completed: all {p.count} stages were applied."
            await _end(conn, row, where, at, "completed", None, "succeeded", detail)
        case Fail(reason=reason):
            stage = row.process.stages[p.index].name
            detail = f"Process {row.process.name} failed at stage {stage}: {reason}."
            await _end(conn, row, where, at, "failed", reason, "failed", detail)


async def _end(
    conn: AsyncConnection,
    row: Row,
    where: str,
    at: dict[str, Any],
    state: str,
    reason: str | None,
    status: str,
    detail: str,
) -> None:
    async with conn.transaction():
        moved = await conn.execute(
            "UPDATE process_stages SET state = %(state)s, detail = %(reason)s,"
            " updated_at = now()" + where,
            {**at, "state": state, "reason": reason},
        )
        if moved.rowcount:
            await end_process_run(conn, row.process_run_id, status, detail)


async def end_process_run(
    conn: AsyncConnection, process_run_id: str, status: str, detail: str
) -> None:
    # Settled at once: the process's task hears its outcome through the outbox like any run's.
    await conn.execute(
        "UPDATE runs SET status = %s, detail = %s, proposal_settled_at = now()"
        " WHERE id = %s AND status = 'running'",
        (status, detail, process_run_id),
    )


async def _withdraw(
    conn: AsyncConnection, row: Row, ports: ProcessPorts, launcher: JobLauncher
) -> None:
    """A canceled process: its running stage run is canceled, a result not yet proposed is
    settled with nothing proposed, and a proposal waiting for a person is rejected."""
    run_id = row.run_id
    if run_id is None:
        # Started, but not yet linked: the run is still the stage's, found by its message.
        found = await _run_of_message(conn, row.owner, row.message_id)
        run_id = None if found is None else found[0]
    stage_run = None if run_id is None else await _stage_run(conn, run_id)
    if stage_run is not None and run_id is not None:
        if stage_run.status == "running":
            cursor = await conn.execute(
                "UPDATE runs SET status = 'canceled', outcome = %s, detail = %s"
                " WHERE id = %s AND status = 'running'",
                (WITHDRAWN, f"Canceled with process {row.process.name}.", run_id),
            )
            if cursor.rowcount:
                await asyncio.to_thread(launcher.delete, run_id)
        elif (
            stage_run.status == "succeeded"
            and not stage_run.settled
            and stage_run.outcome != REPORTED
        ):
            # Settled here, so no later pass opens a merge request nobody would ever close.
            await conn.execute(
                "UPDATE runs SET proposal_settled_at = now(), detail = %s"
                " WHERE id = %s AND proposal_settled_at IS NULL",
                (
                    f"Run {run_id} succeeded, but process {row.process.name} was canceled;"
                    " nothing was proposed.",
                    run_id,
                ),
            )
        elif stage_run.proposal_id is not None and stage_run.proposal_state in WITHDRAWN_STATES:
            if stage_run.kind == MERGE_REQUEST and stage_run.iid is not None:
                await ports.close_merge_request(stage_run.agent, stage_run.iid)
            await conn.execute(
                "UPDATE proposals SET state = 'rejected', decided_by = %s, decided_at = now(),"
                " detail = %s WHERE id = %s AND state = ANY(%s)",
                (row.owner, PROCESS_CANCELED, stage_run.proposal_id, list(WITHDRAWN_STATES)),
            )
    await conn.execute(
        "UPDATE process_stages SET state = 'canceled', updated_at = now()"
        " WHERE process_run_id = %s AND state = ANY(%s)",
        (row.process_run_id, list(LIVE)),
    )


async def refresh_view(conn: AsyncConnection, process_run_id: str) -> None:
    cursor = await conn.execute(
        f"SELECT {ROW_COLUMNS}, s.detail, p.id, p.kind, p.state, p.url"
        " FROM process_stages s JOIN runs r ON r.id = s.process_run_id"
        " LEFT JOIN proposals p ON p.run_id = s.run_id WHERE s.process_run_id = %s",
        (process_run_id,),
    )
    found = await cursor.fetchone()
    if found is None:
        return
    row = row_of(found[:13])
    detail, proposal_id, kind, state, url = found[13:]
    proposal = (
        None
        if proposal_id is None
        else {"id": str(proposal_id), "kind": kind, "state": state, "url": url or ""}
    )
    p = row.position
    view: dict[str, Any] = {
        "state": row.state,
        "stage": row.process.stages[p.index].name,
        "index": p.index,
        "count": p.count,
        "attempt": p.attempt,
        "maxAttempts": p.max_attempts,
        "staleReruns": p.stale_reruns,
        "stageTaskId": row.task_id,
        "proposal": proposal,
    }
    if row.state == "failed":
        view["reason"] = detail
    await conn.execute(
        "UPDATE process_stages SET view = %s WHERE process_run_id = %s"
        " AND view IS DISTINCT FROM %s",
        (Jsonb(view), process_run_id, Jsonb(view)),
    )


async def deliver_views(conn: AsyncConnection, notify: NotifyProcess) -> None:
    cursor = await conn.execute(
        "SELECT process_run_id, view FROM process_stages WHERE view IS DISTINCT FROM notified_view"
    )
    for process_run_id, view in await cursor.fetchall():
        if await notify(str(process_run_id)):
            await conn.execute(
                "UPDATE process_stages SET notified_view = %s WHERE process_run_id = %s",
                (Jsonb(view), process_run_id),
            )


async def process_record(conn: AsyncConnection, process_run_id: str) -> ProcessRecord | None:
    try:
        run_uuid = uuid.UUID(process_run_id)
    except ValueError:
        return None
    cursor = await conn.execute(
        "SELECT s.view, r.caller, r.agent,"
        " array(SELECT t.task_id FROM run_tasks t WHERE t.run_id = r.id ORDER BY t.task_id)"
        " FROM process_stages s JOIN runs r ON r.id = s.process_run_id"
        " WHERE s.process_run_id = %s AND s.view IS NOT NULL",
        (run_uuid,),
    )
    found = await cursor.fetchone()
    if found is None:
        return None
    view, caller, agent, task_ids = found
    return ProcessRecord(view=view, caller=caller, agent=agent, task_ids=tuple(task_ids))


async def resolve_process(
    conn: AsyncConnection, task_id: str, caller: str, action: str, reason: str | None
) -> str:
    """The owner's answer to a process waiting for a reason: rerun the stage, as a rejection
    with that reason would, or end the process."""
    cursor = await conn.execute(
        "SELECT s.process_run_id, s.definition, s.stage FROM run_tasks t"
        " JOIN runs r ON r.id = t.run_id JOIN process_stages s ON s.process_run_id = r.id"
        " WHERE t.task_id = %s AND r.caller = %s AND r.kind = 'process'",
        (task_id, caller),
    )
    found = await cursor.fetchone()
    if found is None:
        return NOT_FOUND
    process_run_id, definition, stage = found
    waiting = " WHERE process_run_id = %s AND state = 'needs_reason'"
    async with conn.transaction():
        if action == RERUN:
            moved = await conn.execute(
                "UPDATE process_stages SET state = 'running', attempt = attempt + 1,"
                " run_id = NULL, task_id = NULL, reason = %s, decided_by = %s,"
                " updated_at = now()" + waiting,
                ((reason or "")[:MAX_REASON_CHARS], caller, process_run_id),
            )
        else:
            moved = await conn.execute(
                "UPDATE process_stages SET state = 'failed', detail = %s, updated_at = now()"
                + waiting,
                (ENDED_BY_OWNER, process_run_id),
            )
            if moved.rowcount:
                process = ProcessCatalog.model_validate(definition)
                detail = (
                    f"Process {process.name} failed at stage {process.stages[stage].name}:"
                    f" {ENDED_BY_OWNER}."
                )
                await end_process_run(conn, str(process_run_id), "failed", detail)
    if not moved.rowcount:
        return NOT_WAITING
    await refresh_view(conn, str(process_run_id))
    return RESOLVED
