import uuid
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from psycopg import AsyncConnection, AsyncCursor

from golem.orchestrator.admission import Limits, Load, Rejected, RunRequest, admit

SCHEMA = Path(__file__).with_name("schema.sql")

# When a run's outcome is final, over `runs r`: it failed, or it succeeded and its proposal is
# settled, so its tasks learn the merge request (or that there is none) rather than a bare
# "succeeded". The outbox delivers by this rule and the task service reads by it, so a
# notification can only ever lead to an outcome the outbox would deliver itself.
FINAL_OUTCOME = (
    "(r.status = 'failed' OR (r.status = 'succeeded' AND r.proposal_settled_at IS NOT NULL))"
)


@dataclass(frozen=True)
class StartRequest:
    caller: str
    message_id: str
    task_id: str
    agent: str
    estimated_cost: Decimal
    root_run_id: str | None = None


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
            " estimated_cost) VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (
                run_id,
                root_run_id,
                request.caller,
                request.message_id,
                request.task_id,
                request.agent,
                request.estimated_cost,
            ),
        )
        await _map_task(conn, request.task_id, run_id)
    return RunCreated(run_id=run_id, root_run_id=root_run_id)


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
        "SELECT r.id, r.caller, r.agent, r.status, r.detail, " + FINAL_OUTCOME + " AS final"
        " FROM run_tasks t JOIN runs r ON r.id = t.run_id WHERE t.task_id = %s",
        (task_id,),
    )
    row = await cursor.fetchone()
    if row is None:
        return None
    run_id, caller, agent, status, detail, final = row
    return RecordedRun(str(run_id), caller, agent, status, detail, final)


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
        " (SELECT count(*) FROM runs WHERE caller = %(caller)s AND status = 'running'),"
        " (SELECT count(*) FROM runs WHERE root_run_id = %(root)s AND status = 'running'),"
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
