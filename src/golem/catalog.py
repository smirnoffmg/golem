import re
import string
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

SLUG = r"^[a-z][a-z0-9-]*$"
TOOL_GROUP = r"^[a-z][a-z0-9-]*(\.[a-z][a-z0-9-]*)*$"
# A relative directory whose segments never start with a dot and hold no glob characters: the
# file tools turn it into an allow rule, and `.git`, `.` or `*` there would open more than it.
WRITES_DIR = r"^[A-Za-z0-9_][A-Za-z0-9._-]*(/[A-Za-z0-9_][A-Za-z0-9._-]*)*/?$"
# The one tool group the runtime serves itself, not an MCP server: `delegate_to_agent`, which
# asks another agent for work through the edge (ADR 0014). Like every group, deny by default.
DELEGATE_GROUP = "agents.delegate"
AGENT_FILE = "agent.yaml"
PROCESS_FILE = "process.yaml"
# A handful the model can tell apart from their `when` (ADR 0019), not a directory to search.
MAX_DELEGATES = 7
# A person the platform names by their identity provider name; never a wildcard (ADR 0015).
REVIEWER = re.compile(r"^user:[^\s:*]+$")
MAX_STAGES = 10
MAX_GOAL_CHARS = 4000
GOAL_INPUT = "input"

ProposalKind = Literal["merge_request", "wiki_edit", "desk_reply", "tracker_issue"]
# A goal run's target, named by whoever started it (ADR 0017). ADR 0017 allows a leading digit;
# a record id may not start with one, and the target becomes a record's id.
GOAL_TARGET = re.compile(r"^[a-z][a-z0-9-]{0,63}$")


class CatalogError(ValueError):
    pass


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
    """A kind of record in the context repository, with the statuses and sections it may have.

    A role may create a record only in the ``initial`` status: every later status is a human
    decision, and a record born in one would skip it."""

    name: str
    initial: str
    statuses: frozenset[str]
    sections: frozenset[str] = frozenset()

    @model_validator(mode="after")
    def _initial_is_a_status(self) -> "Kind":
        if self.initial not in self.statuses:
            raise ValueError(
                f"initial status {self.initial!r} of kind {self.name!r} is not one of its statuses"
            )
        return self


class Role(_Frozen):
    name: str = Field(pattern=SLUG)
    writes: str = Field(pattern=WRITES_DIR)
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


class Goal(_Frozen):
    """What a goal agent's run executes: one role, on a record of one kind it creates."""

    role: str
    kind: str


class Neighbour(_Frozen):
    agent: str = Field(pattern=SLUG)
    when: str = Field(min_length=1, max_length=300)


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
    mode: Literal["records", "goal"] = "records"
    goal: Goal | None = None
    proposal: ProposalKind = "merge_request"
    delegates: tuple[Neighbour, ...] = Field(default=(), max_length=MAX_DELEGATES)
    # Who may decide what the agent proposes, besides a person who started the run (ADR 0015).
    reviewers: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _reviewers_are_people(self) -> "AgentCatalog":
        for reviewer in self.reviewers:
            if not REVIEWER.fullmatch(reviewer):
                raise ValueError(f"reviewer {reviewer!r} is not a named person (user:<name>)")
        repeated = sorted({r for r in self.reviewers if self.reviewers.count(r) > 1})
        if repeated:
            raise ValueError(f"reviewer {repeated[0]!r} is listed twice")
        return self

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

    @model_validator(mode="after")
    def _goal_runs(self) -> "AgentCatalog":
        if self.mode == "records":
            if self.goal is not None:
                raise ValueError("only an agent with mode: goal names a goal")
            return self
        if self.goal is None:
            raise ValueError(f"goal agent {self.name!r} names no goal")
        if self.context is None:
            raise ValueError(f"goal agent {self.name!r} declares no context repository")
        if self.goal.role not in {role.name for role in self.roles}:
            raise ValueError(f"goal refers to undeclared role {self.goal.role!r}")
        if self.goal.kind not in {kind.name for kind in self.kinds}:
            raise ValueError(f"goal refers to undeclared kind {self.goal.kind!r}")
        return self

    @model_validator(mode="after")
    def _delegates_go_with_the_group(self) -> "AgentCatalog":
        # The list is what the tool offers: a group without it offers nothing, a list without
        # the group is dead text a reviewer would take for a grant.
        names = [neighbour.agent for neighbour in self.delegates]
        repeated = sorted({name for name in names if names.count(name) > 1})
        if repeated:
            raise ValueError(f"neighbour {repeated[0]!r} is listed twice")
        if self.name in names:
            raise ValueError(f"agent {self.name!r} lists itself as a neighbour")
        delegating = any(DELEGATE_GROUP in role.tools for role in self.roles)
        if names and not delegating:
            raise ValueError(
                f"agent {self.name!r} lists delegates, but no role holds {DELEGATE_GROUP}"
            )
        if delegating and not names:
            raise ValueError(
                f"a role of {self.name!r} holds {DELEGATE_GROUP},"
                " but the agent declares no delegates"
            )
        return self


class Stage(_Frozen):
    name: str = Field(pattern=SLUG)
    agent: str = Field(pattern=SLUG)
    goal: str = Field(min_length=1)

    @model_validator(mode="after")
    def _goal_is_a_template_of_the_input(self) -> "Stage":
        for _, field_name, spec, conversion in string.Formatter().parse(self.goal):
            if field_name is not None and (field_name != GOAL_INPUT or spec or conversion):
                raise ValueError(
                    f"stage {self.name!r}: a goal may use only {{{GOAL_INPUT}}},"
                    f" not {{{field_name}}}"
                )
        if len(self.goal.format(input="")) > MAX_GOAL_CHARS:
            raise ValueError(f"stage {self.name!r}: the goal is over {MAX_GOAL_CHARS} characters")
        return self


class ProcessCatalog(_Frozen):
    """A process (ADR 0019): stages the platform runs in turn, each a goal agent's run."""

    name: str = Field(pattern=SLUG)
    description: str
    version: str
    skills: tuple[Skill, ...] = ()
    return_limit: int = Field(default=2, ge=0, le=5)
    stages: tuple[Stage, ...] = Field(min_length=1, max_length=MAX_STAGES)

    @model_validator(mode="after")
    def _stages_named_once(self) -> "ProcessCatalog":
        names = [stage.name for stage in self.stages]
        repeated = sorted({name for name in names if names.count(name) > 1})
        if repeated:
            raise ValueError(f"stage {repeated[0]!r} is named twice")
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


def render_goal(stage: Stage, message: str) -> str:
    goal = stage.goal.format(input=message)
    if not goal.strip():
        raise ValueError(f"stage {stage.name!r}: the goal is empty")
    if len(goal) > MAX_GOAL_CHARS:
        raise ValueError(f"stage {stage.name!r}: the goal is over {MAX_GOAL_CHARS} characters")
    return goal


@dataclass(frozen=True)
class Catalogs:
    agents: Mapping[str, AgentCatalog] = field(default_factory=dict)
    processes: Mapping[str, ProcessCatalog] = field(default_factory=dict)


def load_catalogs(root: Path) -> Catalogs:
    """Every ``<dir>/agent.yaml`` and ``<dir>/process.yaml`` under ``root``, checked together."""
    agents: dict[str, AgentCatalog] = {}
    processes: dict[str, ProcessCatalog] = {}
    for directory in sorted(path for path in root.iterdir() if path.is_dir()):
        agent_file, process_file = directory / AGENT_FILE, directory / PROCESS_FILE
        if agent_file.is_file() and process_file.is_file():
            raise CatalogError(
                f"{directory}: both {AGENT_FILE} and {PROCESS_FILE}; a catalog is one"
            )
        if agent_file.is_file():
            agent = _parsed(AgentCatalog, agent_file)
            _check_new(agent.name, agents, processes)
            agents[agent.name] = agent
        elif process_file.is_file():
            process = _parsed(ProcessCatalog, process_file)
            _check_new(process.name, agents, processes)
            processes[process.name] = process
    check_catalogs(agents, processes)
    return Catalogs(agents=agents, processes=processes)


def _parsed[C: (AgentCatalog, ProcessCatalog)](model: type[C], path: Path) -> C:
    try:
        return model.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
    except (ValidationError, yaml.YAMLError) as error:
        raise CatalogError(f"{path}: {error}") from error


def _check_new(name: str, *taken: Mapping[str, object]) -> None:
    if any(name in names for names in taken):
        raise CatalogError(f"{name!r} is defined twice")


def check_catalogs(
    agents: Mapping[str, AgentCatalog], processes: Mapping[str, ProcessCatalog]
) -> None:
    """What no single catalog can check: names across catalogs, and processes' stages."""
    for name in sorted(agents.keys() & processes.keys()):
        raise CatalogError(f"{name!r} is both an agent and a process")
    for agent in agents.values():
        for neighbour in agent.delegates:
            _pinned_agent(agents, processes, neighbour.agent, f"agent {agent.name!r} neighbour")
    for process in processes.values():
        contexts = set()
        for stage in process.stages:
            where = f"process {process.name!r} stage {stage.name!r} names agent"
            worker = _pinned_agent(agents, processes, stage.agent, where)
            if worker.mode != "goal":
                raise CatalogError(f"{where} {worker.name!r}, which is not a goal agent")
            if "proposal" not in worker.model_fields_set:
                raise CatalogError(f"{where} {worker.name!r}, which names no proposal kind")
            contexts.add(worker.context)
        if len(contexts) > 1:
            # Stages hand over through the context repository's main branch alone.
            raise CatalogError(
                f"process {process.name!r}: its stage agents must share one context repository"
            )


def _pinned_agent(
    agents: Mapping[str, AgentCatalog],
    processes: Mapping[str, ProcessCatalog],
    name: str,
    where: str,
) -> AgentCatalog:
    if name in processes:
        raise CatalogError(f"{where} {name!r}, which is a process, not an agent")
    if name not in agents:
        raise CatalogError(f"{where} {name!r}, which no pinned catalog defines")
    return agents[name]


def goal_target(named: str, run_id: str) -> str:
    """The record a goal run works on: the starter's name for it, or the run's own."""
    return named if GOAL_TARGET.fullmatch(named) else f"run-{run_id}"
