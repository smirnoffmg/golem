"""Run and reconciler metrics against real Postgres: what admission, the launch, a cancel and
the reconciler record, and that each final status is counted exactly once."""

import asyncio
import json
from decimal import Decimal

import psycopg
import pytest
from test_reconcile import Inbox, StatusBoard, age, new_run
from test_tasks_to_runs import CATALOG, SIGNING_KEY, TEMPLATE, FakeLauncher

from golem.metrics import OTHER, Metrics, ReconcilerMetrics
from golem.orchestrator.admission import Limits
from golem.orchestrator.jobs import JobStatus
from golem.orchestrator.reconcile import LAUNCH_GRACE_SECONDS, SucceededRun, reconcile_once
from golem.orchestrator.reconciler import run_forever
from golem.orchestrator.service import PostgresOrchestrator
from golem.tasks.ports import Refused, RunStart, Started


def sample(metrics: Metrics, name: str, **labels: str) -> float:
    return metrics.registry.get_sample_value(name, labels) or 0.0


def orchestrator(dsn: str, metrics: Metrics, launcher: FakeLauncher) -> PostgresOrchestrator:
    return PostgresOrchestrator(
        dsn=dsn,
        limits=Limits(max_runs_per_caller=1, max_runs_per_root=3, budget_per_root=Decimal("10")),
        estimated_cost=Decimal("2.5"),
        launcher=launcher,
        template=TEMPLATE,
        catalogs={"discovery": CATALOG},
        signing_key=SIGNING_KEY,
        grants={},
        metrics=metrics,
    )


def run_start(message_id: str, caller: str = "user:alice", agent: str = "discovery") -> RunStart:
    return RunStart(
        task_id=f"task-{message_id}",
        context_id="ctx",
        agent=agent,
        goal="go",
        caller=caller,
        message_id=message_id,
    )


async def test_admission_records_started_runs_their_cost_and_rejections(runs_db: str) -> None:
    metrics = Metrics("tasks", agents={"discovery"})
    service = orchestrator(runs_db, metrics, FakeLauncher())

    assert isinstance(await service.start(run_start("m-1")), Started)
    assert isinstance(await service.start(run_start("m-1")), Started)
    assert isinstance(await service.start(run_start("m-2")), Refused)
    assert isinstance(await service.start(run_start("m-3", agent="unregistered")), Refused)

    assert sample(metrics, "golem_runs_started_total", agent="discovery") == 1
    assert sample(metrics, "golem_run_reserved_cost_total", agent="discovery") == 2.5
    assert sample(metrics, "golem_admission_rejections_total", reason="caller_concurrency") == 1
    assert sample(metrics, "golem_admission_rejections_total", reason="unknown_agent") == 1


async def test_a_cancel_and_a_failed_launch_end_runs_with_their_outcome(runs_db: str) -> None:
    metrics = Metrics("tasks", agents={"discovery"})
    launcher = FakeLauncher()
    service = orchestrator(runs_db, metrics, launcher)

    await service.start(run_start("m-1"))
    await service.cancel("task-m-1")
    await service.cancel("task-m-1")
    launcher.failure = RuntimeError("the API server said no")
    await service.start(run_start("m-2", caller="user:bob"))

    outcomes = "golem_run_outcomes_total"
    assert sample(metrics, outcomes, agent="discovery", outcome="canceled") == 1
    assert sample(metrics, outcomes, agent="discovery", outcome="failed") == 1
    duration = "golem_run_duration_seconds_count"
    assert sample(metrics, duration, agent="discovery", outcome="canceled") == 1


async def reconcile(dsn: str, board: StatusBoard, metrics: ReconcilerMetrics, propose=None) -> None:
    async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as conn:
        await reconcile_once(conn, board, Inbox().notify, propose, metrics)


@pytest.mark.parametrize(
    ("job", "report", "outcome"),
    [
        (JobStatus.SUCCEEDED, {"outcome": "proposed"}, "succeeded"),
        (JobStatus.SUCCEEDED, {"outcome": "idle", "reasons": ["nothing"]}, "idle"),
        (JobStatus.FAILED, {"outcome": "invalid", "reasons": ["bad"]}, "invalid"),
        (JobStatus.FAILED, {"outcome": "failed", "summary": "model"}, "failed"),
        (JobStatus.FAILED, None, "failed"),
        (JobStatus.MISSING, None, "failed"),
        # The report comes from an untrusted Job: an outcome outside the set is not a label.
        (JobStatus.SUCCEEDED, {"outcome": "pwned-label-value"}, "succeeded"),
    ],
)
async def test_the_reconciler_records_each_run_outcome_once(
    runs_db: str, job: JobStatus, report: dict | None, outcome: str
) -> None:
    metrics = ReconcilerMetrics(Metrics("reconciler", agents={"discovery"}))
    run_id = await new_run(runs_db, "m-1")
    await age(runs_db, run_id, LAUNCH_GRACE_SECONDS + 1)
    board = StatusBoard(statuses={run_id: job})
    if report is not None:
        board.messages[run_id] = json.dumps(report)

    await reconcile(runs_db, board, metrics)
    await reconcile(runs_db, board, metrics)

    outcomes = {
        s.labels["outcome"]: s.value
        for family in metrics.runs.registry.collect()
        for s in family.samples
        if s.name == "golem_run_outcomes_total"
    }
    assert outcomes == {outcome: 1}


async def test_the_reconciler_gauges_the_outbox_and_unsettled_proposals(runs_db: str) -> None:
    metrics = ReconcilerMetrics(Metrics("reconciler", agents={"discovery"}))
    failed = await new_run(runs_db, "m-1")
    succeeded = await new_run(runs_db, "m-2")
    board = StatusBoard(statuses={failed: JobStatus.FAILED, succeeded: JobStatus.SUCCEEDED})

    async def gitlab_down(run: SucceededRun) -> str:
        raise RuntimeError("GitLab is down")

    async with await psycopg.AsyncConnection.connect(runs_db, autocommit=True) as conn:
        await reconcile_once(conn, board, Inbox(accepting=False).notify, gitlab_down, metrics)

    r = metrics.runs.registry
    assert r.get_sample_value("golem_outbox_pending") == 1
    assert r.get_sample_value("golem_proposals_pending") == 1
    assert r.get_sample_value("golem_merge_request_failures_total") == 1

    async def no_branch(run: SucceededRun) -> str:
        return f"Run {run.run_id} succeeded and proposed no changes."

    async with await psycopg.AsyncConnection.connect(runs_db, autocommit=True) as conn:
        await reconcile_once(conn, board, Inbox().notify, no_branch, metrics)

    assert r.get_sample_value("golem_outbox_pending") == 0
    assert r.get_sample_value("golem_proposals_pending") == 0
    assert r.get_sample_value("golem_proposals_settled_total") == 1


async def test_an_unconfigured_agent_is_other_in_the_reconciler(runs_db: str) -> None:
    metrics = ReconcilerMetrics(Metrics("reconciler", agents={"reviewer"}))
    run_id = await new_run(runs_db, "m-1")

    await reconcile(runs_db, StatusBoard(statuses={run_id: JobStatus.FAILED}), metrics)

    assert (
        metrics.runs.registry.get_sample_value(
            "golem_run_outcomes_total", {"agent": OTHER, "outcome": "failed"}
        )
        == 1
    )


async def test_the_loop_times_every_pass_and_counts_the_failed_ones() -> None:
    metrics = ReconcilerMetrics()
    stop = asyncio.Event()
    passes: list[int] = []

    async def reconcile_pass() -> None:
        passes.append(len(passes))
        if len(passes) == 2:
            stop.set()
            raise RuntimeError("database went away")

    await run_forever(reconcile_pass, interval_seconds=0.01, stop=stop, metrics=metrics)

    r = metrics.runs.registry
    assert r.get_sample_value("golem_reconcile_pass_duration_seconds_count") == 2
    assert r.get_sample_value("golem_reconcile_pass_errors_total") == 1
