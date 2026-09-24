from decimal import Decimal

import pytest

from golem.orchestrator.admission import (
    Admitted,
    Limits,
    Load,
    Rejected,
    RejectReason,
    RunRequest,
    admit,
)

LIMITS = Limits(max_runs_per_caller=3, max_runs_per_root=5, budget_per_root=Decimal("10.00"))
CALLER = "agent:discovery"
ROOT = "run-1"


def request(cost: str = "1.00") -> RunRequest:
    return RunRequest(caller=CALLER, root_run_id=ROOT, estimated_cost=Decimal(cost))


def load(
    by_caller: int = 0, by_root: int = 0, spent: str = "0", *, include_keys: bool = True
) -> Load:
    if not include_keys:
        return Load(running_by_caller={}, running_by_root={}, spent_by_root={})
    return Load(
        running_by_caller={CALLER: by_caller, "user:alice": 100},
        running_by_root={ROOT: by_root, "run-other": 100},
        spent_by_root={ROOT: Decimal(spent), "run-other": Decimal("1000")},
    )


def test_admits_when_under_all_limits():
    assert admit(request(), load(by_caller=2, by_root=4, spent="8.00"), LIMITS) == Admitted()


def test_missing_keys_are_treated_as_zero():
    assert admit(request("10.00"), load(include_keys=False), LIMITS) == Admitted()


def test_rejects_caller_at_concurrency_limit():
    result = admit(request(), load(by_caller=3), LIMITS)

    assert isinstance(result, Rejected)
    assert result.reason is RejectReason.CALLER_CONCURRENCY
    assert CALLER in result.detail
    assert "3" in result.detail


def test_rejects_chain_at_concurrency_limit():
    result = admit(request(), load(by_root=5), LIMITS)

    assert isinstance(result, Rejected)
    assert result.reason is RejectReason.CHAIN_CONCURRENCY
    assert ROOT in result.detail
    assert "5" in result.detail


def test_admits_when_budget_is_exactly_exhausted():
    assert admit(request("2.00"), load(spent="8.00"), LIMITS) == Admitted()


def test_rejects_one_cent_over_budget():
    result = admit(request("2.01"), load(spent="8.00"), LIMITS)

    assert isinstance(result, Rejected)
    assert result.reason is RejectReason.CHAIN_BUDGET
    for number in ("8.00", "2.01", "10.00"):
        assert number in result.detail


def test_caller_concurrency_is_checked_before_chain_checks():
    result = admit(request("50"), load(by_caller=3, by_root=5), LIMITS)

    assert isinstance(result, Rejected)
    assert result.reason is RejectReason.CALLER_CONCURRENCY


def test_chain_concurrency_is_checked_before_budget():
    result = admit(request("50"), load(by_root=5), LIMITS)

    assert isinstance(result, Rejected)
    assert result.reason is RejectReason.CHAIN_CONCURRENCY


def test_negative_estimated_cost_is_an_error():
    with pytest.raises(ValueError):
        admit(request("-0.01"), load(), LIMITS)


def test_zero_estimated_cost_is_allowed():
    assert admit(request("0"), load(spent="10.00"), LIMITS) == Admitted()


@pytest.mark.parametrize(
    ("per_caller", "per_root", "budget"),
    [
        (0, 1, "1"),
        (-1, 1, "1"),
        (1, 0, "1"),
        (1, -1, "1"),
        (1, 1, "-0.01"),
    ],
)
def test_invalid_limits_are_rejected_at_construction(per_caller, per_root, budget):
    with pytest.raises(ValueError):
        Limits(
            max_runs_per_caller=per_caller,
            max_runs_per_root=per_root,
            budget_per_root=Decimal(budget),
        )


def test_zero_budget_is_a_valid_limit():
    limits = Limits(max_runs_per_caller=1, max_runs_per_root=1, budget_per_root=Decimal("0"))

    assert admit(request("0"), load(include_keys=False), limits) == Admitted()
