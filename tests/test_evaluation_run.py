import shutil
from dataclasses import dataclass, field
from pathlib import Path

from golem.evaluation.cases import Case, load_case
from golem.evaluation.run import CaseResult, run_case, run_cases
from golem.runtime.ports import Brief, RoleResult

EXAMPLES = Path(__file__).parent.parent / "examples"

H2 = """\
---
id: H-2
kind: hypothesis
status: proposed
---
# Teams cannot find last quarter's decisions

## Problem

Decisions live in meeting notes scattered across spaces.

## Evidence

<!-- What supports or refutes the problem? -->
"""

H3 = """\
---
id: H-3
kind: hypothesis
status: validated
---
# Monthly reports are rebuilt by hand

## Problem

Analysts copy the same figures into a slide deck every month.

## Evidence

- Time tracking: about two days per analyst per month.
"""

RESEARCHER_CASE = """\
goal: Work the discovery backlog
expect:
  outcome: proposed
  role: researcher
  target: H-2
  checks:
    sections_filled: {H-2: [Evidence]}
    files_changed_under: hypotheses/
    must_contain: {H-2: [refutes]}
"""


def make_case(root: Path, case_id: str, case_yaml: str, records: dict[str, str]) -> Case:
    case_dir = root / "evals" / case_id
    for relative, text in records.items():
        path = case_dir / "context" / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    (case_dir / "context").mkdir(parents=True, exist_ok=True)
    (case_dir / "case.yaml").write_text(case_yaml)
    return load_case(case_dir)


def catalog(tmp_path: Path) -> Path:
    return Path(shutil.copytree(EXAMPLES / "discovery", tmp_path / "catalog"))


EVIDENCE = "- Interviews support it: 5 of 7 teams.\n- Search logs refute it: few failed searches.\n"


@dataclass
class Researcher:
    briefs: list[Brief] = field(default_factory=list)
    extra: dict[str, str] = field(default_factory=dict)

    async def run(self, brief: Brief) -> RoleResult:
        self.briefs.append(brief)
        brief.target_path.write_text(brief.target_text + EVIDENCE)
        for relative, text in self.extra.items():
            (brief.workspace / relative).write_text(text)
        return RoleResult(summary="evidence collected")


class Broken:
    async def run(self, brief: Brief) -> RoleResult:
        raise RuntimeError("model gateway timed out")


def ticking(*times: float):
    values = iter(times)
    return lambda: next(values)


async def test_a_case_whose_run_meets_expectations_passes(tmp_path):
    case = make_case(tmp_path, "researcher", RESEARCHER_CASE, {"hypotheses/H-2.md": H2})
    runner = Researcher()

    result = await run_case(case, catalog(tmp_path), runner, tmp_path / "work", ticking(10.0, 12.5))

    assert result == CaseResult(
        case_id="researcher",
        failures=(),
        outcome="proposed",
        role="researcher",
        target="H-2",
        reasons=(),
        duration=2.5,
    )
    assert result.passed
    assert runner.briefs[0].goal == "Work the discovery backlog"
    assert runner.briefs[0].instructions.startswith("# Researcher")


async def test_checks_read_what_was_pushed(tmp_path):
    case = make_case(
        tmp_path,
        "researcher",
        RESEARCHER_CASE.replace("[refutes]", "[refutes, 'a phrase nobody wrote']"),
        {"hypotheses/H-2.md": H2},
    )

    result = await run_case(case, catalog(tmp_path), Researcher(), tmp_path / "work")

    assert not result.passed
    assert result.failures == ("must_contain: H-2 does not contain 'a phrase nobody wrote'",)


async def test_a_runner_exception_is_a_failed_case(tmp_path):
    case = make_case(tmp_path, "researcher", RESEARCHER_CASE, {"hypotheses/H-2.md": H2})

    result = await run_case(case, catalog(tmp_path), Broken(), tmp_path / "work")

    assert not result.passed
    assert result.outcome == "failed"
    assert result.reasons == ("RuntimeError: model gateway timed out",)
    assert result.failures[0] == "outcome: expected proposed, got failed"
    assert "sections_filled: H-2 section 'Evidence' is empty" in result.failures


async def test_a_write_outside_the_role_directory_fails_a_proposed_case(tmp_path):
    case = make_case(tmp_path, "researcher", RESEARCHER_CASE, {"hypotheses/H-2.md": H2})
    runner = Researcher(extra={"notes.md": "scratch\n"})

    result = await run_case(case, catalog(tmp_path), runner, tmp_path / "work")

    assert result.outcome == "invalid"
    assert "outcome: expected proposed, got invalid" in result.failures
    assert "notes.md is outside the role's writes directory hypotheses/" in result.reasons


async def test_a_case_may_expect_an_invalid_run(tmp_path):
    case = make_case(
        tmp_path, "stray", "goal: g\nexpect: {outcome: invalid}\n", {"hypotheses/H-2.md": H2}
    )

    result = await run_case(
        case, catalog(tmp_path), Researcher(extra={"notes.md": "x\n"}), tmp_path / "work"
    )

    assert result.passed


async def test_pending_targets_have_an_open_proposal(tmp_path):
    case = make_case(
        tmp_path,
        "pending",
        "goal: g\npending: [H-2, H-3]\nexpect: {outcome: idle}\n",
        {"hypotheses/H-2.md": H2, "hypotheses/H-3.md": H3},
    )
    runner = Researcher()

    result = await run_case(case, catalog(tmp_path), runner, tmp_path / "work")

    assert result.passed
    assert result.outcome == "idle"
    assert runner.briefs == []
    assert any("pending" in reason for reason in result.reasons)


async def test_the_catalog_context_branch_is_honoured(tmp_path):
    catalog_dir = catalog(tmp_path)
    agent = catalog_dir / "agent.yaml"
    agent.write_text(agent.read_text().replace("branch: main", "branch: trunk"))
    case = make_case(tmp_path, "researcher", RESEARCHER_CASE, {"hypotheses/H-2.md": H2})

    result = await run_case(case, catalog_dir, Researcher(), tmp_path / "work")

    assert result.passed, result.failures


async def test_a_broken_catalog_fails_the_case_without_crashing(tmp_path):
    catalog_dir = catalog(tmp_path)
    (catalog_dir / "agent.yaml").write_text("name: discovery\nroles: [oops]\n")
    case = make_case(tmp_path, "researcher", RESEARCHER_CASE, {"hypotheses/H-2.md": H2})

    result = await run_case(case, catalog_dir, Researcher(), tmp_path / "work")

    assert not result.passed
    assert result.outcome == "failed"
    assert result.failures[0].startswith("the run did not complete: ValidationError")


async def test_a_catalog_without_context_fails_the_case(tmp_path):
    catalog_dir = catalog(tmp_path)
    agent = catalog_dir / "agent.yaml"
    text = agent.read_text()
    start, end = text.index("context:"), text.index("skills:")
    agent.write_text(text[:start] + text[end:])
    case = make_case(tmp_path, "researcher", RESEARCHER_CASE, {"hypotheses/H-2.md": H2})

    result = await run_case(case, catalog_dir, Researcher(), tmp_path / "work")

    assert not result.passed
    assert "declares no context repository" in result.failures[0]


async def test_run_cases_isolates_every_case(tmp_path):
    first = make_case(tmp_path, "first", RESEARCHER_CASE, {"hypotheses/H-2.md": H2})
    second = make_case(tmp_path, "second", RESEARCHER_CASE, {"hypotheses/H-2.md": H2})

    results = await run_cases((first, second), catalog(tmp_path), Researcher(), tmp_path / "work")

    assert [(r.case_id, r.passed) for r in results] == [("first", True), ("second", True)]
