import pytest

from golem.evaluation.gate import Verdict
from golem.evaluation.report import ReportError, baseline_of, format_table, report_data
from golem.evaluation.run import CaseResult

RESULTS = (
    CaseResult("designer", (), "proposed", "designer", "H-3", (), 1.25),
    CaseResult(
        "researcher",
        ("outcome: expected proposed, got invalid",),
        "invalid",
        "researcher",
        "H-2",
        ("notes.md is outside the role's writes directory hypotheses/",),
        0.5,
    ),
    CaseResult("idle", (), "idle", None, None, ("researcher: no hypothesis",), 0.25),
)
VERDICT = Verdict(passed=False, pass_rate=2 / 3, threshold=0.5, regressions=("researcher",))


def test_report_data_holds_the_verdict_and_every_case():
    data = report_data(RESULTS, VERDICT)

    assert data["passed"] is False
    assert data["pass_rate"] == pytest.approx(0.6667, abs=1e-4)
    assert data["threshold"] == 0.5
    assert data["regressions"] == ["researcher"]
    assert data["cases"][1] == {
        "id": "researcher",
        "passed": False,
        "outcome": "invalid",
        "role": "researcher",
        "target": "H-2",
        "duration_seconds": 0.5,
        "failures": ["outcome: expected proposed, got invalid"],
        "reasons": ["notes.md is outside the role's writes directory hypotheses/"],
    }


def test_the_table_lists_cases_failures_and_the_verdict():
    table = format_table(RESULTS, VERDICT)
    lines = table.splitlines()

    assert lines[0].split() == ["CASE", "RESULT", "OUTCOME", "ROLE", "TARGET", "TIME"]
    assert lines[1].split() == ["designer", "pass", "proposed", "designer", "H-3", "1.2s"]
    assert lines[2].split() == ["researcher", "FAIL", "invalid", "researcher", "H-2", "0.5s"]
    assert "  - outcome: expected proposed, got invalid" in lines
    assert "    runtime: notes.md is outside the role's writes directory hypotheses/" in lines
    assert lines[-3].split()[0] == "idle"
    assert lines[-2] == "Pass rate 2/3 (67%), threshold 50%. Regressions: researcher."
    assert lines[-1] == "Gate: FAILED"


def test_a_passing_gate_says_so():
    verdict = Verdict(passed=True, pass_rate=1.0, threshold=0.8, regressions=())

    lines = format_table(RESULTS[:1], verdict).splitlines()

    assert lines[-2:] == ["Pass rate 1/1 (100%), threshold 80%.", "Gate: passed"]


def test_baseline_of_a_report_maps_case_ids_to_passed():
    assert baseline_of(report_data(RESULTS, VERDICT), "report.json") == {
        "designer": True,
        "researcher": False,
        "idle": True,
    }


@pytest.mark.parametrize("data", [[], {"cases": "x"}, {"cases": [{"id": "a"}]}, {}])
def test_a_malformed_report_is_an_error_naming_the_file(data):
    with pytest.raises(ReportError, match=r"^report\.json: "):
        baseline_of(data, "report.json")
