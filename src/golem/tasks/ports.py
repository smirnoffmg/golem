from dataclasses import dataclass
from typing import Protocol


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


@dataclass(frozen=True)
class Started:
    run_id: str


@dataclass(frozen=True)
class Refused:
    reason: str


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


@dataclass(frozen=True)
class TaskRun:
    """A task's run as the orchestrator recorded it, the system of record for its outcome."""

    run_id: str
    # Who started the task and for which agent: they address the task in the task store.
    caller: str
    agent: str
    # None until the outcome is final; a run that is still running, or was canceled, has none.
    outcome: RunOutcome | None


class Orchestrator(Protocol):
    async def start(self, run: RunStart) -> Started | Refused: ...

    async def cancel(self, task_id: str) -> None: ...

    async def status(self, run_id: str) -> str | None: ...

    async def run_of_task(self, task_id: str) -> TaskRun | None: ...

    async def proposal(self, proposal_id: str) -> ProposalRecord | None: ...

    async def agents_of_tasks(self, task_ids: tuple[str, ...]) -> dict[str, str]: ...
