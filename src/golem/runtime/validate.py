"""Validators: what a role's change must satisfy before it becomes a branch.

They guard against the model's mistakes, not against attacks (the Job is the boundary). Each
returns every violation it finds as a message, so a run reports all of them at once.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from golem.catalog import Kind, Role
from golem.runtime.snapshot import (
    Located,
    RecordError,
    check_links,
    check_unique_ids,
    has_front_matter,
    markdown_files,
    parse_record,
)

Violations = tuple[str, ...]


@dataclass(frozen=True)
class ContextState:
    """The records that parse, and the first reason the tree does not build, if any."""

    records: tuple[Located, ...]
    error: str | None = None


def read_state(root: Path) -> ContextState:
    records: list[Located] = []
    errors: list[str] = []
    for path in markdown_files(root):
        text = path.read_text(encoding="utf-8", errors="replace")
        if not has_front_matter(text):
            continue
        source = path.relative_to(root).as_posix()
        try:
            records.append(Located(source=source, record=parse_record(text, source)))
        except RecordError as error:
            errors.append(str(error))
    if not errors:
        try:
            check_unique_ids(tuple(records))
            check_links(tuple(records))
        except RecordError as error:
            errors.append(str(error))
    return ContextState(records=tuple(records), error=errors[0] if errors else None)


def validate(
    *,
    role: Role,
    target_id: str,
    target_source: str,
    changed: Sequence[str],
    before: ContextState,
    after: ContextState,
    kinds: Sequence[Kind],
) -> Violations:
    return (
        *outside_writes(changed, role.writes),
        *broken_context(after),
        *changed_statuses(before, after),
        *new_records_start_initial(before, after, kinds),
        *no_changes(changed),
        *target_untouched(changed, target_id, target_source, after),
    )


def outside_writes(changed: Sequence[str], writes: str) -> Violations:
    allowed = PurePosixPath(writes)
    return tuple(
        f"{path} is outside the role's writes directory {writes}"
        for path in changed
        if not PurePosixPath(path).is_relative_to(allowed)
    )


def broken_context(after: ContextState) -> Violations:
    return () if after.error is None else (f"the context no longer builds: {after.error}",)


def changed_statuses(before: ContextState, after: ContextState) -> Violations:
    was = {item.record.id: item.record.status for item in before.records}
    return tuple(
        f"{rid} changed status from {was[rid]!r} to {status!r}; status changes are human decisions"
        for rid, status in sorted((item.record.id, item.record.status) for item in after.records)
        if rid in was and was[rid] != status
    )


def new_records_start_initial(
    before: ContextState, after: ContextState, kinds: Sequence[Kind]
) -> Violations:
    existed = {item.record.id for item in before.records}
    initial = {kind.name: kind.initial for kind in kinds}
    violations: list[str] = []
    for record in sorted((item.record for item in after.records), key=lambda r: r.id):
        if record.id in existed:
            continue
        if record.kind not in initial:
            violations.append(
                f"new record {record.id} is of kind {record.kind!r},"
                " which the catalog does not declare"
            )
        elif record.status != initial[record.kind]:
            violations.append(
                f"new record {record.id} has status {record.status!r};"
                f" a new {record.kind} starts as {initial[record.kind]!r}"
            )
    return tuple(violations)


def no_changes(changed: Sequence[str]) -> Violations:
    return () if changed else ("the role changed no files",)


def target_untouched(
    changed: Sequence[str], target_id: str, target_source: str, after: ContextState
) -> Violations:
    # A role may work on a target by writing another record about it: the designer answers a
    # hypothesis with a new solution in its own directory, never touching the hypothesis.
    touched = set(changed)
    linked = any(
        item.source in touched and target_id in item.record.links for item in after.records
    )
    if target_source in touched or linked:
        return ()
    return (
        f"neither the target {target_id} ({target_source}) nor a changed record linking to it"
        " changed",
    )
