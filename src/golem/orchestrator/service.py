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
from golem.catalog import ProcessCatalog, render_goal
from golem.metrics import Metrics
from golem.orchestrator import proposals
from golem.orchestrator.admission import Limits, Rejected
from golem.orchestrator.jobs import CatalogRef, JobLauncher, JobSpec
from golem.orchestrator.process_runs import process_record, resolve_process
from golem.orchestrator.proposals import proposal_record
from golem.orchestrator.runs import (
    RunCreated,
    RunReused,
    StartRequest,
    agents_of_tasks,
    cancel_run_of_task,
    fail_run,
    run_of_task,
    run_status,
    start_process,
    start_run,
)
from golem.proposal_payload import MERGE_REQUEST
from golem.run_token import RunClaims, SigningKey, issue
from golem.tasks.ports import (
    Access,
    ProcessRecord,
    ProposalDetail,
    ProposalPage,
    ProposalRecord,
    Refused,
    Report,
    ReportPage,
    RunOutcome,
    RunStart,
    Started,
    TaskRun,
)

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
        target=run.target,
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
    # The pinned processes (ADR 0019): a task for one of them is a process, not a run.
    processes: Mapping[str, ProcessCatalog] = field(default_factory=dict)
    # What each pinned agent proposes (ADR 0015); an agent missing here proposes a merge request.
    proposal_kinds: Mapping[str, str] = field(default_factory=dict)

    async def start(self, run: RunStart) -> Started | Refused:
        process = self.processes.get(run.agent)
        if process is not None:
            return await self._start_process(run, process)
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
            proposal_kind=self.proposal_kinds.get(run.agent, MERGE_REQUEST),
        )
        async with await AsyncConnection.connect(
            self.dsn, autocommit=True, connect_timeout=CONNECT_TIMEOUT_SECONDS
        ) as conn:
            outcome = await start_run(conn, request, self.limits)
            if isinstance(outcome, Rejected):
                self.metrics.admission_rejected(outcome.reason.value)
                return Refused(reason=outcome.detail, code=outcome.reason.value)
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

    async def _start_process(self, run: RunStart, process: ProcessCatalog) -> Started | Refused:
        if run.chain:
            # A process is where a chain starts, never a step of one.
            return Refused(reason=f"Process {run.agent!r} is started by a person, not an agent.")
        try:
            for stage in process.stages:
                render_goal(stage, run.goal)
        except ValueError as error:
            # Refused now, with the reason, rather than failed at a stage days from now.
            return Refused(reason=f"Process {run.agent!r} cannot take this input: {error}.")
        request = StartRequest(
            caller=run.caller,
            message_id=run.message_id,
            task_id=run.task_id,
            agent=run.agent,
            estimated_cost=Decimal(0),
        )
        async with await AsyncConnection.connect(
            self.dsn, autocommit=True, connect_timeout=CONNECT_TIMEOUT_SECONDS
        ) as conn:
            outcome = await start_process(conn, request, process.model_dump(mode="json"), run.goal)
        if isinstance(outcome, RunReused) and outcome.status != "running":
            return Refused(
                reason=f"Process run {outcome.run_id} for this message is already {outcome.status}."
            )
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
                proposal=run.proposal,
                report=run.report,
                canceled=run.withdrawn,
                proposal_payload=run.proposal_payload,
            )
        return TaskRun(run.run_id, run.caller, run.agent, outcome)

    async def proposal(self, proposal_id: str) -> ProposalRecord | None:
        async with await AsyncConnection.connect(
            self.dsn, autocommit=True, connect_timeout=CONNECT_TIMEOUT_SECONDS
        ) as conn:
            return await proposal_record(conn, proposal_id)

    async def process(self, process_run_id: str) -> ProcessRecord | None:
        async with await AsyncConnection.connect(
            self.dsn, autocommit=True, connect_timeout=CONNECT_TIMEOUT_SECONDS
        ) as conn:
            return await process_record(conn, process_run_id)

    async def resolve_process(
        self, task_id: str, caller: str, action: str, reason: str | None
    ) -> str:
        async with await AsyncConnection.connect(
            self.dsn, autocommit=True, connect_timeout=CONNECT_TIMEOUT_SECONDS
        ) as conn:
            return await resolve_process(conn, task_id, caller, action, reason)

    async def list_proposals(
        self,
        access: Access,
        agent: str | None,
        states: tuple[str, ...] | None,
        process: str | None,
        page: str | None,
    ) -> ProposalPage:
        async with await self._connect() as conn:
            return await proposals.list_proposals(
                conn, access, agent=agent, states=states, process=process, page=page
            )

    async def read_proposal(self, access: Access, proposal_id: str) -> ProposalDetail | None:
        async with await self._connect() as conn:
            return await proposals.read_proposal(conn, access, proposal_id)

    async def decide_proposal(
        self, access: Access, proposal_id: str, decision: str, reason: str | None
    ) -> ProposalDetail | str:
        async with await self._connect() as conn:
            return await proposals.decide_proposal(conn, access, proposal_id, decision, reason)

    async def accepted_proposal(self, proposal_id: str) -> ProposalDetail | None:
        async with await self._connect() as conn:
            return await proposals.accepted_proposal(conn, proposal_id)

    async def record_apply(
        self, proposal_id: str, state: str, detail: str | None, from_states: tuple[str, ...]
    ) -> bool:
        async with await self._connect() as conn:
            return await proposals.record_apply(
                conn, proposal_id, state, detail, from_states=from_states
            )

    async def list_reports(self, access: Access, agent: str | None, page: str | None) -> ReportPage:
        async with await self._connect() as conn:
            return await proposals.list_reports(conn, access, agent=agent, page=page)

    async def read_report(self, access: Access, task_id: str) -> Report | None:
        async with await self._connect() as conn:
            return await proposals.read_report(conn, access, task_id)

    async def _connect(self) -> AsyncConnection:
        return await AsyncConnection.connect(
            self.dsn, autocommit=True, connect_timeout=CONNECT_TIMEOUT_SECONDS
        )

    async def agents_of_tasks(self, task_ids: tuple[str, ...]) -> dict[str, str]:
        async with await AsyncConnection.connect(
            self.dsn, autocommit=True, connect_timeout=CONNECT_TIMEOUT_SECONDS
        ) as conn:
            return await agents_of_tasks(conn, task_ids)

    async def cancel(self, task_id: str) -> None:
        async with await AsyncConnection.connect(
            self.dsn, autocommit=True, connect_timeout=CONNECT_TIMEOUT_SECONDS
        ) as conn:
            ended = await cancel_run_of_task(conn, task_id)
        # A process has no Job; the reconciler withdraws its current stage (ADR 0019).
        if ended is not None and ended.agent not in self.processes:
            self.metrics.run_ended(ended.agent, "canceled", ended.seconds)
            await self._delete_job(ended.run_id)


def _is_run_id(value: str) -> bool:
    try:
        uuid.UUID(value)
    except ValueError:
        return False
    return True
