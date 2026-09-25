"""The evaluation's outputs: a JSON report for CI artifacts and a table for the job log."""

from collections.abc import Sequence
from typing import Any

from golem.evaluation.gate import Verdict
from golem.evaluation.run import CaseResult

COLUMNS = ("CASE", "RESULT", "OUTCOME", "ROLE", "TARGET", "TIME")


class ReportError(ValueError):
    pass


def report_data(results: Sequence[CaseResult], verdict: Verdict) -> dict[str, Any]:
    return {
        "passed": verdict.passed,
        "pass_rate": round(verdict.pass_rate, 4),
        "threshold": verdict.threshold,
        "regressions": list(verdict.regressions),
        "cases": [case_data(result) for result in results],
    }


def case_data(result: CaseResult) -> dict[str, Any]:
    return {
        "id": result.case_id,
        "passed": result.passed,
        "outcome": result.outcome,
        "role": result.role,
        "target": result.target,
        "duration_seconds": round(result.duration, 3),
        "failures": list(result.failures),
        "reasons": list(result.reasons),
    }


def format_table(results: Sequence[CaseResult], verdict: Verdict) -> str:
    rows = [COLUMNS, *(row(result) for result in results)]
    widths = [max(len(cells[i]) for cells in rows) for i in range(len(COLUMNS))]
    lines = [format_row(COLUMNS, widths)]
    for result, cells in zip(results, rows[1:], strict=True):
        lines.append(format_row(cells, widths))
        lines.extend(f"  - {failure}" for failure in result.failures)
        if result.failures:
            lines.extend(f"    runtime: {reason}" for reason in result.reasons)
    lines.append(summary(results, verdict))
    lines.append(f"Gate: {'passed' if verdict.passed else 'FAILED'}")
    return "\n".join(lines)


def row(result: CaseResult) -> tuple[str, ...]:
    return (
        result.case_id,
        "pass" if result.passed else "FAIL",
        result.outcome,
        result.role or "-",
        result.target or "-",
        f"{result.duration:.1f}s",
    )


def format_row(cells: Sequence[str], widths: Sequence[int]) -> str:
    return "  ".join(cell.ljust(width) for cell, width in zip(cells, widths, strict=True)).rstrip()


def summary(results: Sequence[CaseResult], verdict: Verdict) -> str:
    passed = sum(result.passed for result in results)
    text = (
        f"Pass rate {passed}/{len(results)} ({verdict.pass_rate:.0%}),"
        f" threshold {verdict.threshold:.0%}."
    )
    if verdict.regressions:
        text += f" Regressions: {', '.join(verdict.regressions)}."
    return text


def baseline_of(data: Any, source: str) -> dict[str, bool]:
    cases = data.get("cases") if isinstance(data, dict) else None
    if not isinstance(cases, list):
        raise ReportError(f"{source}: not an evaluation report: expected cases with id and passed")
    valid = all(
        isinstance(case, dict)
        and isinstance(case.get("id"), str)
        and isinstance(case.get("passed"), bool)
        for case in cases
    )
    if not valid:
        raise ReportError(f"{source}: not an evaluation report: expected cases with id and passed")
    return {case["id"]: case["passed"] for case in cases}
