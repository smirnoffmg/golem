"""The golden set: cases of a catalog's evaluation, one directory per case.

``evals/<case id>/case.yaml`` states the goal and what the run must produce;
``evals/<case id>/context/`` is the whole context repository the run starts from. The format is
documented in ``examples/discovery/evals/README.md``. Unknown keys are errors: a misspelt check
would otherwise never run and the case would pass for the wrong reason.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from golem.runtime.main import Outcome

CASE_FILE = "case.yaml"
CONTEXT_DIR = "context"
CASE_ID = re.compile(r"[a-z0-9][a-z0-9-]*")
EXPECTED_OUTCOMES = (Outcome.IDLE, Outcome.INVALID, Outcome.PROPOSED, Outcome.REPORTED)
CASE_KEYS = frozenset({"goal", "pending", "expect"})
EXPECT_KEYS = frozenset({"outcome", "role", "target", "checks", "delegates"})
CHECK_KEYS = frozenset(
    {"sections_filled", "files_changed_under", "must_contain", "must_not_contain"}
)


class CaseError(ValueError):
    pass


@dataclass(frozen=True)
class RecordSections:
    record: str
    sections: tuple[str, ...]


@dataclass(frozen=True)
class RecordPhrases:
    record: str
    phrases: tuple[str, ...]


@dataclass(frozen=True)
class Checks:
    sections_filled: tuple[RecordSections, ...] = ()
    files_changed_under: str | None = None
    must_contain: tuple[RecordPhrases, ...] = ()
    must_not_contain: tuple[RecordPhrases, ...] = ()


@dataclass(frozen=True)
class Expectation:
    outcome: Outcome
    role: str | None = None
    target: str | None = None
    checks: Checks = Checks()
    # The neighbours the role must ask; empty: none; None: routing is not checked.
    delegates: tuple[str, ...] | None = None


@dataclass(frozen=True)
class Case:
    id: str
    goal: str
    pending: tuple[str, ...]
    expect: Expectation
    context_dir: Path


def load_cases(cases_dir: Path) -> tuple[Case, ...]:
    if not cases_dir.is_dir():
        raise CaseError(f"{cases_dir}: not a directory")
    cases = tuple(load_case(path) for path in sorted(cases_dir.iterdir()) if is_case_dir(path))
    if not cases:
        raise CaseError(f"{cases_dir}: no cases: expected <case id>/{CASE_FILE} directories")
    return cases


def is_case_dir(path: Path) -> bool:
    return path.is_dir() and not path.name.startswith(".")


def load_case(case_dir: Path) -> Case:
    source = case_dir / CASE_FILE
    if not CASE_ID.fullmatch(case_dir.name):
        raise CaseError(f"{case_dir}: case id {case_dir.name!r} must match {CASE_ID.pattern!r}")
    if not source.is_file():
        raise CaseError(f"{source}: missing: every case directory needs a {CASE_FILE}")
    context = case_dir / CONTEXT_DIR
    if not context.is_dir():
        raise CaseError(f"{context}: missing: every case needs its context repository")
    return parse_case(case_dir.name, source.read_text(encoding="utf-8"), str(source), context)


def parse_case(case_id: str, text: str, source: str, context_dir: Path) -> Case:
    fields = load_mapping(text, source)
    check_keys(fields, CASE_KEYS, None, source)
    return Case(
        id=case_id,
        goal=required_string(fields, "goal", source),
        pending=string_list(fields.get("pending", []), "pending", source),
        expect=parse_expectation(required(fields, "expect", source), source),
        context_dir=context_dir,
    )


def parse_expectation(value: Any, source: str) -> Expectation:
    fields = mapping(value, "expect", source)
    check_keys(fields, EXPECT_KEYS, "expect", source)
    return Expectation(
        outcome=parse_outcome(required(fields, "outcome", source), source),
        role=optional_string(fields, "role", source),
        target=optional_string(fields, "target", source),
        checks=parse_checks(fields.get("checks") or {}, source),
        delegates=(
            string_list(fields["delegates"], "delegates", source)
            if fields.get("delegates") is not None
            else None
        ),
    )


def parse_outcome(value: Any, source: str) -> Outcome:
    names = [outcome.value for outcome in EXPECTED_OUTCOMES]
    if value not in names:
        raise CaseError(f"{source}: 'outcome' must be one of {', '.join(sorted(names))}")
    return Outcome(value)


def parse_checks(value: Any, source: str) -> Checks:
    fields = mapping(value, "checks", source)
    check_keys(fields, CHECK_KEYS, "checks", source)
    return Checks(
        sections_filled=tuple(
            RecordSections(record, sections)
            for record, sections in per_record(fields, "sections_filled", source)
        ),
        files_changed_under=optional_string(fields, "files_changed_under", source),
        must_contain=tuple(
            RecordPhrases(record, phrases)
            for record, phrases in per_record(fields, "must_contain", source)
        ),
        must_not_contain=tuple(
            RecordPhrases(record, phrases)
            for record, phrases in per_record(fields, "must_not_contain", source)
        ),
    )


def per_record(
    fields: Mapping[str, Any], key: str, source: str
) -> tuple[tuple[str, tuple[str, ...]], ...]:
    value = fields.get(key) or {}
    valid = isinstance(value, dict) and all(
        isinstance(record, str) and is_string_list(items) for record, items in value.items()
    )
    if not valid:
        raise CaseError(f"{source}: {key!r} must map record ids to lists of strings")
    return tuple((record, tuple(items)) for record, items in value.items())


def load_mapping(text: str, source: str) -> dict[str, Any]:
    try:
        fields = yaml.safe_load(text)
    except yaml.YAMLError as error:
        raise CaseError(f"{source}: not valid YAML: {error}") from error
    if not isinstance(fields, dict):
        raise CaseError(f"{source}: a case must be a mapping of keys to values")
    return fields


def mapping(value: Any, key: str, source: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise CaseError(f"{source}: {key!r} must be a mapping")
    return value


def check_keys(
    fields: Mapping[str, Any], allowed: frozenset[str], parent: str | None, source: str
) -> None:
    where = f" in {parent!r}" if parent else ""
    for key in fields:
        if key not in allowed:
            raise CaseError(
                f"{source}: unknown key {key!r}{where}; allowed: {', '.join(sorted(allowed))}"
            )


def required(fields: Mapping[str, Any], key: str, source: str) -> Any:
    if key not in fields:
        raise CaseError(f"{source}: missing required key {key!r}")
    return fields[key]


def required_string(fields: Mapping[str, Any], key: str, source: str) -> str:
    value = required(fields, key, source)
    if not isinstance(value, str) or not value.strip():
        raise CaseError(f"{source}: {key!r} must be a non-empty string, got {value!r}")
    return value


def optional_string(fields: Mapping[str, Any], key: str, source: str) -> str | None:
    return required_string(fields, key, source) if fields.get(key) is not None else None


def string_list(value: Any, key: str, source: str) -> tuple[str, ...]:
    if not is_string_list(value):
        raise CaseError(f"{source}: {key!r} must be a list of strings, got {value!r}")
    return tuple(value)


def is_string_list(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)
