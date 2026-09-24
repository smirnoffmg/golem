import asyncio
from decimal import Decimal

import psycopg
import pytest

from golem.orchestrator.admission import Limits, Rejected, RejectReason
from golem.orchestrator.runs import RunCreated, RunReused, StartRequest, start_run

LIMITS = Limits(max_runs_per_caller=3, max_runs_per_root=3, budget_per_root=Decimal("10"))


def request(message_id: str = "m-1", **overrides: object) -> StartRequest:
    fields: dict[str, object] = {
        "caller": "user:alice",
        "message_id": message_id,
        "task_id": f"task-{message_id}",
        "agent": "discovery",
        "estimated_cost": Decimal("1"),
        "root_run_id": None,
    }
    fields.update(overrides)
    return StartRequest(**fields)  # type: ignore[arg-type]


async def start(dsn: str, req: StartRequest, limits: Limits = LIMITS):
    async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as conn:
        return await start_run(conn, req, limits)


async def count_runs(dsn: str) -> int:
    async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as conn:
        row = await (await conn.execute("SELECT count(*) FROM runs")).fetchone()
        assert row is not None
        return row[0]


async def test_first_start_creates_a_run(runs_db: str) -> None:
    outcome = await start(runs_db, request())

    assert isinstance(outcome, RunCreated)
    assert await count_runs(runs_db) == 1


async def test_retry_with_the_same_message_id_returns_the_same_run(runs_db: str) -> None:
    first = await start(runs_db, request("m-1"))
    retry = await start(runs_db, request("m-1", task_id="task-retry"))

    assert isinstance(first, RunCreated)
    assert retry == RunReused(run_id=first.run_id)
    assert await count_runs(runs_db) == 1


async def test_same_message_id_from_another_caller_is_a_different_run(runs_db: str) -> None:
    await start(runs_db, request("m-1"))
    other = await start(runs_db, request("m-1", caller="user:bob"))

    assert isinstance(other, RunCreated)
    assert await count_runs(runs_db) == 2


async def test_concurrent_retries_create_exactly_one_run(runs_db: str) -> None:
    outcomes = await asyncio.gather(*(start(runs_db, request("m-1")) for _ in range(8)))

    created = [o for o in outcomes if isinstance(o, RunCreated)]
    reused = [o for o in outcomes if isinstance(o, RunReused)]
    assert len(created) == 1
    assert {o.run_id for o in reused} == {created[0].run_id}
    assert await count_runs(runs_db) == 1


async def test_concurrent_distinct_requests_respect_the_caller_limit(runs_db: str) -> None:
    limits = Limits(max_runs_per_caller=1, max_runs_per_root=3, budget_per_root=Decimal("10"))

    outcomes = await asyncio.gather(*(start(runs_db, request(f"m-{i}"), limits) for i in range(8)))

    assert sum(isinstance(o, RunCreated) for o in outcomes) == 1
    rejected = [o for o in outcomes if isinstance(o, Rejected)]
    assert len(rejected) == 7
    assert {o.reason for o in rejected} == {RejectReason.CALLER_CONCURRENCY}


async def test_child_runs_draw_on_the_root_budget(runs_db: str) -> None:
    root = await start(runs_db, request("m-root", estimated_cost=Decimal("6")))
    assert isinstance(root, RunCreated)

    child = await start(
        runs_db,
        request(
            "m-child",
            caller="agent:discovery",
            estimated_cost=Decimal("5"),
            root_run_id=root.run_id,
        ),
    )

    assert isinstance(child, Rejected)
    assert child.reason is RejectReason.CHAIN_BUDGET


async def test_a_rejected_request_is_not_recorded_so_its_retry_is_reconsidered(
    runs_db: str,
) -> None:
    limits = Limits(max_runs_per_caller=1, max_runs_per_root=3, budget_per_root=Decimal("10"))
    busy = await start(runs_db, request("m-1"), limits)
    assert isinstance(busy, RunCreated)
    assert isinstance(await start(runs_db, request("m-2"), limits), Rejected)

    async with await psycopg.AsyncConnection.connect(runs_db, autocommit=True) as conn:
        await conn.execute("UPDATE runs SET status = 'succeeded' WHERE id = %s", (busy.run_id,))

    assert isinstance(await start(runs_db, request("m-2"), limits), RunCreated)


async def test_the_schema_itself_forbids_a_duplicate_key(runs_db: str) -> None:
    await start(runs_db, request("m-1"))

    async with await psycopg.AsyncConnection.connect(runs_db, autocommit=True) as conn:
        with pytest.raises(psycopg.errors.UniqueViolation):
            await conn.execute(
                "INSERT INTO runs (id, root_run_id, caller, message_id, task_id, agent,"
                " estimated_cost) VALUES (gen_random_uuid(), gen_random_uuid(),"
                " 'user:alice', 'm-1', 't', 'discovery', 1)"
            )


async def test_a_reused_run_reports_its_current_status(runs_db: str) -> None:
    first = await start(runs_db, request("m-1"))
    assert isinstance(first, RunCreated)
    async with await psycopg.AsyncConnection.connect(runs_db, autocommit=True) as conn:
        await conn.execute("UPDATE runs SET status = 'canceled' WHERE id = %s", (first.run_id,))

    assert await start(runs_db, request("m-1")) == RunReused(run_id=first.run_id, status="canceled")
