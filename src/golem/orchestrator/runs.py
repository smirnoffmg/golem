import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from psycopg import AsyncConnection, AsyncCursor
from psycopg.types.json import Jsonb

from golem.orchestrator.admission import Limits, Load, Rejected, RunRequest, admit
from golem.tasks.ports import ProposalView

SCHEMA = Path(__file__).with_name("schema.sql")

# When a run's outcome is final, over `runs r`: it failed, or it succeeded and its proposal is
# settled, so its tasks learn the merge request (or that there is none) rather than a bare
# "succeeded". The outbox delivers by this rule and the task service reads by it, so a
# notification can only ever lead to an outcome the outbox would deliver itself.
# A run the platform withdrew (a stage of a canceled process, ADR 0019) is final as well: its task
# hears it was canceled, since nobody canceled that task through A2A.
FINAL_OUTCOME = (
    "(r.status = 'failed' OR (r.status = 'succeeded' AND r.proposal_settled_at IS NOT NULL)"
    " OR r.outcome = 'withdrawn')"
)
WITHDRAWN = "withdrawn"
PROCESS_KIND = "process"


@dataclass(frozen=True)
class StartRequest:
    caller: str
    message_id: str
    task_id: str
    agent: str
    estimated_cost: Decimal
    root_run_id: str | None = None
    # What the agent's runs propose, from its pinned catalog (ADR 0015).
    proposal_kind: str = "merge_request"


@dataclass(frozen=True)
class RunCreated:
    run_id: str
    root_run_id: str


@dataclass(frozen=True)
class RunReused:
    run_id: str
    root_run_id: str
    status: str = "running"


@dataclass(frozen=True)
class EndedRun:
    """A run that has just left 'running', for the run metrics."""

    run_id: str
    agent: str
    # From recording the run to now.
    seconds: float


# Appended to an UPDATE of `runs` that moves a run out of 'running'.
RETURNING_ENDED = (
    " RETURNING runs.id, runs.agent, extract(epoch FROM now() - runs.created_at)::float8"
)


async def ended_run(cursor: AsyncCursor) -> EndedRun | None:
    row = await cursor.fetchone()
    return None if row is None else EndedRun(str(row[0]), row[1], row[2])


@dataclass(frozen=True)
class RecordedRun:
    run_id: str
    caller: str
    agent: str
    status: str
    detail: str | None
    final: bool
    proposal: ProposalView | None = None
    # A goal run that found nothing to propose: the report its task shows (ADR 0017).
    report: str | None = None
    withdrawn: bool = False
    # What a proposal of a kind the platform applies proposes: the task's `proposal` artifact.
    proposal_payload: dict[str, Any] | None = None


async def apply_schema(conn: AsyncConnection) -> None:
    await conn.execute(SCHEMA.read_text())


def lock_keys(request: StartRequest) -> list[str]:
    keys = [f"caller:{request.caller}"]
    if request.root_run_id is not None:
        keys.append(f"root:{request.root_run_id}")
    # A fixed order across transactions keeps two lockers from deadlocking each other.
    return sorted(keys)


async def start_run(
    conn: AsyncConnection, request: StartRequest, limits: Limits
) -> RunCreated | RunReused | Rejected:
    """Start a run at most once per (caller, message id), within admission limits.

    Transaction-scoped advisory locks on the caller and the root chain serialize the
    check-then-insert, so neither a retry storm nor a burst of distinct requests can slip
    past the duplicate check or the limits.
    """
    run_id = str(uuid.uuid4())
    root_run_id = request.root_run_id or run_id
    async with conn.transaction():
        for key in lock_keys(request):
            await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (key,))

        existing = await _find_run(conn, request.caller, request.message_id)
        if existing is not None:
            run_id_found, root_found, status = existing
            await _map_task(conn, request.task_id, run_id_found)
            return RunReused(run_id=run_id_found, root_run_id=root_found, status=status)

        decision = admit(
            RunRequest(request.caller, root_run_id, request.estimated_cost),
            await _load(conn, request.caller, root_run_id),
            limits,
        )
        if isinstance(decision, Rejected):
            return decision

        await conn.execute(
            "INSERT INTO runs (id, root_run_id, caller, message_id, task_id, agent,"
            " estimated_cost, proposal_kind) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (
                run_id,
                root_run_id,
                request.caller,
                request.message_id,
                request.task_id,
                request.agent,
                request.estimated_cost,
                request.proposal_kind,
            ),
        )
        await _map_task(conn, request.task_id, run_id)
    return RunCreated(run_id=run_id, root_run_id=root_run_id)


async def start_process(
    conn: AsyncConnection, request: StartRequest, definition: Mapping[str, Any], person_input: str
) -> RunCreated | RunReused:
    """Record a process run at most once per (caller, message id), with its first stage to
    start (ADR 0019). It launches no Job and takes no admission slot: its stages do, as runs
    of the caller under it as their root."""
    run_id = str(uuid.uuid4())
    async with conn.transaction():
        await conn.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (f"caller:{request.caller}",)
        )
        existing = await _find_run(conn, request.caller, request.message_id)
        if existing is not None:
            run_id_found, root_found, status = existing
            await _map_task(conn, request.task_id, run_id_found)
            return RunReused(run_id=run_id_found, root_run_id=root_found, status=status)
        await conn.execute(
            "INSERT INTO runs (id, root_run_id, caller, message_id, task_id, agent,"
            " estimated_cost, kind) VALUES (%s, %s, %s, %s, %s, %s, 0, %s)",
            (
                run_id,
                run_id,
                request.caller,
                request.message_id,
                request.task_id,
                request.agent,
                PROCESS_KIND,
            ),
        )
        await conn.execute(
            "INSERT INTO process_stages (process_run_id, definition, input) VALUES (%s, %s, %s)",
            (run_id, Jsonb(dict(definition)), person_input),
        )
        await _map_task(conn, request.task_id, run_id)
    return RunCreated(run_id=run_id, root_run_id=run_id)


async def cancel_run_of_task(conn: AsyncConnection, task_id: str) -> EndedRun | None:
    cursor = await conn.execute(
        "UPDATE runs SET status = 'canceled' FROM run_tasks"
        " WHERE run_tasks.run_id = runs.id AND run_tasks.task_id = %s"
        " AND runs.status = 'running'" + RETURNING_ENDED,
        (task_id,),
    )
    return await ended_run(cursor)


async def run_status(conn: AsyncConnection, run_id: str) -> str | None:
    try:
        run_uuid = uuid.UUID(run_id)
    except ValueError:
        return None
    cursor = await conn.execute("SELECT status FROM runs WHERE id = %s", (run_uuid,))
    row = await cursor.fetchone()
    return None if row is None else row[0]


async def run_of_task(conn: AsyncConnection, task_id: str) -> RecordedRun | None:
    cursor = await conn.execute(
        "SELECT r.id, r.caller, r.agent, r.status, r.detail, " + FINAL_OUTCOME + " AS final,"
        " p.id, p.kind, p.state, p.url, CASE WHEN r.outcome = 'reported' THEN r.report END,"
        " r.outcome IS NOT DISTINCT FROM %s,"
        " CASE WHEN p.kind <> 'merge_request' THEN p.payload END"
        " FROM run_tasks t JOIN runs r ON r.id = t.run_id"
        " LEFT JOIN proposals p ON p.run_id = r.id WHERE t.task_id = %s",
        (WITHDRAWN, task_id),
    )
    row = await cursor.fetchone()
    if row is None:
        return None
    (run_id, caller, agent, status, detail, final) = row[:6]
    (proposal_id, kind, state, url, report, withdrawn, payload) = row[6:]
    proposal = (
        None if proposal_id is None else ProposalView(str(proposal_id), kind, state, url or "")
    )
    return RecordedRun(
        str(run_id), caller, agent, status, detail, final, proposal, report, withdrawn, payload
    )


async def agents_of_tasks(conn: AsyncConnection, task_ids: tuple[str, ...]) -> dict[str, str]:
    cursor = await conn.execute(
        "SELECT t.task_id, r.agent FROM run_tasks t JOIN runs r ON r.id = t.run_id"
        " WHERE t.task_id = ANY(%s)",
        (list(task_ids),),
    )
    return dict(await cursor.fetchall())


async def fail_run(conn: AsyncConnection, run_id: str) -> EndedRun | None:
    cursor = await conn.execute(
        "UPDATE runs SET status = 'failed' WHERE id = %s AND status = 'running'" + RETURNING_ENDED,
        (run_id,),
    )
    return await ended_run(cursor)


async def _map_task(conn: AsyncConnection, task_id: str, run_id: str) -> None:
    await conn.execute(
        "INSERT INTO run_tasks (task_id, run_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
        (task_id, run_id),
    )


async def _find_run(
    conn: AsyncConnection, caller: str, message_id: str
) -> tuple[str, str, str] | None:
    cursor = await conn.execute(
        "SELECT id, root_run_id, status FROM runs WHERE caller = %s AND message_id = %s",
        (caller, message_id),
    )
    row = await cursor.fetchone()
    return None if row is None else (str(row[0]), str(row[1]), row[2])


async def _load(conn: AsyncConnection, caller: str, root_run_id: str) -> Load:
    cursor = await conn.execute(
        "SELECT"
        # A process run is no Job: only runs count against the limits (ADR 0019).
        " (SELECT count(*) FROM runs WHERE caller = %(caller)s AND status = 'running'"
        "  AND kind = 'run'),"
        " (SELECT count(*) FROM runs WHERE root_run_id = %(root)s AND status = 'running'"
        "  AND kind = 'run'),"
        " (SELECT coalesce(sum(estimated_cost), 0) FROM runs WHERE root_run_id = %(root)s)",
        {"caller": caller, "root": root_run_id},
    )
    row = await cursor.fetchone()
    assert row is not None
    running_by_caller, running_by_root, spent_by_root = row
    return Load(
        running_by_caller={caller: running_by_caller},
        running_by_root={root_run_id: running_by_root},
        spent_by_root={root_run_id: spent_by_root},
    )
