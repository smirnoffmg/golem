"""The lead: decides which role of an agent runs next, from a snapshot of its context repository."""

import re
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from golem.catalog import Condition, EmptySection, NoLinked, Rule


@dataclass(frozen=True)
class Record:
    id: str
    kind: str
    status: str
    links: frozenset[str]
    empty_sections: frozenset[str]


@dataclass(frozen=True)
class Snapshot:
    records: tuple[Record, ...]
    pending: frozenset[str]


@dataclass(frozen=True)
class Command:
    role: str
    target_id: str


@dataclass(frozen=True)
class Idle:
    reasons: tuple[str, ...]


Incoming = Mapping[str, tuple[Record, ...]]


def decide(rules: Sequence[Rule], snapshot: Snapshot) -> Command | Idle:
    """Return the first rule's command that has an eligible target, or why every rule is idle."""
    incoming = incoming_links(snapshot.records)
    reasons = []
    for rule in rules:
        matching = [
            r for r in snapshot.records if r.kind == rule.kind and r.status in rule.statuses
        ]
        met = [r for r in matching if all(holds(c, r, incoming) for c in rule.conditions)]
        free = [r for r in met if r.id not in snapshot.pending]
        if free:
            target = min(free, key=lambda r: (natural_key(r.id), r.id))
            return Command(role=rule.role, target_id=target.id)
        reasons.append(idle_reason(rule, len(matching), len(met)))
    return Idle(reasons=tuple(reasons))


def incoming_links(records: Sequence[Record]) -> dict[str, tuple[Record, ...]]:
    sources: defaultdict[str, list[Record]] = defaultdict(list)
    for source in records:
        for target_id in source.links:
            sources[target_id].append(source)
    return {target_id: tuple(found) for target_id, found in sources.items()}


def holds(condition: Condition, record: Record, incoming: Incoming) -> bool:
    match condition:
        case EmptySection(section=section):
            return section in record.empty_sections
        case NoLinked(kind=kind, statuses=statuses):
            return not any(
                r.kind == kind and r.status in statuses for r in incoming.get(record.id, ())
            )


def natural_key(record_id: str) -> tuple[str | int, ...]:
    # re.split with a capturing group alternates text and digits, so positions never mix types.
    parts = re.split(r"(\d+)", record_id)
    return tuple(int(part) if part.isdigit() else part for part in parts)


def idle_reason(rule: Rule, matching: int, met: int) -> str:
    wanted = f"{rule.kind} in status {', '.join(sorted(rule.statuses))}"
    if matching == 0:
        return f"{rule.role}: no {wanted}"
    if met == 0:
        return f"{rule.role}: {matching} {wanted}, none meet the conditions"
    unmet = matching - met
    return (
        f"{rule.role}: {met} {wanted} meet the conditions, all pending ({unmet} did not meet them)"
    )
