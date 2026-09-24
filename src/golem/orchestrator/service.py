import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal

from psycopg import AsyncConnection

from golem.orchestrator.admission import Limits, Rejected
from golem.orchestrator.jobs import CatalogRef, JobLauncher, JobSpec
from golem.orchestrator.runs import (
    RunReused,
    StartRequest,
    cancel_run_of_task,
    fail_run,
    start_run,
)
from golem.tasks.ports import Refused, RunStart, Started


@dataclass(frozen=True)
class JobTemplate:
    image: str
    namespace: str
    secret_name: str
    active_deadline_seconds: int
    ttl_seconds_after_finished: int
    cpu: str
    memory: str


def job_spec_for(run_id: str, run: RunStart, catalog: CatalogRef, template: JobTemplate) -> JobSpec:
    return JobSpec(
        run_id=run_id,
        agent=run.agent,
        image=template.image,
        namespace=template.namespace,
        catalog_ref=catalog,
        goal=run.goal,
        secret_name=template.secret_name,
        active_deadline_seconds=template.active_deadline_seconds,
        ttl_seconds_after_finished=template.ttl_seconds_after_finished,
        cpu=template.cpu,
        memory=template.memory,
    )


@dataclass(frozen=True)
class PostgresOrchestrator:
    """The task service's orchestrator port: records runs in Postgres and runs them as Jobs.

    Every run is estimated at the same flat cost until estimates come from the agent catalog.
    """

    dsn: str
    limits: Limits
    estimated_cost: Decimal
    launcher: JobLauncher
    template: JobTemplate
    catalogs: Mapping[str, CatalogRef]

    async def start(self, run: RunStart) -> Started | Refused:
        catalog = self.catalogs.get(run.agent)
        if catalog is None:
            return Refused(reason=f"No agent named {run.agent!r} is registered.")
        request = StartRequest(
            caller=run.caller,
            message_id=run.message_id,
            task_id=run.task_id,
            agent=run.agent,
            estimated_cost=self.estimated_cost,
        )
        async with await AsyncConnection.connect(self.dsn, autocommit=True) as conn:
            outcome = await start_run(conn, request, self.limits)
            if isinstance(outcome, Rejected):
                return Refused(reason=outcome.detail)
            if isinstance(outcome, RunReused) and outcome.status != "running":
                # A retry of a finished run must not leave a task WORKING that nothing will finish.
                return Refused(
                    reason=f"Run {outcome.run_id} for this message is already {outcome.status}."
                )
            # Launching is idempotent per run id, so a retry of a running run launches again:
            # that heals a crash between recording the run and launching its Job.
            spec = job_spec_for(outcome.run_id, run, catalog, self.template)
            try:
                await asyncio.to_thread(self.launcher.launch, spec)
            except Exception as error:
                await fail_run(conn, outcome.run_id)
                return Refused(reason=f"Could not launch run {outcome.run_id}: {error}")
        return Started(run_id=outcome.run_id)

    async def cancel(self, task_id: str) -> None:
        async with await AsyncConnection.connect(self.dsn, autocommit=True) as conn:
            run_id = await cancel_run_of_task(conn, task_id)
        if run_id is not None:
            await asyncio.to_thread(self.launcher.delete, run_id)
