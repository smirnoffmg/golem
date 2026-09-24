import asyncio
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal

from psycopg import AsyncConnection

from golem.orchestrator.admission import Limits, Rejected
from golem.orchestrator.jobs import CatalogRef, JobLauncher, JobSpec
from golem.orchestrator.runs import (
    RunCreated,
    RunReused,
    StartRequest,
    cancel_run_of_task,
    fail_run,
    run_status,
    start_run,
)
from golem.run_token import RunClaims, SigningKey, issue
from golem.tasks.ports import Refused, RunStart, Started

# The token outlives the Job's deadline by this much, so a call made in the run's last second
# is not refused on clock skew between the orchestrator and an MCP server.
TOKEN_GRACE_SECONDS = 60


@dataclass(frozen=True)
class JobTemplate:
    image: str
    namespace: str
    secret_name: str
    active_deadline_seconds: int
    ttl_seconds_after_finished: int
    cpu: str
    memory: str
    mcp_registry_configmap: str | None = None


def run_claims(
    started: RunCreated | RunReused,
    run: RunStart,
    grants: Mapping[str, tuple[str, ...]],
    template: JobTemplate,
    now: int,
) -> RunClaims:
    return RunClaims(
        run_id=started.run_id,
        agent=run.agent,
        caller=run.caller,
        root_run_id=started.root_run_id,
        tools=grants.get(run.agent, ()),
        expires_at=now + template.active_deadline_seconds + TOKEN_GRACE_SECONDS,
    )


def job_spec_for(
    run_id: str, run: RunStart, catalog: CatalogRef, template: JobTemplate, run_token: str
) -> JobSpec:
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
        run_token=run_token,
        traceparent=run.traceparent,
        tracestate=run.tracestate,
        mcp_registry_configmap=template.mcp_registry_configmap,
    )


@dataclass(frozen=True)
class PostgresOrchestrator:
    """The task service's orchestrator port: records runs in Postgres and runs them as Jobs.

    Every run is estimated at the same flat cost until estimates come from the agent catalog.
    A launched run gets a run token carrying the platform's tool grants for its agent.
    """

    dsn: str
    limits: Limits
    estimated_cost: Decimal
    launcher: JobLauncher
    template: JobTemplate
    catalogs: Mapping[str, CatalogRef]
    signing_key: SigningKey
    grants: Mapping[str, tuple[str, ...]]
    clock: Callable[[], float] = field(default=time.time)

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
            now = int(self.clock())
            claims = run_claims(outcome, run, self.grants, self.template, now)
            token = issue(claims, self.signing_key, now)
            spec = job_spec_for(outcome.run_id, run, catalog, self.template, token)
            try:
                await asyncio.to_thread(self.launcher.launch, spec)
            except Exception as error:
                await fail_run(conn, outcome.run_id)
                return Refused(reason=f"Could not launch run {outcome.run_id}: {error}")
        return Started(run_id=outcome.run_id)

    async def status(self, run_id: str) -> str | None:
        async with await AsyncConnection.connect(self.dsn, autocommit=True) as conn:
            return await run_status(conn, run_id)

    async def cancel(self, task_id: str) -> None:
        async with await AsyncConnection.connect(self.dsn, autocommit=True) as conn:
            run_id = await cancel_run_of_task(conn, task_id)
        if run_id is not None:
            await asyncio.to_thread(self.launcher.delete, run_id)
