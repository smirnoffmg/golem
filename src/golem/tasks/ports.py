from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class RunStart:
    task_id: str
    context_id: str
    agent: str
    goal: str
    caller: str
    # Clients retry after timeouts and every retry becomes a new A2A task; the orchestrator
    # deduplicates runs by (caller, message_id) so a retry never starts a second Job.
    message_id: str
    # W3C Trace Context of the request, so the run's trace continues the caller's.
    traceparent: str = ""
    tracestate: str = ""
    # A delegated run (ADR 0014): the agents that delegated so far and the root run of their
    # chain, which the run is admitted under. Empty for a run a person or a service started.
    root_run_id: str = ""
    chain: tuple[str, ...] = ()
    # The record a goal agent's run works on, as its starter named it (ADR 0017); "" if none.
    target: str = ""


@dataclass(frozen=True)
class Started:
    run_id: str


# Why admission refused a run, machine-readable, in the rejected task's metadata (ADR 0017).
REFUSAL_METADATA = "golemRefusal"


@dataclass(frozen=True)
class Refused:
    reason: str
    # The admission rule that refused (``caller_concurrency``...); None for any other refusal.
    code: str | None = None


@dataclass(frozen=True)
class ProposalView:
    """What a task shows of its run's proposal, as ``metadata.golemProposal`` (ADR 0015)."""

    id: str
    kind: str
    state: str
    url: str


@dataclass(frozen=True)
class ProposalRecord:
    view: ProposalView
    # The tasks of the proposal's run, and who and which agent address them in the task store.
    caller: str
    agent: str
    task_ids: tuple[str, ...]


@dataclass(frozen=True)
class RunOutcome:
    run_id: str
    succeeded: bool
    detail: str
    proposal: ProposalView | None = None
    # A goal run that found nothing to propose: its target record, the task's `report`.
    report: str | None = None
    # A stage of a canceled process, withdrawn by the platform (ADR 0019).
    canceled: bool = False
    # What a proposal of a kind the platform applies proposes: the task's `proposal` artifact.
    proposal_payload: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class TaskRun:
    """A task's run as the orchestrator recorded it, the system of record for its outcome."""

    run_id: str
    # Who started the task and for which agent: they address the task in the task store.
    caller: str
    agent: str
    # None until the outcome is final; a run that is still running, or was canceled, has none.
    outcome: RunOutcome | None


@dataclass(frozen=True)
class ProcessRecord:
    """What a process task shows of its process, as ``metadata.golemProcess`` (ADR 0019)."""

    view: Mapping[str, Any]
    # Who owns the process task and which process it addresses in the task store.
    caller: str
    agent: str
    task_ids: tuple[str, ...]


# How a process's owner resolved it while it waited for a reason (ADR 0019).
RESOLVED = "resolved"
NOT_WAITING = "not_waiting"
NOT_FOUND = "not_found"
# Why a decision on a proposal was refused (ADR 0015).
ALREADY_DECIDED = "already_decided"
DECIDED_IN_GITLAB = "decided_in_gitlab"
REASON_REQUIRED = "reason_required"
# A principal no proposal token can carry (ADR 0015): refused before the row moves.
UNNAMEABLE_DECIDER = "unnameable_decider"


@dataclass(frozen=True)
class Access:
    """Who asks for proposals and reports: the principal the edge authenticated, and the agents
    whose catalogs name them a reviewer (ADR 0015)."""

    principal: str
    reviews: frozenset[str]


@dataclass(frozen=True)
class ProposalSummary:
    id: str
    task_id: str
    agent: str
    kind: str
    state: str
    summary: str
    url: str | None
    owner: str
    created_at: str
    decided_by: str | None
    decided_at: str | None


@dataclass(frozen=True)
class ProposalDetail:
    summary: ProposalSummary
    payload: Mapping[str, Any]
    digest: str | None
    target: str | None
    reason: str | None
    detail: str | None
    # The run's report, when its goal run left one next to the proposal (ADR 0017).
    report: str | None
    # A stage of a process: its rejection needs a reason, which the stage reruns with.
    stage: bool


@dataclass(frozen=True)
class ProposalPage:
    items: tuple[ProposalSummary, ...]
    next: str | None


@dataclass(frozen=True)
class ReportSummary:
    task_id: str
    agent: str
    target: str | None
    completed_at: str
    summary: str


@dataclass(frozen=True)
class Report:
    task_id: str
    agent: str
    target: str | None
    completed_at: str
    text: str


@dataclass(frozen=True)
class ReportPage:
    items: tuple[ReportSummary, ...]
    next: str | None


@dataclass(frozen=True)
class Applied:
    """What a write server made of a proposal: ``applied``, ``stale`` or ``failed``."""

    state: str
    detail: str | None = None


@dataclass(frozen=True)
class ProposalGate:
    """What a write server asks of a proposal before serving its token: whether its state
    allows the call, and whether the token was issued for the payload the row holds."""

    id: str
    state: str
    digest: str | None
    kind: str


@dataclass(frozen=True)
class LivePage:
    """A wiki_edit's page as Confluence holds it now: what the diff is taken against."""

    title: str
    version: int
    body: str


class Applier(Protocol):
    """The write servers, reached with a proposal token per call (ADR 0015)."""

    async def apply(self, decided: ProposalDetail) -> Applied: ...

    async def preview(self, proposal: ProposalDetail, reader: str) -> LivePage: ...


class Orchestrator(Protocol):
    async def start(self, run: RunStart) -> Started | Refused: ...

    async def cancel(self, task_id: str) -> None: ...

    async def status(self, run_id: str) -> str | None: ...

    async def run_of_task(self, task_id: str) -> TaskRun | None: ...

    async def proposal(self, proposal_id: str) -> ProposalRecord | None: ...

    async def agents_of_tasks(self, task_ids: tuple[str, ...]) -> dict[str, str]: ...

    async def process(self, process_run_id: str) -> ProcessRecord | None: ...

    async def resolve_process(
        self, task_id: str, caller: str, action: str, reason: str | None
    ) -> str: ...

    async def list_proposals(
        self,
        access: Access,
        agent: str | None,
        states: tuple[str, ...] | None,
        process: str | None,
        page: str | None,
    ) -> ProposalPage: ...

    async def read_proposal(self, access: Access, proposal_id: str) -> ProposalDetail | None: ...

    async def decide_proposal(
        self, access: Access, proposal_id: str, decision: str, reason: str | None
    ) -> ProposalDetail | str: ...

    async def claim_apply(self, proposal_id: str) -> ProposalDetail | None: ...

    async def proposal_gate(self, proposal_id: str) -> ProposalGate | None: ...

    async def record_apply(
        self, proposal_id: str, state: str, detail: str | None, from_states: tuple[str, ...]
    ) -> bool: ...

    async def list_reports(
        self, access: Access, agent: str | None, page: str | None
    ) -> ReportPage: ...

    async def read_report(self, access: Access, task_id: str) -> Report | None: ...
