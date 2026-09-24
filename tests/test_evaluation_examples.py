"""The discovery golden set, run through the runtime with fake roles in place of the model."""

from dataclasses import dataclass
from pathlib import Path

from golem.evaluation.cases import load_cases
from golem.evaluation.gate import judge
from golem.evaluation.run import run_cases
from golem.runtime.ports import Brief, RoleResult

CATALOG = Path(__file__).parent.parent / "examples" / "discovery"
CASES = CATALOG / "evals"

EVIDENCE = (
    "- Interviews: 5 of 7 teams searched chat history for a past decision; supports the problem.\n"
    "- Wiki search logs: few failed searches for decisions; refutes it in part.\n"
)
REVIEW = (
    "The approach follows from the funnel figure and the interviews. Missing risk: payment"
    " fraud.\n\nRecommendation: accept, the evidence supports it.\n"
)


def fill_section(text: str, section: str, content: str) -> str:
    return text.replace(f"## {section}\n", f"## {section}\n\n{content}", 1)


def next_solution_id(workspace: Path) -> str:
    taken = {path.stem for path in (workspace / "solutions").glob("S-*.md")}
    return next(f"S-{n}" for n in range(1, 1000) if f"S-{n}" not in taken)


def solution(solution_id: str, target_id: str) -> str:
    return (
        f"---\nid: {solution_id}\nkind: solution\nstatus: proposed\nlinks: [{target_id}]\n---\n"
        "# Export the monthly figures\n\n"
        "## Approach\n\nA scheduled export of the figures analysts copy by hand.\n\n"
        "## Risks\n\n- Nobody reads the export.\n\n"
        "## Review\n\n<!-- reviewer -->\n"
    )


@dataclass(frozen=True)
class DiscoveryRoles:
    """Does what each role's instructions ask; a role in `stray` also leaves a note at the root."""

    stray: frozenset[str] = frozenset()

    async def run(self, brief: Brief) -> RoleResult:
        match brief.role.name:
            case "researcher":
                brief.target_path.write_text(fill_section(brief.target_text, "Evidence", EVIDENCE))
            case "designer":
                solution_id = next_solution_id(brief.workspace)
                path = brief.workspace / "solutions" / f"{solution_id}.md"
                path.parent.mkdir(exist_ok=True)
                path.write_text(solution(solution_id, brief.target.id))
            case "reviewer":
                review_free = brief.target_text.split("## Review\n")[0]
                brief.target_path.write_text(f"{review_free}## Review\n\n{REVIEW}")
        if brief.role.name in self.stray:
            (brief.workspace / "notes.md").write_text("scratch\n")
        return RoleResult(summary=f"{brief.role.name} on {brief.target.id}")


async def evaluate(runner: DiscoveryRoles, workdir: Path) -> dict[str, bool]:
    results = await run_cases(load_cases(CASES), CATALOG, runner, workdir)
    return {result.case_id: result.passed for result in results}


async def test_the_golden_set_has_the_documented_cases():
    assert [case.id for case in load_cases(CASES)] == [
        "designer-answers-validated-hypothesis",
        "everything-pending-is-idle",
        "researcher-fills-evidence",
        "reviewer-reviews-proposed-solution",
    ]


async def test_roles_that_follow_their_instructions_pass_the_gate(tmp_path):
    results = await run_cases(load_cases(CASES), CATALOG, DiscoveryRoles(), tmp_path)

    assert [(r.case_id, r.failures) for r in results if not r.passed] == []
    verdict = judge({r.case_id: r.passed for r in results}, threshold=0.8, baseline=None)
    assert verdict.passed
    assert verdict.pass_rate == 1.0


async def test_a_role_writing_outside_its_directory_fails_its_case_and_the_gate(tmp_path):
    results = await run_cases(
        load_cases(CASES), CATALOG, DiscoveryRoles(stray=frozenset({"researcher"})), tmp_path
    )

    failed = [r for r in results if not r.passed]
    assert [r.case_id for r in failed] == ["researcher-fills-evidence"]
    assert failed[0].outcome == "invalid"
    assert "notes.md is outside the role's writes directory hypotheses/" in failed[0].reasons
    verdict = judge({r.case_id: r.passed for r in results}, threshold=0.8, baseline=None)
    assert not verdict.passed
    assert verdict.pass_rate == 0.75


async def test_a_case_that_passed_in_the_baseline_is_a_regression_above_the_threshold(tmp_path):
    baseline = await evaluate(DiscoveryRoles(), tmp_path / "merged")

    now = await evaluate(DiscoveryRoles(stray=frozenset({"researcher"})), tmp_path / "branch")
    verdict = judge(now, threshold=0.7, baseline=baseline)

    assert verdict.pass_rate >= 0.7
    assert not verdict.passed
    assert verdict.regressions == ("researcher-fills-evidence",)
