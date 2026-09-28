"""Runs one golden-set case through the production runtime and checks what it published.

Each case gets two local bare repositories, as a Job gets two GitLab remotes: a catalog seeded
from the catalog directory under test, with its context URL pointed at the second, a context
seeded from the case's ``context/``. Then ``golem.runtime.main.run`` runs exactly as in a Job,
and the checks read the proposal branch back from the context remote.
"""

import shutil
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

import yaml

from golem.catalog import load_catalog
from golem.evaluation.cases import Case
from golem.evaluation.checks import Published, check
from golem.evaluation.delegations import DelegationRecorder
from golem.runtime.main import Outcome, RunReport, RuntimeSettings, run
from golem.runtime.ports import RoleRunner
from golem.runtime.validate import read_state
from golem.runtime.workspace import clone_branch, git, proposal_branch

CATALOG_FILE = "agent.yaml"
AUTHOR = ("-c", "user.name=Golem evaluation", "-c", "user.email=golem@localhost")
NO_SIGNING = ("-c", "commit.gpgsign=false")
PENDING_RUN = "pending"
CATALOG_BRANCH = "main"
Clock = Callable[[], float]


@dataclass(frozen=True)
class CaseResult:
    case_id: str
    failures: tuple[str, ...]
    outcome: str
    role: str | None
    target: str | None
    reasons: tuple[str, ...]
    duration: float

    @property
    def passed(self) -> bool:
        return not self.failures


@dataclass(frozen=True)
class Remotes:
    catalog: Path
    context: Path
    agent: str
    context_branch: str


async def run_cases(
    cases: Sequence[Case],
    catalog_dir: Path,
    runner: RoleRunner,
    workdir: Path,
    clock: Clock = time.monotonic,
    delegations: DelegationRecorder | None = None,
) -> tuple[CaseResult, ...]:
    return tuple(
        [
            await run_case(case, catalog_dir, runner, workdir / case.id, clock, delegations)
            for case in cases
        ]
    )


async def run_case(
    case: Case,
    catalog_dir: Path,
    runner: RoleRunner,
    workdir: Path,
    clock: Clock = time.monotonic,
    delegations: DelegationRecorder | None = None,
) -> CaseResult:
    start = clock()
    if delegations is not None:
        delegations.take()
    try:
        report, published = await execute(case, catalog_dir, runner, workdir)
    except Exception as error:
        return CaseResult(
            case_id=case.id,
            failures=(f"the run did not complete: {type(error).__name__}: {error}",),
            outcome=Outcome.FAILED.value,
            role=None,
            target=None,
            reasons=(),
            duration=clock() - start,
        )
    delegated = delegations.take() if delegations is not None else ()
    return CaseResult(
        case_id=case.id,
        failures=check(case.expect, report, published, delegated),
        outcome=report.outcome.value,
        role=report.role,
        target=report.target_id,
        reasons=report.reasons,
        duration=clock() - start,
    )


async def execute(
    case: Case, catalog_dir: Path, runner: RoleRunner, workdir: Path
) -> tuple[RunReport, Published]:
    remotes = prepare_remotes(case, catalog_dir, workdir)
    job = workdir / "job"
    job.mkdir(parents=True)
    settings = RuntimeSettings(
        run_id=f"eval-{case.id}",
        agent=remotes.agent,
        catalog_url=str(remotes.catalog),
        catalog_revision=CATALOG_BRANCH,
        goal=case.goal,
        workdir=job,
    )
    report = await run(settings, runner)
    published = read_published(
        remotes.context, remotes.context_branch, report.branch, workdir / "published"
    )
    return report, published


def prepare_remotes(case: Case, catalog_dir: Path, workdir: Path) -> Remotes:
    catalog = load_catalog(catalog_dir / CATALOG_FILE)
    branch = catalog.context.branch if catalog.context else "main"
    context_seed = workdir / "seeds" / "context"
    shutil.copytree(case.context_dir, context_seed)
    context = seed_bare(context_seed, branch, workdir / "context.git")
    for target in case.pending:
        git(["branch", proposal_branch(target, PENDING_RUN), branch], context)
    catalog_seed = workdir / "seeds" / "catalog"
    shutil.copytree(catalog_dir, catalog_seed, ignore=shutil.ignore_patterns(".git"))
    point_context(catalog_seed / CATALOG_FILE, context)
    catalog_remote = seed_bare(catalog_seed, CATALOG_BRANCH, workdir / "catalog.git")
    return Remotes(
        catalog=catalog_remote, context=context, agent=catalog.name, context_branch=branch
    )


def point_context(agent_file: Path, url: Path) -> None:
    # A catalog without a context stays without one, so the runtime reports it as it would in
    # production instead of the evaluation papering over it.
    data = yaml.safe_load(agent_file.read_text(encoding="utf-8"))
    if isinstance(data.get("context"), dict):
        data["context"]["url"] = str(url)
        agent_file.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def seed_bare(work: Path, branch: str, bare: Path) -> Path:
    git(["init", "--quiet", f"--initial-branch={branch}"], work)
    git(["add", "--all"], work)
    git(
        [*AUTHOR, *NO_SIGNING, "commit", "--quiet", "--no-verify", "--allow-empty", "-m", "seed"],
        work,
    )
    # --no-local: a local clone walks the source's object directory and hardlinks each file,
    # which failed now and then with "No such file or directory" on an object; the pack
    # protocol reads objects through git instead.
    git(["clone", "--quiet", "--bare", "--no-local", "--", str(work), str(bare)])
    return bare


def read_published(context: Path, base: str, branch: str | None, dest: Path) -> Published:
    ref = branch or base
    clone_branch(str(context), ref, dest)
    changed = () if branch is None else diff_names(context, base, branch)
    records = {
        item.record.id: (dest / item.source).read_text(encoding="utf-8")
        for item in read_state(dest).records
    }
    return Published(changed=changed, records=records)


def diff_names(repo: Path, base: str, branch: str) -> tuple[str, ...]:
    output = git(["diff", "--name-only", "--no-renames", "-z", base, branch], repo)
    return tuple(path for path in output.split("\0") if path)
