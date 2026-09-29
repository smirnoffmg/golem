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
