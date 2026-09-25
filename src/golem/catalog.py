from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

SLUG = r"^[a-z][a-z0-9-]*$"
TOOL_GROUP = r"^[a-z][a-z0-9-]*(\.[a-z][a-z0-9-]*)*$"
# The one tool group the runtime serves itself, not an MCP server: `delegate_to_agent`, which
# asks another agent for work through the edge (ADR 0014). Like every group, deny by default.
DELEGATE_GROUP = "agents.delegate"


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Skill(_Frozen):
    id: str
    name: str
    description: str
    tags: tuple[str, ...] = ()


class EmptySection(_Frozen):
    type: Literal["empty_section"] = "empty_section"
    section: str


class NoLinked(_Frozen):
    """Holds when no record of `kind` with one of `statuses` links to the target."""

    type: Literal["no_linked"] = "no_linked"
    kind: str
    statuses: frozenset[str]


Condition = Annotated[EmptySection | NoLinked, Field(discriminator="type")]


class Rule(_Frozen):
    role: str
    kind: str
    statuses: frozenset[str]
    conditions: tuple[Condition, ...] = ()


class Kind(_Frozen):
    """A kind of record in the context repository, with the statuses and sections it may have."""

    name: str
    statuses: frozenset[str]
    sections: frozenset[str] = frozenset()


class Role(_Frozen):
    name: str = Field(pattern=SLUG)
    writes: str
    tools: tuple[Annotated[str, Field(pattern=TOOL_GROUP)], ...] = ()

    @model_validator(mode="after")
    def _tools_named_once(self) -> "Role":
        repeated = sorted({tool for tool in self.tools if self.tools.count(tool) > 1})
        if repeated:
            raise ValueError(f"role {self.name!r} names tool group {repeated[0]!r} twice")
        return self


class ContextRepo(_Frozen):
    """The Git repository of records the agent reads and writes through merge requests."""

    url: str
    branch: str = "main"


class AgentCatalog(_Frozen):
    name: str = Field(pattern=SLUG)
    description: str
    version: str
    context: ContextRepo | None = None
    skills: tuple[Skill, ...] = ()
    kinds: tuple[Kind, ...] = ()
    roles: tuple[Role, ...] = ()
    # Order is priority: the lead takes the first rule that has a target.
    rules: tuple[Rule, ...] = ()

    @model_validator(mode="after")
    def _rules_use_declared_names(self) -> "AgentCatalog":
        # A misspelt kind, status or section would otherwise leave a rule idle forever.
        roles = {role.name for role in self.roles}
        kinds = {kind.name: kind for kind in self.kinds}
        for rule in self.rules:
            if rule.role not in roles:
                raise ValueError(f"rule refers to undeclared role {rule.role!r}")
            kind = _declared_kind(kinds, rule.kind)
            _check_statuses(kind, rule.statuses)
            for condition in rule.conditions:
                match condition:
                    case EmptySection(section=section) if section not in kind.sections:
                        raise ValueError(
                            f"rule for {rule.role!r} checks undeclared section {section!r}"
                            f" of kind {kind.name!r}"
                        )
                    case NoLinked(kind=linked, statuses=statuses):
                        _check_statuses(_declared_kind(kinds, linked), statuses)
        return self


def _declared_kind(kinds: dict[str, Kind], name: str) -> Kind:
    if name not in kinds:
        raise ValueError(f"rule refers to undeclared kind {name!r}")
    return kinds[name]


def _check_statuses(kind: Kind, statuses: frozenset[str]) -> None:
    unknown = sorted(statuses - kind.statuses)
    if unknown:
        raise ValueError(f"rule refers to undeclared status {unknown} of kind {kind.name!r}")


def load_catalog(path: Path) -> AgentCatalog:
    return AgentCatalog.model_validate(yaml.safe_load(path.read_text()))
