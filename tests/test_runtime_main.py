import io
import json
import shutil
import subprocess
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import pytest

from golem.catalog import Neighbour, load_catalog
from golem.runtime.brief import BriefError, build_brief, linked_ids
from golem.runtime.lead import Command
from golem.runtime.main import (
    Outcome,
    RunReport,
    RuntimeSettings,
    SettingsError,
    exit_code,
    main,
    parse_settings,
    report_json,
    run,
)
from golem.runtime.ports import Brief, RoleResult
from golem.runtime.validate import read_state

EXAMPLES = Path(__file__).parent.parent / "examples"
AUTHOR = ("-c", "user.name=Test", "-c", "user.email=test@localhost")
ENV = {
    "GOLEM_RUN_ID": "run-1",
    "GOLEM_AGENT": "discovery",
    "GOLEM_CATALOG_REF": "https://git.example.com/agents/discovery.git#0123abc",
    "GOLEM_GOAL": "Work the discovery backlog",
}


def sh(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


# Settings


def test_parse_settings_reads_the_job_environment():
    settings = parse_settings({**ENV, "GOLEM_GIT_TOKEN": "tok", "GOLEM_WORKDIR": "/tmp/w"})

    assert settings == RuntimeSettings(
        run_id="run-1",
        agent="discovery",
        catalog_url="https://git.example.com/agents/discovery.git",
        catalog_revision="0123abc",
        goal="Work the discovery backlog",
        git_token="tok",
        workdir=Path("/tmp/w"),
    )


def test_the_target_is_read_when_the_starter_named_one():
    assert parse_settings({**ENV, "GOLEM_TARGET": "alert-0a1b2c3d4e5f"}).target == (
        "alert-0a1b2c3d4e5f"
    )
    assert parse_settings(ENV).target == ""


def test_parse_settings_defaults():
    settings = parse_settings(ENV)

    assert settings.git_token is None
    assert settings.workdir == Path("/workspace")


def test_empty_token_is_no_token():
    assert parse_settings({**ENV, "GOLEM_GIT_TOKEN": ""}).git_token is None


def test_the_token_never_shows_in_repr():
    assert "tok-secret" not in repr(parse_settings({**ENV, "GOLEM_GIT_TOKEN": "tok-secret"}))


def test_missing_settings_are_all_named():
    with pytest.raises(SettingsError, match="GOLEM_AGENT, GOLEM_GOAL"):
        parse_settings({"GOLEM_RUN_ID": "r", "GOLEM_CATALOG_REF": "u#v", "GOLEM_GOAL": " "})


@pytest.mark.parametrize("ref", ["no-revision", "#rev", "url#", "url# "])
def test_catalog_ref_needs_url_and_revision(ref):
    with pytest.raises(SettingsError, match="GOLEM_CATALOG_REF"):
        parse_settings({**ENV, "GOLEM_CATALOG_REF": ref})


def test_catalog_ref_splits_on_the_last_hash():
    settings = parse_settings({**ENV, "GOLEM_CATALOG_REF": "file:///a#b/repo.git#main"})

    assert (settings.catalog_url, settings.catalog_revision) == ("file:///a#b/repo.git", "main")


# Report


def test_exit_codes_follow_the_outcome():
    codes = {
        outcome: exit_code(RunReport(run_id="r", agent="a", outcome=outcome)) for outcome in Outcome
    }

    assert codes == {
        Outcome.IDLE: 0,
        Outcome.PROPOSED: 0,
        Outcome.REPORTED: 0,
        Outcome.INVALID: 2,
        Outcome.FAILED: 1,
    }


def test_report_json_fits_the_termination_message():
    reasons = tuple(f"violation {n}: " + "x" * 300 for n in range(100))
    report = RunReport(
        run_id="r", agent="a", outcome=Outcome.INVALID, reasons=reasons, summary="s" * 10_000
    )

    encoded = report_json(report)
    data = json.loads(encoded)

    assert len(encoded.encode()) <= 4096
    assert data["reasons"][0].startswith("violation 0")
    assert data["reasons"][-1].endswith("more")
    assert len(data["summary"]) < 1000


def test_small_report_is_kept_whole():
    report = RunReport(run_id="r", agent="a", outcome=Outcome.IDLE, reasons=("a", "b"))

    assert json.loads(report_json(report)) == {
        "run_id": "r",
        "agent": "a",
        "outcome": "idle",
        "role": None,
        "target_id": None,
        "branch": None,
        "reasons": ["a", "b"],
        "summary": None,
        "record": None,
    }


# Brief


def example_context(tmp_path: Path) -> Path:
    return Path(shutil.copytree(EXAMPLES / "context", tmp_path / "context"))


def example_catalog(tmp_path: Path) -> Path:
    return Path(shutil.copytree(EXAMPLES / "discovery", tmp_path / "catalog"))


def brief_for(tmp_path: Path, role: str, target: str) -> Brief:
    catalog_dir = example_catalog(tmp_path)
    context_dir = example_context(tmp_path)
    return build_brief(
        run_id="run-1",
        goal="goal",
        catalog=load_catalog(catalog_dir / "agent.yaml"),
        catalog_dir=catalog_dir,
        context_dir=context_dir,
        records=read_state(context_dir).records,
        command=Command(role=role, target_id=target),
    )


def test_brief_carries_role_instructions_and_target(tmp_path):
    brief = brief_for(tmp_path, "researcher", "H-2")

    assert brief.role.name == "researcher"
    assert brief.role.writes == "hypotheses/"
    assert brief.instructions == (EXAMPLES / "discovery/roles/researcher.md").read_text()
    assert brief.target.id == "H-2"
    assert brief.target_path == tmp_path / "context/hypotheses/H-2.md"
    assert brief.target_text == (EXAMPLES / "context/hypotheses/H-2.md").read_text()
    assert brief.linked == ()
    assert brief.workspace == tmp_path / "context"
    assert brief.skills_dir is None
    assert (brief.run_id, brief.goal) == ("run-1", "goal")


def test_brief_links_both_ways(tmp_path):
    to_hypothesis = brief_for(tmp_path / "a", "reviewer", "S-1")
    from_solution = brief_for(tmp_path / "b", "designer", "H-1")

    assert [linked.id for linked in to_hypothesis.linked] == ["H-1"]
    assert to_hypothesis.linked[0].text == (EXAMPLES / "context/hypotheses/H-1.md").read_text()
    assert [linked.id for linked in from_solution.linked] == ["S-1"]


def test_brief_points_at_catalog_skills_when_present(tmp_path):
    catalog_dir = example_catalog(tmp_path)
    (catalog_dir / "skills" / "research").mkdir(parents=True)
    context_dir = example_context(tmp_path)

    brief = build_brief(
        run_id="run-1",
        goal="goal",
        catalog=load_catalog(catalog_dir / "agent.yaml"),
        catalog_dir=catalog_dir,
        context_dir=context_dir,
        records=read_state(context_dir).records,
        command=Command(role="researcher", target_id="H-2"),
    )

    assert brief.skills_dir == catalog_dir / "skills"


def test_brief_carries_the_agents_neighbours_for_the_delegation_tool(tmp_path):
    catalog_dir = example_catalog(tmp_path)
    context_dir = example_context(tmp_path)
    catalog = load_catalog(catalog_dir / "agent.yaml")
    delegating = catalog.model_copy(
        update={"delegates": (Neighbour(agent="checker", when="A contract changes."),)}
    )

    brief = build_brief(
        run_id="run-1",
        goal="goal",
        catalog=delegating,
        catalog_dir=catalog_dir,
        context_dir=context_dir,
        records=read_state(context_dir).records,
        command=Command(role="researcher", target_id="H-2"),
    )

    assert brief.delegates == (Neighbour(agent="checker", when="A contract changes."),)
    assert brief_for(tmp_path / "plain", "researcher", "H-2").delegates == ()


def test_missing_role_instructions_are_a_clear_error(tmp_path):
    catalog_dir = example_catalog(tmp_path)
    (catalog_dir / "roles" / "reviewer.md").unlink()
    context_dir = example_context(tmp_path)

    with pytest.raises(BriefError, match=r"roles/reviewer\.md"):
        build_brief(
            run_id="run-1",
            goal="goal",
            catalog=load_catalog(catalog_dir / "agent.yaml"),
            catalog_dir=catalog_dir,
            context_dir=context_dir,
            records=read_state(context_dir).records,
            command=Command(role="reviewer", target_id="S-1"),
        )


def test_linked_ids_are_sorted_naturally_and_unique():
    from golem.runtime.lead import Record

    def record(rid: str, links: set[str]) -> Record:
        return Record(rid, "k", "s", frozenset(links), frozenset())

    records = (
        record("H-10", set()),
        record("H-2", set()),
        record("T", {"H-10", "H-2"}),
        record("S-1", {"T"}),
    )

    assert linked_ids(records[2], records) == ("H-2", "H-10", "S-1")


# End to end


@dataclass(frozen=True)
class Remotes:
    catalog: Path
    context: Path
    revision: str


def seed_bare(source: Path, bare: Path, work: Path) -> str:
    sh("init", "--quiet", "--initial-branch=main", cwd=work)
    sh("add", "-A", cwd=work)
    sh(*AUTHOR, "commit", "--quiet", "-m", f"seed from {source.name}", cwd=work)
    sh("clone", "--quiet", "--bare", str(work), str(bare), cwd=work.parent)
    return sh("rev-parse", "HEAD", cwd=work)


@pytest.fixture
def remotes(tmp_path: Path) -> Remotes:
    seeds = tmp_path / "seeds"
    context_seed = Path(shutil.copytree(EXAMPLES / "context", seeds / "context"))
    context = tmp_path / "remotes" / "context.git"
    seed_bare(EXAMPLES / "context", context, context_seed)

    catalog_seed = Path(shutil.copytree(EXAMPLES / "discovery", seeds / "catalog"))
    agent = catalog_seed / "agent.yaml"
    agent.write_text(
        agent.read_text().replace(
            "https://git.example.com/product/discovery-context.git", str(context)
        )
    )
    catalog = tmp_path / "remotes" / "catalog.git"
    revision = seed_bare(EXAMPLES / "discovery", catalog, catalog_seed)
    return Remotes(catalog=catalog, context=context, revision=revision)


def settings_for(remotes: Remotes, tmp_path: Path, run_id: str = "run-1") -> RuntimeSettings:
    workdir = tmp_path / f"workspace-{run_id}"
    workdir.mkdir()
    return parse_settings(
        {
            **ENV,
            "GOLEM_RUN_ID": run_id,
            "GOLEM_CATALOG_REF": f"{remotes.catalog}#{remotes.revision}",
            "GOLEM_WORKDIR": str(workdir),
        }
    )


def remote_golem_branches(bare: Path) -> list[str]:
    output = sh("for-each-ref", "--format=%(refname:short)", "refs/heads/golem/", cwd=bare)
    return output.splitlines()


def push_pending(remotes: Remotes, tmp_path: Path, branch: str) -> None:
    work = tmp_path / f"pending-{branch.replace('/', '-')}"
    sh("clone", "--quiet", str(remotes.context), str(work), cwd=tmp_path)
    sh("push", "--quiet", "origin", f"HEAD:refs/heads/{branch}", cwd=work)


EVIDENCE = "- Interviews: 5 of 7 teams searched chat history for a past decision last month.\n"


@dataclass
class FakeRunner:
    """Stands in for the model: edits files in the workspace like a role would."""

    edits: dict[str, str] = field(default_factory=dict)
    fill_target: bool = True
    status_to: str | None = None
    propose: bool = False
    # the proposal file's content the role submits, for a kind the platform applies.
    proposal: dict[str, Any] | None = None
    briefs: list[Brief] = field(default_factory=list)

    async def run(self, brief: Brief) -> RoleResult:
        self.briefs.append(brief)
        if self.fill_target:
            text = brief.target_text + EVIDENCE
            if self.status_to:
                text = text.replace(f"status: {brief.target.status}", f"status: {self.status_to}")
            brief.target_path.write_text(text)
        for relative, text in self.edits.items():
            path = brief.workspace / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        return RoleResult(
            summary=f"worked on {brief.target.id}",
            proposed=self.propose or self.proposal is not None,
            proposal=self.proposal,
        )


class BrokenRunner:
    async def run(self, brief: Brief) -> RoleResult:
        raise RuntimeError("model gateway timed out")


async def test_a_clean_run_pushes_a_proposal_branch(remotes, tmp_path):
    runner = FakeRunner()

    report = await run(settings_for(remotes, tmp_path), runner)

    assert report == RunReport(
        run_id="run-1",
        agent="discovery",
        outcome=Outcome.PROPOSED,
        role="researcher",
        target_id="H-2",
        branch="golem/H-2/run-1",
        summary="worked on H-2",
    )
    assert runner.briefs[0].instructions.startswith("# Researcher")
    assert remote_golem_branches(remotes.context) == ["golem/H-2/run-1"]
    message = sh("log", "-1", "--format=%an <%ae>%n%B", "golem/H-2/run-1", cwd=remotes.context)
    assert message.splitlines()[0] == "Golem <golem@localhost>"
    assert "run-1" in message and "researcher" in message and "H-2" in message
    pushed = sh("show", "golem/H-2/run-1:hypotheses/H-2.md", cwd=remotes.context)
    assert EVIDENCE.strip() in pushed
    changed = sh("diff", "--name-only", "main", "golem/H-2/run-1", cwd=remotes.context)
    assert changed == "hypotheses/H-2.md"


async def test_a_write_outside_the_role_directory_is_invalid_and_nothing_is_pushed(
    remotes, tmp_path
):
    runner = FakeRunner(edits={"solutions/S-1.md": "tampered\n"})

    report = await run(settings_for(remotes, tmp_path), runner)

    assert report.outcome is Outcome.INVALID
    assert report.branch is None
    assert "solutions/S-1.md is outside the role's writes directory hypotheses/" in report.reasons
    assert remote_golem_branches(remotes.context) == []


async def test_a_status_change_is_invalid(remotes, tmp_path):
    report = await run(settings_for(remotes, tmp_path), FakeRunner(status_to="validated"))

    assert report.outcome is Outcome.INVALID
    assert report.reasons == (
        "H-2 changed status from 'proposed' to 'validated'; status changes are human decisions",
    )
    assert remote_golem_branches(remotes.context) == []


async def test_a_run_that_changes_nothing_is_invalid(remotes, tmp_path):
    report = await run(settings_for(remotes, tmp_path), FakeRunner(fill_target=False))

    assert report.outcome is Outcome.INVALID
    assert "the role changed no files" in report.reasons


async def test_a_pending_target_is_skipped_for_the_next_rule(remotes, tmp_path):
    push_pending(remotes, tmp_path, "golem/H-2/run-0")
    solution = (
        "---\nid: S-2\nkind: solution\nstatus: proposed\nlinks: [H-3]\n---\n"
        "# Export the monthly figures\n\n## Approach\n\nA scheduled export.\n\n"
        "## Risks\n\n- Nobody reads it.\n\n## Review\n\n<!-- reviewer -->\n"
    )
    runner = FakeRunner(fill_target=False, edits={"solutions/S-2.md": solution})

    report = await run(settings_for(remotes, tmp_path, "run-2"), runner)

    assert (report.outcome, report.role, report.target_id) == (Outcome.PROPOSED, "designer", "H-3")
    assert remote_golem_branches(remotes.context) == ["golem/H-2/run-0", "golem/H-3/run-2"]


async def test_idle_when_every_target_is_pending(remotes, tmp_path):
    for branch in ("golem/H-2/run-0", "golem/H-3/run-0", "golem/S-1/run-0"):
        push_pending(remotes, tmp_path, branch)
    runner = FakeRunner()

    report = await run(settings_for(remotes, tmp_path), runner)

    assert report.outcome is Outcome.IDLE
    assert len(report.reasons) == 3
    assert all("pending" in reason for reason in report.reasons)
    assert runner.briefs == []
    assert len(remote_golem_branches(remotes.context)) == 3


async def test_a_runner_exception_is_a_failed_run(remotes, tmp_path):
    report = await run(settings_for(remotes, tmp_path), BrokenRunner())

    assert (report.outcome, report.role, report.target_id) == (Outcome.FAILED, "researcher", "H-2")
    assert report.reasons == ("RuntimeError: model gateway timed out",)
    assert remote_golem_branches(remotes.context) == []


async def test_the_catalog_must_be_the_agent_named_in_the_job(remotes, tmp_path):
    settings = replace(settings_for(remotes, tmp_path), agent="other")

    with pytest.raises(ValueError, match=r"'discovery'.*'other'"):
        await run(settings, BrokenRunner())


def _env_of(settings: RuntimeSettings) -> dict[str, str]:
    return {
        "GOLEM_CATALOG_REF": f"{settings.catalog_url}#{settings.catalog_revision}",
        "GOLEM_WORKDIR": str(settings.workdir),
    }


def test_main_writes_the_report_and_returns_the_exit_code(remotes, tmp_path):
    settings = settings_for(remotes, tmp_path)
    termination = tmp_path / "termination-log"
    out = io.StringIO()

    code = main({**ENV, **_env_of(settings)}, lambda _: FakeRunner(), termination, out)

    assert code == 0
    written = json.loads(termination.read_text())
    assert written["outcome"] == "proposed"
    assert json.loads(out.getvalue()) == written


def test_main_reports_setup_failures_as_failed(tmp_path):
    termination = tmp_path / "termination-log"
    env = {**ENV, "GOLEM_CATALOG_REF": f"{tmp_path / 'missing.git'}#main"}
    env["GOLEM_WORKDIR"] = str(tmp_path)

    code = main(env, lambda _: FakeRunner(), termination, io.StringIO())

    assert code == 1
    written = json.loads(termination.read_text())
    assert written["outcome"] == "failed"
    assert written["run_id"] == "run-1"
    assert "git clone" in written["reasons"][0]


def test_main_reports_bad_settings_as_failed(tmp_path):
    out = io.StringIO()

    code = main({"GOLEM_RUN_ID": "run-9"}, lambda _: FakeRunner(), tmp_path / "t", out)

    assert code == 1
    report = json.loads(out.getvalue())
    assert report["run_id"] == "run-9"
    assert "GOLEM_AGENT" in report["reasons"][0]


def test_main_tolerates_an_unwritable_termination_path(tmp_path):
    out = io.StringIO()

    code = main({}, lambda _: FakeRunner(), tmp_path / "no" / "such" / "dir", out)

    assert code == 1
    assert json.loads(out.getvalue())["outcome"] == "failed"


# Goal agents (ADR 0017): no lead, a target named by the starter, an optional proposal

GOAL = "mode: goal\ngoal:\n  role: researcher\n  kind: hypothesis\n"


@pytest.fixture
def goal_remotes(tmp_path: Path) -> Remotes:
    return remotes_with(tmp_path, extra=GOAL)


def remotes_with(tmp_path: Path, extra: str) -> Remotes:
    seeds = tmp_path / "goal-seeds"
    context_seed = Path(shutil.copytree(EXAMPLES / "context", seeds / "context"))
    context = tmp_path / "goal-remotes" / "context.git"
    seed_bare(EXAMPLES / "context", context, context_seed)
    catalog_seed = Path(shutil.copytree(EXAMPLES / "discovery", seeds / "catalog"))
    agent = catalog_seed / "agent.yaml"
    agent.write_text(
        agent.read_text().replace(
            "https://git.example.com/product/discovery-context.git", str(context)
        )
        + extra
    )
    catalog = tmp_path / "goal-remotes" / "catalog.git"
    revision = seed_bare(EXAMPLES / "discovery", catalog, catalog_seed)
    return Remotes(catalog=catalog, context=context, revision=revision)


def goal_settings(
    remotes: Remotes, tmp_path: Path, target: str = "alert-0a1b2c3d4e5f", run_id: str = "run-1"
) -> RuntimeSettings:
    return replace(settings_for(remotes, tmp_path, run_id), target=target)


async def test_a_goal_run_opens_its_target_record_and_reports_without_a_proposal(
    goal_remotes, tmp_path
):
    runner = FakeRunner()

    report = await run(goal_settings(goal_remotes, tmp_path), runner)

    assert report == RunReport(
        run_id="run-1",
        agent="discovery",
        outcome=Outcome.REPORTED,
        role="researcher",
        target_id="alert-0a1b2c3d4e5f",
        branch="golem/alert-0a1b2c3d4e5f/run-1",
        summary="worked on alert-0a1b2c3d4e5f",
        record="hypotheses/alert-0a1b2c3d4e5f.md",
    )
    brief = runner.briefs[0]
    assert (brief.goal_mode, brief.target.kind, brief.target.status) == (
        True,
        "hypothesis",
        "proposed",
    )
    pushed = sh(
        "show",
        "golem/alert-0a1b2c3d4e5f/run-1:hypotheses/alert-0a1b2c3d4e5f.md",
        cwd=goal_remotes.context,
    )
    assert pushed.startswith("---\nid: alert-0a1b2c3d4e5f\nkind: hypothesis\nstatus: proposed\n")
    assert "Work the discovery backlog" in pushed
    assert "## Evidence" in pushed and "## Problem" in pushed
    assert EVIDENCE.strip() in pushed
    # What survives of the report if the Job is gone before the reconciler reads it.
    head = sh(
        "log", "-1", "--format=%B", "golem/alert-0a1b2c3d4e5f/run-1", cwd=goal_remotes.context
    )
    assert head.rstrip().endswith("Outcome: reported\nRecord: hypotheses/alert-0a1b2c3d4e5f.md")


async def test_a_goal_run_that_found_something_proposes(goal_remotes, tmp_path):
    report = await run(goal_settings(goal_remotes, tmp_path), FakeRunner(propose=True))

    assert (report.outcome, report.branch) == (
        Outcome.PROPOSED,
        "golem/alert-0a1b2c3d4e5f/run-1",
    )
    assert remote_golem_branches(goal_remotes.context) == ["golem/alert-0a1b2c3d4e5f/run-1"]
    head = sh(
        "log", "-1", "--format=%B", "golem/alert-0a1b2c3d4e5f/run-1", cwd=goal_remotes.context
    )
    assert head.rstrip().endswith("Outcome: proposed")


def push_record(remotes: Remotes, tmp_path: Path, relative: str, text: str) -> None:
    work = tmp_path / "record-work"
    sh("clone", "--quiet", str(remotes.context), str(work), cwd=tmp_path)
    path = work / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    sh("add", "-A", cwd=work)
    sh(*AUTHOR, "commit", "--quiet", "-m", f"add {relative}", cwd=work)
    sh("push", "--quiet", "origin", "HEAD:refs/heads/main", cwd=work)


async def test_a_goal_run_works_on_an_existing_target_as_it_is(goal_remotes, tmp_path):
    existing = (
        "---\nid: alert-0a1b2c3d4e5f\nkind: hypothesis\nstatus: proposed\n---\n"
        "# Seen before\n\n## Problem\n\nDisk fills up.\n\n## Evidence\n\n"
    )
    push_record(goal_remotes, tmp_path, "hypotheses/alert-0a1b2c3d4e5f.md", existing)
    runner = FakeRunner()

    report = await run(goal_settings(goal_remotes, tmp_path), runner)

    assert report.outcome is Outcome.REPORTED
    assert runner.briefs[0].target_text == existing
    commits = sh(
        "rev-list", "--count", "main..golem/alert-0a1b2c3d4e5f/run-1", cwd=goal_remotes.context
    )
    assert commits == "1"


async def test_an_open_proposal_on_the_target_does_not_block_a_goal_run(goal_remotes, tmp_path):
    push_pending(goal_remotes, tmp_path, "golem/alert-0a1b2c3d4e5f/run-0")
    push_pending(goal_remotes, tmp_path, "golem/H-2/run-0")
    runner = FakeRunner()

    report = await run(goal_settings(goal_remotes, tmp_path), runner)

    assert report.outcome is Outcome.REPORTED
    assert runner.briefs[0].open_proposals == ("golem/alert-0a1b2c3d4e5f/run-0",)


@pytest.mark.parametrize("target", ["", "Alert-1", "9-lives", "a" * 65, "alert_1"])
async def test_a_missing_or_malformed_target_becomes_the_run_id(goal_remotes, tmp_path, target):
    report = await run(goal_settings(goal_remotes, tmp_path, target=target), FakeRunner())

    assert report.target_id == "run-run-1"
    assert report.record == "hypotheses/run-run-1.md"


async def test_a_goal_run_that_changes_nothing_is_invalid(goal_remotes, tmp_path):
    report = await run(goal_settings(goal_remotes, tmp_path), FakeRunner(fill_target=False))

    assert report.outcome is Outcome.INVALID
    assert "the role changed no files" in report.reasons
    assert remote_golem_branches(goal_remotes.context) == []


REPLY = {
    "kind": "desk_reply",
    "request": "SD-12",
    "public": True,
    "text_file": "hypotheses/replies/sd-12.txt",
}


async def test_a_goal_run_of_an_applied_kind_carries_golem_proposal_json(tmp_path):
    remotes = remotes_with(tmp_path, extra=GOAL + "proposal: desk_reply\n")
    runner = FakeRunner(
        edits={"hypotheses/replies/sd-12.txt": "The export works again."}, proposal=REPLY
    )

    report = await run(goal_settings(remotes, tmp_path), runner)

    assert report.outcome is Outcome.PROPOSED
    assert runner.briefs[0].proposal_kind == "desk_reply"
    branch = "golem/alert-0a1b2c3d4e5f/run-1"
    manifest = sh("show", f"{branch}:golem-proposals/run-1.json", cwd=remotes.context)
    assert json.loads(manifest) == REPLY
    assert sh("show", f"{branch}:hypotheses/replies/sd-12.txt", cwd=remotes.context) == (
        "The export works again."
    )


async def test_two_runs_proposals_never_share_a_file_that_lands_in_main(tmp_path):
    # Applied proposals land their branch in main; one manifest path for every run made the
    # second of two proposals from one main conflict with the first (ADR 0015).
    remotes = remotes_with(tmp_path, extra=GOAL + "proposal: desk_reply\n")
    runner = FakeRunner(
        edits={"hypotheses/replies/sd-12.txt": "The export works again."}, proposal=REPLY
    )

    report = await run(goal_settings(remotes, tmp_path), runner)

    files = sh("ls-tree", "-r", "--name-only", report.branch, cwd=remotes.context).split()
    assert "golem-proposal.json" not in files
    assert "golem-proposals/run-1.json" in files


async def test_a_goal_run_of_an_applied_kind_with_nothing_to_propose_reports(tmp_path):
    remotes = remotes_with(tmp_path, extra=GOAL + "proposal: tracker_issue\n")

    report = await run(goal_settings(remotes, tmp_path), FakeRunner())

    assert report.outcome is Outcome.REPORTED


async def test_a_record_run_of_an_applied_kind_without_its_proposal_is_invalid(tmp_path):
    remotes = remotes_with(tmp_path, extra="proposal: wiki_edit\n")

    report = await run(settings_for(remotes, tmp_path), FakeRunner())

    assert report.outcome is Outcome.INVALID
    assert report.reasons == (
        "the role submitted no wiki_edit proposal; the run must end with one",
    )
    assert remote_golem_branches(remotes.context) == []


async def test_a_record_run_of_an_applied_kind_proposes_with_its_file(tmp_path):
    remotes = remotes_with(tmp_path, extra="proposal: desk_reply\n")
    runner = FakeRunner(
        edits={"hypotheses/replies/sd-12.txt": "The export works again."}, proposal=REPLY
    )

    report = await run(settings_for(remotes, tmp_path), runner)

    assert report.outcome is Outcome.PROPOSED
    assert json.loads(
        sh("show", f"{report.branch}:golem-proposals/run-1.json", cwd=remotes.context)
    )


@pytest.mark.parametrize(
    "change",
    [
        # Outside the role's directory: the platform would never read it back as the role's.
        {"text_file": "README.md"},
        {"text_file": "hypotheses/replies/missing.txt"},
        {"kind": "wiki_edit"},
        {"public": "yes"},
    ],
)
async def test_a_proposal_the_platform_would_refuse_is_invalid(tmp_path, change):
    remotes = remotes_with(tmp_path, extra=GOAL + "proposal: desk_reply\n")
    runner = FakeRunner(
        edits={"hypotheses/replies/sd-12.txt": "The export works again."},
        proposal=REPLY | change,
    )

    report = await run(goal_settings(remotes, tmp_path), runner)

    assert report.outcome is Outcome.INVALID
    assert report.reasons[0].startswith("the proposal is invalid: ")
    assert remote_golem_branches(remotes.context) == []


async def test_a_record_agent_ignores_a_target_and_asks_the_lead(remotes, tmp_path):
    settings = replace(settings_for(remotes, tmp_path), target="alert-0a1b2c3d4e5f")

    report = await run(settings, FakeRunner())

    assert (report.outcome, report.target_id) == (Outcome.PROPOSED, "H-2")
