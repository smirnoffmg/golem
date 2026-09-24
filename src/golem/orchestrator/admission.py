"""Admission: the orchestrator's decision whether a new run may start.

One of three layers against runaway callers (with edge rate limiting and namespace
ResourceQuota); this one knows about callers and call chains.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum


@dataclass(frozen=True)
class RunRequest:
    caller: str
    root_run_id: str
    estimated_cost: Decimal


@dataclass(frozen=True)
class Load:
    """Current state read from the runs database; missing keys mean zero."""

    running_by_caller: Mapping[str, int]
    running_by_root: Mapping[str, int]
    spent_by_root: Mapping[str, Decimal]


@dataclass(frozen=True)
class Limits:
    max_runs_per_caller: int
    max_runs_per_root: int
    budget_per_root: Decimal

    def __post_init__(self) -> None:
        if self.max_runs_per_caller <= 0:
            raise ValueError(f"max_runs_per_caller must be positive: {self.max_runs_per_caller}")
        if self.max_runs_per_root <= 0:
            raise ValueError(f"max_runs_per_root must be positive: {self.max_runs_per_root}")
        if self.budget_per_root < 0:
            raise ValueError(f"budget_per_root must not be negative: {self.budget_per_root}")


class RejectReason(Enum):
    CALLER_CONCURRENCY = "caller_concurrency"
    CHAIN_CONCURRENCY = "chain_concurrency"
    CHAIN_BUDGET = "chain_budget"


@dataclass(frozen=True)
class Admitted:
    pass


@dataclass(frozen=True)
class Rejected:
    reason: RejectReason
    detail: str


def admit(request: RunRequest, load: Load, limits: Limits) -> Admitted | Rejected:
    if request.estimated_cost < 0:
        raise ValueError(f"estimated_cost must not be negative: {request.estimated_cost}")
    return (
        _check_caller_concurrency(request, load, limits)
        or _check_chain_concurrency(request, load, limits)
        or _check_chain_budget(request, load, limits)
        or Admitted()
    )


def _check_caller_concurrency(request: RunRequest, load: Load, limits: Limits) -> Rejected | None:
    running = load.running_by_caller.get(request.caller, 0)
    if running < limits.max_runs_per_caller:
        return None
    return Rejected(
        RejectReason.CALLER_CONCURRENCY,
        f"Caller {request.caller} already has {running} running runs; "
        f"the limit is {limits.max_runs_per_caller}.",
    )


def _check_chain_concurrency(request: RunRequest, load: Load, limits: Limits) -> Rejected | None:
    running = load.running_by_root.get(request.root_run_id, 0)
    if running < limits.max_runs_per_root:
        return None
    return Rejected(
        RejectReason.CHAIN_CONCURRENCY,
        f"Call chain {request.root_run_id} already has {running} running runs; "
        f"the limit is {limits.max_runs_per_root}.",
    )


def _check_chain_budget(request: RunRequest, load: Load, limits: Limits) -> Rejected | None:
    spent = load.spent_by_root.get(request.root_run_id, Decimal(0))
    if spent + request.estimated_cost <= limits.budget_per_root:
        return None
    return Rejected(
        RejectReason.CHAIN_BUDGET,
        f"Call chain {request.root_run_id} has spent {spent}; a run estimated at "
        f"{request.estimated_cost} would exceed the budget of {limits.budget_per_root}.",
    )
