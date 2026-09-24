from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

SLUG = r"^[a-z][a-z0-9-]*$"


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


class Role(_Frozen):
    name: str = Field(pattern=SLUG)
    writes: str
    tools: tuple[str, ...] = ()


class AgentCatalog(_Frozen):
    name: str = Field(pattern=SLUG)
    description: str
    version: str
    skills: tuple[Skill, ...] = ()
    roles: tuple[Role, ...] = ()
    # Order is priority: the lead takes the first rule that has a target.
    rules: tuple[Rule, ...] = ()

    @model_validator(mode="after")
    def _rules_name_declared_roles(self) -> "AgentCatalog":
        declared = {role.name for role in self.roles}
        for rule in self.rules:
            if rule.role not in declared:
                raise ValueError(f"rule refers to undeclared role {rule.role!r}")
        return self


def load_catalog(path: Path) -> AgentCatalog:
    return AgentCatalog.model_validate(yaml.safe_load(path.read_text()))
