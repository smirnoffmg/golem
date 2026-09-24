"""Checks of one case: what the run reported and what it published, against what was expected.

The result is what production would publish: the proposal branch on the context remote, or the
unchanged base branch when nothing was pushed. Every failed check is listed, not the first.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath

from golem.evaluation.cases import Checks, Expectation, RecordPhrases, RecordSections
from golem.runtime.main import RunReport
from golem.runtime.snapshot import is_blank, sections, split_front_matter

Failures = tuple[str, ...]


@dataclass(frozen=True)
class Published:
    changed: tuple[str, ...]
    records: Mapping[str, str]


def check(expect: Expectation, report: RunReport, result: Published) -> Failures:
    return (
        *compare("outcome", expect.outcome.value, report.outcome.value),
        *compare("role", expect.role, report.role),
        *compare("target", expect.target, report.target_id),
        *apply_checks(expect.checks, result),
    )


def compare(name: str, expected: str | None, actual: str | None) -> Failures:
    if expected is None or expected == actual:
        return ()
    return (f"{name}: expected {expected}, got {actual or 'none'}",)


def apply_checks(checks: Checks, result: Published) -> Failures:
    return (
        *(
            failure
            for item in checks.sections_filled
            for failure in sections_filled(item, result.records)
        ),
        *files_changed_under(checks.files_changed_under, result.changed),
        *(failure for item in checks.must_contain for failure in contains(item, result.records)),
        *(
            failure
            for item in checks.must_not_contain
            for failure in not_contains(item, result.records)
        ),
    )


def sections_filled(item: RecordSections, records: Mapping[str, str]) -> Failures:
    if item.record not in records:
        return (f"sections_filled: {item.record} is not a record of the result",)
    _, body = split_front_matter(records[item.record], item.record)
    content = dict(sections(body))
    return tuple(
        f"sections_filled: {item.record} has no section {name!r}"
        if name not in content
        else f"sections_filled: {item.record} section {name!r} is empty"
        for name in item.sections
        if name not in content or is_blank(content[name])
    )


def files_changed_under(directory: str | None, changed: tuple[str, ...]) -> Failures:
    if directory is None:
        return ()
    allowed = PurePosixPath(directory)
    return tuple(
        f"files_changed_under: {path} changed outside {directory}"
        for path in changed
        if not PurePosixPath(path).is_relative_to(allowed)
    )


def contains(item: RecordPhrases, records: Mapping[str, str]) -> Failures:
    if item.record not in records:
        return (f"must_contain: {item.record} is not a record of the result",)
    text = records[item.record].casefold()
    return tuple(
        f"must_contain: {item.record} does not contain {phrase!r}"
        for phrase in item.phrases
        if phrase.casefold() not in text
    )


def not_contains(item: RecordPhrases, records: Mapping[str, str]) -> Failures:
    text = records.get(item.record, "").casefold()
    return tuple(
        f"must_not_contain: {item.record} contains {phrase!r}"
        for phrase in item.phrases
        if phrase.casefold() in text
    )
