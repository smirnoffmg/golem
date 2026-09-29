"""One run of an agent inside its Job: clone, decide, run a role, validate, propose a branch.

Outcomes and exit codes: ``idle`` (0) when the lead has nothing to do, ``proposed`` (0) when a
clean branch was pushed, ``reported`` (0) when a goal agent's clean branch carries only its
record, nothing to decide (ADR 0017), ``invalid`` (2) when the role's change broke a validator
and nothing was pushed, ``failed`` (1) for everything else. The report goes to stdout and, as
JSON under 4 KiB, to the Kubernetes termination message, where the orchestrator reads the branch.
"""

import asyncio
import contextlib
import json
import os
import sys
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field, replace
from enum import StrEnum
from pathlib import Path
from typing import TextIO

from golem.catalog import AgentCatalog, ContextRepo, Goal, Kind, goal_target, load_catalog
from golem.proposal_payload import APPLIED_KINDS, PROPOSAL_FILE, ProposalError, payload_of
from golem.runtime.brief import build_brief
from golem.runtime.lead import Command, Idle, decide
from golem.runtime.ports import Brief, RoleResult, RoleRunner
from golem.runtime.snapshot import Located, build_snapshot
from golem.runtime.validate import ContextState, read_state, validate
from golem.runtime.workspace import (
    PROPOSAL_PREFIX,
    changed_paths,
    clone_at_revision,
    clone_branch,
    commit_all,
    create_branch,
    git_env,
    head_commit,
    pending_ids,
    proposal_branch,
    proposal_target,
    push_branch,
    remote_branches,
)

REQUIRED = ("GOLEM_RUN_ID", "GOLEM_AGENT", "GOLEM_CATALOG_REF", "GOLEM_GOAL")
DEFAULT_WORKDIR = Path("/workspace")
TERMINATION_LOG = Path("/dev/termination-log")
# Kubernetes keeps at most 4096 bytes of a termination message.
MAX_REPORT_BYTES = 4096
MAX_TEXT = 500


class SettingsError(ValueError):
    pass


@dataclass(frozen=True)
class RuntimeSettings:
    run_id: str
    agent: str
    catalog_url: str
    catalog_revision: str
    goal: str
    git_token: str | None = field(default=None, repr=False)
    workdir: Path = DEFAULT_WORKDIR
    # The record a goal agent's run works on, as its starter named it; unchecked here.
    target: str = ""


def parse_settings(environ: Mapping[str, str]) -> RuntimeSettings:
    missing = [name for name in REQUIRED if not environ.get(name, "").strip()]
    if missing:
        raise SettingsError(f"missing required settings: {', '.join(missing)}")
    url, _, revision = environ["GOLEM_CATALOG_REF"].rpartition("#")
    if not url.strip() or not revision.strip():
        raise SettingsError("GOLEM_CATALOG_REF must be '<url>#<revision>'")
    return RuntimeSettings(
        run_id=environ["GOLEM_RUN_ID"],
        agent=environ["GOLEM_AGENT"],
        catalog_url=url,
        catalog_revision=revision,
        goal=environ["GOLEM_GOAL"],
        git_token=environ.get("GOLEM_GIT_TOKEN") or None,
        workdir=Path(environ.get("GOLEM_WORKDIR") or DEFAULT_WORKDIR),
        target=environ.get("GOLEM_TARGET", ""),
    )


class Outcome(StrEnum):
    IDLE = "idle"
    PROPOSED = "proposed"
    REPORTED = "reported"
    INVALID = "invalid"
    FAILED = "failed"


EXIT_CODES = {
    Outcome.IDLE: 0,
    Outcome.PROPOSED: 0,
    Outcome.REPORTED: 0,
    Outcome.FAILED: 1,
    Outcome.INVALID: 2,
}


@dataclass(frozen=True)
class RunReport:
    run_id: str
    agent: str
    outcome: Outcome
    role: str | None = None
    target_id: str | None = None
    branch: str | None = None
    reasons: tuple[str, ...] = ()
    summary: str | None = None
    # A reported run's target record on its branch: the reconciler reads it as the report.
    record: str | None = None


def exit_code(report: RunReport) -> int:
    return EXIT_CODES[report.outcome]


@dataclass(frozen=True)
class Checkout:
    catalog: AgentCatalog
    catalog_dir: Path
    context_dir: Path
    env: Mapping[str, str]


async def run(
    settings: RuntimeSettings, runner: RoleRunner, base_env: Mapping[str, str] = os.environ
) -> RunReport:
    checkout = check_out(settings, git_env(settings.git_token, base_env))
    if checkout.catalog.goal is not None:
        return await run_goal(settings, runner, checkout, checkout.catalog.goal)
    before = read_state(checkout.context_dir)
    pending = pending_ids(remote_branches(checkout.context_dir, PROPOSAL_PREFIX, checkout.env))
    decision = decide(checkout.catalog.rules, build_snapshot(checkout.context_dir, pending))
    if isinstance(decision, Idle):
        return RunReport(
            run_id=settings.run_id,
            agent=settings.agent,
            outcome=Outcome.IDLE,
            reasons=decision.reasons,
        )
    branch = proposal_branch(decision.target_id, settings.run_id)
    create_branch(checkout.context_dir, branch, checkout.env)
    return await propose(settings, runner, checkout, before, decision, branch)


async def run_goal(
    settings: RuntimeSettings, runner: RoleRunner, checkout: Checkout, goal: Goal
) -> RunReport:
    """A goal agent's run: no lead; the starter names the target, the runtime opens it."""
    repo, env = checkout.context_dir, checkout.env
    target = goal_target(settings.target, settings.run_id)
    # An alert that fires again is new information: open proposals inform the run, not stop it.
    open_proposals = tuple(
        branch
        for branch in remote_branches(repo, PROPOSAL_PREFIX, env)
        if proposal_target(branch) == target
    )
    branch = proposal_branch(target, settings.run_id)
    create_branch(repo, branch, env)
    if find(read_state(repo).records, target) is None:
        writes = role_writes(checkout.catalog, goal.role)
        write_goal_record(
            repo / writes / f"{target}.md", target, kind_of(checkout.catalog, goal), settings.goal
        )
        # Committed before the role runs, so the validators judge the role's change alone.
        commit_all(repo, f"golem: open {target}\n\nRun: {settings.run_id}\n", env)
    return await propose(
        settings,
        runner,
        checkout,
        read_state(repo),
        Command(role=goal.role, target_id=target),
        branch,
        open_proposals=open_proposals,
    )


def find(records: tuple[Located, ...], record_id: str) -> Located | None:
    return next((item for item in records if item.record.id == record_id), None)


def role_writes(catalog: AgentCatalog, role: str) -> str:
    return next(r.writes for r in catalog.roles if r.name == role).strip("/")


def kind_of(catalog: AgentCatalog, goal: Goal) -> Kind:
    return next(kind for kind in catalog.kinds if kind.name == goal.kind)


def write_goal_record(path: Path, target: str, kind: Kind, goal: str) -> None:
    # Quoted: a goal carries an alert's text, and a `## ` line of its own would become a section.
    quoted = "\n".join(f"> {line}".rstrip() for line in goal.strip().splitlines())
    sections = "".join(f"\n## {name}\n" for name in sorted(kind.sections))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\nid: {target}\nkind: {kind.name}\nstatus: {kind.initial}\n---\n{quoted}\n{sections}",
        encoding="utf-8",
    )


def check_out(settings: RuntimeSettings, env: Mapping[str, str]) -> Checkout:
    catalog_dir = settings.workdir / "catalog"
    context_dir = settings.workdir / "context"
    clone_at_revision(settings.catalog_url, settings.catalog_revision, catalog_dir, env)
    catalog = load_catalog(catalog_dir / "agent.yaml")
    if catalog.name != settings.agent:
        raise ValueError(
            f"catalog {settings.catalog_url}#{settings.catalog_revision} is agent"
            f" {catalog.name!r}, the Job was started for {settings.agent!r}"
        )
    context = context_of(catalog)
    clone_branch(context.url, context.branch, context_dir, env)
    return Checkout(catalog=catalog, catalog_dir=catalog_dir, context_dir=context_dir, env=env)


def context_of(catalog: AgentCatalog) -> ContextRepo:
    if catalog.context is None:
        raise ValueError(f"catalog {catalog.name!r} declares no context repository")
    return catalog.context


async def propose(
    settings: RuntimeSettings,
    runner: RoleRunner,
    checkout: Checkout,
    before: ContextState,
    command: Command,
    branch: str,
    open_proposals: tuple[str, ...] = (),
) -> RunReport:
    catalog = checkout.catalog
    brief = replace(
        build_brief(
            run_id=settings.run_id,
            goal=settings.goal,
            catalog=catalog,
            catalog_dir=checkout.catalog_dir,
            context_dir=checkout.context_dir,
            records=before.records,
            command=command,
        ),
        goal_mode=catalog.goal is not None,
        open_proposals=open_proposals,
        proposal_kind=catalog.proposal,
    )
    report = RunReport(
        run_id=settings.run_id,
        agent=settings.agent,
        outcome=Outcome.FAILED,
        role=command.role,
        target_id=command.target_id,
    )
    repo, env = checkout.context_dir, checkout.env
    base = head_commit(repo, env)
    try:
        result = await runner.run(brief)
    except Exception as error:
        return replace(report, reasons=(f"{type(error).__name__}: {error}",))
    violations = validate(
        role=brief.role,
        target_id=command.target_id,
        target_source=brief.target_path.relative_to(repo).as_posix(),
        changed=changed_paths(repo, base, env),
        before=before,
        after=read_state(repo),
        kinds=checkout.catalog.kinds,
    )
    if not violations and catalog.proposal in APPLIED_KINDS:
        violations = proposal_violations(repo, brief, result)
    if violations:
        return replace(report, outcome=Outcome.INVALID, reasons=violations, summary=result.summary)
    outcome, record = Outcome.PROPOSED, None
    if brief.goal_mode and not result.proposed:
        outcome, record = Outcome.REPORTED, brief.target_path.relative_to(repo).as_posix()
    # A goal run's outcome goes on its branch too: if its Job is gone before the reconciler
    # reads the report, the branch is all that says whether it reported or proposed.
    trailers = {"Outcome": outcome.value, "Record": record} if brief.goal_mode else {}
    if outcome is Outcome.PROPOSED and result.proposal is not None:
        # Written by the runtime after validation, outside the role's directory: the one file
        # the reconciler reads to learn what the run proposes (ADR 0015).
        (repo / PROPOSAL_FILE).write_text(
            json.dumps(result.proposal, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
    commit_all(repo, commit_message(settings.run_id, command, result.summary, trailers), env)
    push_branch(repo, branch, env)
    return replace(report, branch=branch, summary=result.summary, outcome=outcome, record=record)


def proposal_violations(repo: Path, brief: Brief, result: RoleResult) -> tuple[str, ...]:
    """What keeps a run of a kind the platform applies from proposing (ADR 0015): no proposal
    where one is due, or one the reconciler would refuse when it reads it back."""
    if result.proposal is None:
        if brief.goal_mode:
            return ()
        return (f"the role submitted no {brief.proposal_kind} proposal; the run must end with one",)
    try:
        payload_of(
            brief.proposal_kind,
            dict(result.proposal),
            lambda path: (repo / path).read_text(encoding="utf-8"),
            under=brief.role.writes,
        )
    except ProposalError as error:
        return (f"the proposal is invalid: {error}",)
    return ()


def commit_message(
    run_id: str, command: Command, summary: str, extra: Mapping[str, str | None] | None = None
) -> str:
    subject = f"golem: {command.role} on {command.target_id}"
    trailer = f"Run: {run_id}\nRole: {command.role}\nTarget: {command.target_id}\n"
    trailer += "".join(f"{key}: {value}\n" for key, value in (extra or {}).items() if value)
    return "\n\n".join(part for part in (subject, summary.strip(), trailer) if part)


def report_json(report: RunReport, limit: int = MAX_REPORT_BYTES) -> str:
    clipped = replace(
        report,
        reasons=tuple(clip(reason) for reason in report.reasons),
        summary=None if report.summary is None else clip(report.summary),
    )
    total = len(clipped.reasons)
    encoded = encode(clipped)
    for keep in range(total - 1, -1, -1):
        if len(encoded.encode()) <= limit:
            break
        encoded = encode(
            replace(clipped, reasons=(*clipped.reasons[:keep], f"and {total - keep} more"))
        )
    return encoded


def clip(text: str, limit: int = MAX_TEXT) -> str:
    return text if len(text) <= limit else f"{text[: limit - 1]}…"


def encode(report: RunReport) -> str:
    return json.dumps(asdict(report), ensure_ascii=False)


def write_report(report: RunReport, termination_path: Path, out: TextIO) -> None:
    encoded = report_json(report)
    # Outside Kubernetes there is no termination log; stdout still has the report.
    with contextlib.suppress(OSError):
        termination_path.write_text(encoded, encoding="utf-8")
    print(encoded, file=out, flush=True)


RunnerFactory = Callable[[Mapping[str, str]], RoleRunner]


def main(
    environ: Mapping[str, str],
    runner_factory: RunnerFactory,
    termination_path: Path = TERMINATION_LOG,
    out: TextIO = sys.stdout,
) -> int:
    report = asyncio.run(run_safely(environ, runner_factory))
    write_report(report, termination_path, out)
    return exit_code(report)


async def run_safely(environ: Mapping[str, str], runner_factory: RunnerFactory) -> RunReport:
    try:
        settings = parse_settings(environ)
        return await run(settings, runner_factory(environ), environ)
    except Exception as error:
        return RunReport(
            run_id=environ.get("GOLEM_RUN_ID", ""),
            agent=environ.get("GOLEM_AGENT", ""),
            outcome=Outcome.FAILED,
            reasons=(f"{type(error).__name__}: {error}",),
        )
