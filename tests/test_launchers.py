from decimal import Decimal

import pytest
from test_tasks_to_runs import CATALOG, TEMPLATE

from golem.orchestrator.admission import Limits
from golem.orchestrator.launchers import NO_CLUSTER, NoCluster, launcher_for
from golem.orchestrator.service import PostgresOrchestrator
from golem.settings import Kubernetes
from golem.tasks.ports import Refused, RunStart


def test_no_cluster_is_chosen_by_the_setting() -> None:
    assert isinstance(launcher_for(Kubernetes.NONE, "team-jobs"), NoCluster)


async def test_without_a_cluster_every_run_is_refused_with_the_reason(runs_db: str) -> None:
    orchestrator = PostgresOrchestrator(
        dsn=runs_db,
        limits=Limits(max_runs_per_caller=3, max_runs_per_root=3, budget_per_root=Decimal("10")),
        estimated_cost=Decimal("1"),
        launcher=NoCluster(),
        template=TEMPLATE,
        catalogs={"discovery": CATALOG},
    )

    outcome = await orchestrator.start(
        RunStart("task-1", "ctx-1", "discovery", "go", "user:alice", "m-1")
    )

    assert isinstance(outcome, Refused)
    assert NO_CLUSTER in outcome.reason


def test_no_cluster_reports_no_job_status() -> None:
    with pytest.raises(RuntimeError, match="GOLEM_KUBERNETES=none"):
        NoCluster().status("run-1")
