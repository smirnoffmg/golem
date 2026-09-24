"""The quality gate: pass rate against a threshold, and no regression against the baseline.

The baseline is ``evals/baseline.json`` in the catalog repository: case id to whether it passed
on the last merged version. A case that passed there and fails now fails the gate even above
the threshold, so a change cannot trade a working case for the slack under the threshold. Cases
missing from either side are not regressions: added and removed cases show in the diff of the
merge request itself.
"""

import json
from collections.abc import Mapping
from dataclasses import dataclass

Results = Mapping[str, bool]


class BaselineError(ValueError):
    pass


@dataclass(frozen=True)
class Verdict:
    passed: bool
    pass_rate: float
    threshold: float
    regressions: tuple[str, ...]


def judge(results: Results, threshold: float, baseline: Results | None) -> Verdict:
    rate = pass_rate(results)
    lost = regressions(results, baseline or {})
    return Verdict(
        passed=rate >= threshold and not lost,
        pass_rate=rate,
        threshold=threshold,
        regressions=lost,
    )


def pass_rate(results: Results) -> float:
    return sum(results.values()) / len(results) if results else 0.0


def regressions(results: Results, baseline: Results) -> tuple[str, ...]:
    return tuple(
        sorted(
            case_id
            for case_id, passed in results.items()
            if not passed and baseline.get(case_id) is True
        )
    )


def parse_baseline(text: str, source: str) -> dict[str, bool]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as error:
        raise BaselineError(f"{source}: not valid JSON: {error}") from error
    if not isinstance(data, dict) or not all(isinstance(v, bool) for v in data.values()):
        raise BaselineError(f"{source}: a baseline maps case ids to true or false")
    return data
