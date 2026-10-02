from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from golem.catalog import Neighbour, Role
from golem.runtime.lead import Record


@dataclass(frozen=True)
class LinkedRecord:
    id: str
    text: str


@dataclass(frozen=True)
class Brief:
    """Everything a role needs from its first turn; it must not have to go looking."""

    run_id: str
    goal: str
    role: Role
    instructions: str
    target: Record
    target_path: Path
    target_text: str
    linked: tuple[LinkedRecord, ...]
    workspace: Path
    skills_dir: Path | None
    delegates: tuple[Neighbour, ...] = ()
    # A goal agent's run (ADR 0017): it may end with a proposal or with only its record.
    goal_mode: bool = False
    # Proposal branches still open on the target; a goal run extends them rather than repeats.
    open_proposals: tuple[str, ...] = ()
    # What the agent's runs propose (ADR 0015); a kind the platform applies ends with the role's
    # own proposal, not only its branch.
    proposal_kind: str = "merge_request"


@dataclass(frozen=True)
class RoleResult:
    summary: str
    # Whether a goal run's role found something to act on; a record run always proposes.
    proposed: bool = False
    # What the role submitted for a kind the platform applies: the proposal file's content,
    # body files as paths in the workspace. Checked again before it leaves the Job.
    proposal: Mapping[str, Any] | None = None


class RoleRunner(Protocol):
    async def run(self, brief: Brief) -> RoleResult: ...
