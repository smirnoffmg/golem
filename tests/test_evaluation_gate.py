import pytest

from golem.evaluation.gate import BaselineError, Verdict, judge, parse_baseline, pass_rate


def test_pass_rate_is_the_share_of_passed_cases():
    assert pass_rate({"a": True, "b": False, "c": True, "d": True}) == 0.75


def test_no_cases_is_a_zero_pass_rate():
    assert pass_rate({}) == 0.0


def test_the_gate_passes_at_the_threshold_without_a_baseline():
    verdict = judge({"a": True, "b": True, "c": True, "d": False}, threshold=0.75, baseline=None)

    assert verdict == Verdict(passed=True, pass_rate=0.75, threshold=0.75, regressions=())


def test_the_gate_fails_below_the_threshold():
    verdict = judge({"a": True, "b": False}, threshold=0.8, baseline=None)

    assert not verdict.passed
    assert verdict.pass_rate == 0.5


def test_a_regression_fails_the_gate_above_the_threshold():
    results = {"a": True, "b": True, "c": True, "d": True, "e": False}
    baseline = {"a": True, "b": True, "c": True, "d": True, "e": True}

    verdict = judge(results, threshold=0.8, baseline=baseline)

    assert verdict == Verdict(passed=False, pass_rate=0.8, threshold=0.8, regressions=("e",))


def test_a_case_failing_in_the_baseline_too_is_not_a_regression():
    verdict = judge({"a": True, "b": False}, threshold=0.5, baseline={"a": True, "b": False})

    assert verdict.passed
    assert verdict.regressions == ()


def test_new_and_removed_cases_are_not_regressions():
    verdict = judge({"new": False, "a": True}, threshold=0.5, baseline={"a": True, "gone": True})

    assert verdict.passed
    assert verdict.regressions == ()


def test_regressions_are_sorted():
    verdict = judge({"b": False, "a": False}, threshold=0.0, baseline={"a": True, "b": True})

    assert verdict.regressions == ("a", "b")


def test_parse_baseline_reads_case_ids_to_passed():
    assert parse_baseline('{"a": true, "b": false}', "evals/baseline.json") == {
        "a": True,
        "b": False,
    }


@pytest.mark.parametrize("text", ["[]", '{"a": "yes"}', "{not json", '{"a": 1}'])
def test_a_malformed_baseline_is_an_error_naming_the_file(text):
    with pytest.raises(BaselineError, match=r"^evals/baseline\.json: "):
        parse_baseline(text, "evals/baseline.json")
