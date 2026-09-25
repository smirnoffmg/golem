import asyncio
import logging
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal

from psycopg import AsyncConnection

from golem import call_token
from golem.call_token import CallClaims
from golem.metrics import Metrics
from golem.orchestrator.admission import Limits, Rejected
from golem.orchestrator.jobs import CatalogRef, JobLauncher, JobSpec
from golem.orchestrator.runs import (
    RunCreated,
    RunReused,
    StartRequest,
    cancel_run_of_task,
    fail_run,
    run_of_task,
    run_status,
    start_run,
)
from golem.run_token import RunClaims, SigningKey, issue
from golem.tasks.ports import Refused, RunOutcome, RunStart, Started, TaskRun

# The token outlives the Job's deadline by this much, so a call made in the run's last second
# is not refused on clock skew between the orchestrator and an MCP server.
TOKEN_GRACE_SECONDS = 60
UNKNOWN_AGENT = "unknown_agent"
# psycopg's default is 130 s, and every A2A start, status and cancel would wait that long for
# an unreachable database; statement_timeout does not cover connecting.
CONNECT_TIMEOUT_SECONDS = 5

log = logging.getLogger(__name__)


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


def call_claims(started: RunCreated | RunReused, run: RunStart, expires_at: int) -> CallClaims:
    # The run acts for whoever started its chain (a delegated run's caller is already that
    # subject), and its own agent joins the chain it was started with.
    return CallClaims(
        subject=run.caller,
        agent=run.agent,
        chain=(*run.chain, run.agent),
        root_run_id=started.root_run_id,
        run_id=started.run_id,
        expires_at=expires_at,
    )


def job_spec_for(
    run_id: str,
    run: RunStart,
    catalog: CatalogRef,
    template: JobTemplate,
    run_token: str,
    call_token: str,
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
        call_token=call_token,
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
    metrics: Metrics = field(default_factory=lambda: Metrics("tasks"))

    async def start(self, run: RunStart) -> Started | Refused:
        catalog = self.catalogs.get(run.agent)
        if catalog is None:
            self.metrics.admission_rejected(UNKNOWN_AGENT)
            return Refused(reason=f"No agent named {run.agent!r} is registered.")
        if run.root_run_id and not _is_run_id(run.root_run_id):
            return Refused(reason=f"The root run {run.root_run_id!r} is not a run id.")
        request = StartRequest(
            caller=run.caller,
            message_id=run.message_id,
            task_id=run.task_id,
            agent=run.agent,
            estimated_cost=self.estimated_cost,
            # A delegated run shares its chain's concurrency and budget (ADR 0004, ADR 0014).
            root_run_id=run.root_run_id or None,
        )
        async with await AsyncConnection.connect(
            self.dsn, autocommit=True, connect_timeout=CONNECT_TIMEOUT_SECONDS
        ) as conn:
            outcome = await start_run(conn, request, self.limits)
            if isinstance(outcome, Rejected):
                self.metrics.admission_rejected(outcome.reason.value)
                return Refused(reason=outcome.detail)
            if isinstance(outcome, RunCreated):
                self.metrics.run_started(run.agent, self.estimated_cost)
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
            delegation = call_token.issue(
                call_claims(outcome, run, claims.expires_at), self.signing_key, now
            )
            spec = job_spec_for(outcome.run_id, run, catalog, self.template, token, delegation)
            try:
                await asyncio.to_thread(self.launcher.launch, spec)
            except Exception as error:
                ended = await fail_run(conn, outcome.run_id)
                if ended is not None:
                    self.metrics.run_ended(ended.agent, "failed", ended.seconds)
                # The Job may exist without its token Secret, waiting for it until the deadline.
                await self._delete_job(outcome.run_id)
                return Refused(reason=f"Could not launch run {outcome.run_id}: {error}")
            # A cancel, or a reconciler, may have ended the run while its Job was created; the
            # Job would otherwise run to its deadline for a run nobody waits for.
            if await run_status(conn, outcome.run_id) != "running":
                await self._delete_job(outcome.run_id)
        return Started(run_id=outcome.run_id)

    async def _delete_job(self, run_id: str) -> None:
        try:
            await asyncio.to_thread(self.launcher.delete, run_id)
        except Exception:
            log.exception("could not delete the Job of run %s", run_id)

    async def status(self, run_id: str) -> str | None:
        async with await AsyncConnection.connect(
            self.dsn, autocommit=True, connect_timeout=CONNECT_TIMEOUT_SECONDS
        ) as conn:
            return await run_status(conn, run_id)

    async def run_of_task(self, task_id: str) -> TaskRun | None:
        async with await AsyncConnection.connect(
            self.dsn, autocommit=True, connect_timeout=CONNECT_TIMEOUT_SECONDS
        ) as conn:
            run = await run_of_task(conn, task_id)
        if run is None:
            return None
        outcome = None
        if run.final:
            outcome = RunOutcome(
                run_id=run.run_id,
                succeeded=run.status == "succeeded",
                detail=run.detail or f"Run {run.run_id} {run.status}.",
            )
        return TaskRun(run.run_id, run.caller, run.agent, outcome)

    async def cancel(self, task_id: str) -> None:
        async with await AsyncConnection.connect(
            self.dsn, autocommit=True, connect_timeout=CONNECT_TIMEOUT_SECONDS
        ) as conn:
            ended = await cancel_run_of_task(conn, task_id)
        if ended is not None:
            self.metrics.run_ended(ended.agent, "canceled", ended.seconds)
            await self._delete_job(ended.run_id)


def _is_run_id(value: str) -> bool:
    try:
        uuid.UUID(value)
    except ValueError:
        return False
    return True
