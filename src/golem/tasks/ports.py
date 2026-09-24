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


class Orchestrator(Protocol):
    async def start(self, run: RunStart) -> Started | Refused: ...

    async def cancel(self, task_id: str) -> None: ...
