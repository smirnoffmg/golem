from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from golem.catalog import Role
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


@dataclass(frozen=True)
class RoleResult:
    summary: str


class RoleRunner(Protocol):
    async def run(self, brief: Brief) -> RoleResult: ...
