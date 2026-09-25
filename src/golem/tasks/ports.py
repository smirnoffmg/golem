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


@dataclass(frozen=True)
class Started:
    run_id: str


@dataclass(frozen=True)
class Refused:
    reason: str


@dataclass(frozen=True)
class RunOutcome:
    run_id: str
    succeeded: bool
    detail: str


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
