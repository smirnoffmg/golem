from golem.evaluation.cases import Checks, Expectation, RecordPhrases, RecordSections
from golem.evaluation.checks import Published, check
from golem.runtime.main import Outcome, RunReport

H2 = """\
---
id: H-2
kind: hypothesis
status: proposed
---
# Teams cannot find last quarter's decisions

## Problem

Decisions live in meeting notes.

## Evidence

- Interviews SUPPORT it: 5 of 7 teams.
"""

EMPTY_H2 = H2.replace("- Interviews SUPPORT it: 5 of 7 teams.\n", "<!-- sources -->\n")


def report(outcome=Outcome.PROPOSED, role="researcher", target="H-2") -> RunReport:
    return RunReport(run_id="r", agent="discovery", outcome=outcome, role=role, target_id=target)


def published(text: str = H2, changed=("hypotheses/H-2.md",)) -> Published:
    return Published(changed=changed, records={"H-2": text})


def test_a_result_that_meets_every_expectation_has_no_failures():
    expect = Expectation(
        outcome=Outcome.PROPOSED,
        role="researcher",
        target="H-2",
        checks=Checks(
            sections_filled=(RecordSections("H-2", ("Problem", "Evidence")),),
            files_changed_under="hypotheses/",
            must_contain=(RecordPhrases("H-2", ("support", "5 of 7")),),
            must_not_contain=(RecordPhrases("H-2", ("status: validated",)),),
        ),
    )

    assert check(expect, report(), published()) == ()


def test_outcome_role_and_target_mismatches_are_all_listed():
    expect = Expectation(outcome=Outcome.PROPOSED, role="designer", target="H-3")

    failures = check(expect, report(outcome=Outcome.INVALID), published())

    assert failures == (
        "outcome: expected proposed, got invalid",
        "role: expected designer, got researcher",
        "target: expected H-3, got H-2",
    )


def test_unset_role_and_target_are_not_checked():
    assert check(Expectation(outcome=Outcome.PROPOSED), report(), published()) == ()


def test_an_idle_run_has_no_role_to_compare():
    expect = Expectation(outcome=Outcome.PROPOSED, role="researcher")

    failures = check(expect, report(outcome=Outcome.IDLE, role=None, target=None), published())

    assert failures == (
        "outcome: expected proposed, got idle",
        "role: expected researcher, got none",
    )


def test_sections_must_exist_be_filled_and_the_record_must_exist():
    expect = Expectation(
        outcome=Outcome.PROPOSED,
        checks=Checks(
            sections_filled=(
                RecordSections("H-2", ("Evidence", "Risks")),
                RecordSections("S-9", ("Approach",)),
            )
        ),
    )

    failures = check(expect, report(), published(EMPTY_H2))

    assert failures == (
        "sections_filled: H-2 section 'Evidence' is empty",
        "sections_filled: H-2 has no section 'Risks'",
        "sections_filled: S-9 is not a record of the result",
    )


def test_changes_outside_the_directory_are_listed():
    expect = Expectation(outcome=Outcome.PROPOSED, checks=Checks(files_changed_under="hypotheses"))
    changes = ("hypotheses/H-2.md", "solutions/S-1.md", "notes.md")

    failures = check(expect, report(), published(changed=changes))

    assert failures == (
        "files_changed_under: solutions/S-1.md changed outside hypotheses",
        "files_changed_under: notes.md changed outside hypotheses",
    )


def test_phrases_are_matched_case_insensitively():
    expect = Expectation(
        outcome=Outcome.PROPOSED,
        checks=Checks(
            must_contain=(RecordPhrases("H-2", ("interviews support", "refutes")),),
            must_not_contain=(RecordPhrases("H-2", ("MEETING NOTES", "lorem")),),
        ),
    )

    failures = check(expect, report(), published())

    assert failures == (
        "must_contain: H-2 does not contain 'refutes'",
        "must_not_contain: H-2 contains 'MEETING NOTES'",
    )


def test_phrases_on_a_missing_record_fail_must_contain_only():
    expect = Expectation(
        outcome=Outcome.PROPOSED,
        checks=Checks(
            must_contain=(RecordPhrases("S-1", ("H-2",)),),
            must_not_contain=(RecordPhrases("S-1", ("H-2",)),),
        ),
    )

    assert check(expect, report(), published()) == (
        "must_contain: S-1 is not a record of the result",
    )
