from pathlib import Path

import pytest

from golem.evaluation.cases import (
    Case,
    CaseError,
    Checks,
    Expectation,
    RecordPhrases,
    RecordSections,
    load_cases,
    parse_case,
)
from golem.runtime.main import Outcome

FULL = """\
goal: Work the discovery backlog
pending: [H-3]
expect:
  outcome: proposed
  role: researcher
  target: H-2
  checks:
    sections_filled:
      H-2: [Evidence]
    files_changed_under: hypotheses/
    must_contain:
      H-2: [supports, refutes]
    must_not_contain:
      H-2: ["status: validated"]
"""


def test_a_full_case_parses_into_frozen_types(tmp_path):
    case = parse_case("researcher", FULL, "evals/researcher/case.yaml", tmp_path)

    assert case == Case(
        id="researcher",
        goal="Work the discovery backlog",
        pending=("H-3",),
        context_dir=tmp_path,
        expect=Expectation(
            outcome=Outcome.PROPOSED,
            role="researcher",
            target="H-2",
            checks=Checks(
                sections_filled=(RecordSections(record="H-2", sections=("Evidence",)),),
                files_changed_under="hypotheses/",
                must_contain=(RecordPhrases(record="H-2", phrases=("supports", "refutes")),),
                must_not_contain=(RecordPhrases(record="H-2", phrases=("status: validated",)),),
            ),
        ),
    )


def test_a_minimal_case_needs_only_goal_and_outcome(tmp_path):
    case = parse_case("idle", "goal: g\nexpect:\n  outcome: idle\n", "case.yaml", tmp_path)

    assert case.pending == ()
    assert case.expect == Expectation(outcome=Outcome.IDLE, role=None, target=None, checks=Checks())


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("goal: g\n", "missing required key 'expect'"),
        ("expect: {outcome: idle}\n", "missing required key 'goal'"),
        ("goal: ' '\nexpect: {outcome: idle}\n", "'goal' must be a non-empty string"),
        (
            "goal: g\nexpect: {outcome: failed}\n",
            "'outcome' must be one of idle, invalid, proposed",
        ),
        ("goal: g\nexpect: {outcome: idle, rol: x}\n", "unknown key 'rol' in 'expect'"),
        ("goal: g\ngoals: x\nexpect: {outcome: idle}\n", "unknown key 'goals'"),
        (
            "goal: g\nexpect: {outcome: idle, checks: {must_contains: {}}}\n",
            "unknown key 'must_contains' in 'checks'",
        ),
        (
            "goal: g\nexpect: {outcome: idle, checks: {must_contain: [a]}}\n",
            "'must_contain' must map record ids to lists of strings",
        ),
        (
            "goal: g\nexpect: {outcome: idle, checks: {sections_filled: {H-1: Evidence}}}\n",
            "'sections_filled' must map record ids to lists of strings",
        ),
        (
            "goal: g\nexpect: {outcome: idle, checks: {files_changed_under: 3}}\n",
            "'files_changed_under' must be a non-empty string",
        ),
        ("goal: g\npending: H-1\nexpect: {outcome: idle}\n", "'pending' must be a list of"),
        ("goal: [unclosed\n", "not valid YAML"),
        ("- a list\n", "must be a mapping"),
    ],
)
def test_malformed_cases_are_errors_naming_the_file(tmp_path, text, message):
    with pytest.raises(CaseError) as raised:
        parse_case("c", text, "evals/c/case.yaml", tmp_path)

    assert str(raised.value).startswith("evals/c/case.yaml: ")
    assert message in str(raised.value)


def write_case(root: Path, case_id: str, text: str = "goal: g\nexpect: {outcome: idle}\n") -> Path:
    case_dir = root / case_id
    (case_dir / "context").mkdir(parents=True)
    (case_dir / "case.yaml").write_text(text)
    return case_dir


def test_load_cases_reads_every_case_directory_in_order(tmp_path):
    write_case(tmp_path, "b-case")
    write_case(tmp_path, "a-case")
    (tmp_path / "baseline.json").write_text("{}")
    (tmp_path / "README.md").write_text("# Golden set\n")

    cases = load_cases(tmp_path)

    assert [case.id for case in cases] == ["a-case", "b-case"]
    assert cases[0].context_dir == tmp_path / "a-case" / "context"


def test_a_case_without_context_is_an_error(tmp_path):
    (tmp_path / "lonely").mkdir()
    (tmp_path / "lonely" / "case.yaml").write_text("goal: g\nexpect: {outcome: idle}\n")

    with pytest.raises(CaseError, match=r"lonely/context.*missing"):
        load_cases(tmp_path)


def test_a_directory_without_case_yaml_is_an_error(tmp_path):
    (tmp_path / "half" / "context").mkdir(parents=True)

    with pytest.raises(CaseError, match=r"half/case\.yaml.*missing"):
        load_cases(tmp_path)


def test_a_case_id_must_be_a_slug(tmp_path):
    write_case(tmp_path, "Bad Case")

    with pytest.raises(CaseError, match="case id 'Bad Case'"):
        load_cases(tmp_path)


def test_hidden_directories_are_skipped(tmp_path):
    write_case(tmp_path, "real")
    (tmp_path / ".cache").mkdir()

    assert [case.id for case in load_cases(tmp_path)] == ["real"]


def test_no_cases_is_an_error(tmp_path):
    with pytest.raises(CaseError, match="no cases"):
        load_cases(tmp_path)


def test_a_missing_cases_directory_is_an_error(tmp_path):
    with pytest.raises(CaseError, match="not a directory"):
        load_cases(tmp_path / "nowhere")
