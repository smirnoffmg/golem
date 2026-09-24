from golem.catalog import EmptySection, NoLinked, Rule
from golem.runtime.lead import Command, Idle, Record, Snapshot, decide


def record(
    id: str,
    kind: str = "hypothesis",
    status: str = "proposed",
    links: frozenset[str] = frozenset(),
    empty_sections: frozenset[str] = frozenset(),
) -> Record:
    return Record(id=id, kind=kind, status=status, links=links, empty_sections=empty_sections)


def snapshot(*records: Record, pending: frozenset[str] = frozenset()) -> Snapshot:
    return Snapshot(records=records, pending=pending)


RESEARCH = Rule(role="researcher", kind="hypothesis", statuses=frozenset({"proposed"}))


def test_empty_snapshot_is_idle():
    result = decide([RESEARCH], snapshot())

    assert isinstance(result, Idle)
    assert len(result.reasons) == 1


def test_no_rules_is_idle_without_reasons():
    assert decide([], snapshot(record("H-1"))) == Idle(reasons=())


def test_single_eligible_record_is_targeted():
    assert decide([RESEARCH], snapshot(record("H-1"))) == Command("researcher", "H-1")


def test_record_of_other_kind_or_status_is_not_eligible():
    result = decide(
        [RESEARCH],
        snapshot(record("S-1", kind="solution"), record("H-1", status="accepted")),
    )

    assert isinstance(result, Idle)


def test_pending_target_is_skipped_for_the_next_one():
    result = decide([RESEARCH], snapshot(record("H-1"), record("H-2"), pending=frozenset({"H-1"})))

    assert result == Command("researcher", "H-2")


def test_earlier_rule_wins_over_later():
    decide_rule = Rule(role="decider", kind="solution", statuses=frozenset({"proposed"}))

    result = decide(
        [decide_rule, RESEARCH], snapshot(record("H-1"), record("S-9", kind="solution"))
    )

    assert result == Command("decider", "S-9")


def test_later_rule_runs_when_earlier_has_no_target():
    decide_rule = Rule(role="decider", kind="solution", statuses=frozenset({"proposed"}))

    assert decide([decide_rule, RESEARCH], snapshot(record("H-1"))) == Command("researcher", "H-1")


def test_ids_compare_in_natural_order():
    result = decide([RESEARCH], snapshot(record("H-10"), record("H-9"), record("H-2")))

    assert result == Command("researcher", "H-2")


def test_choice_does_not_depend_on_record_order():
    records = [record("H-02"), record("H-2"), record("G-3")]

    first = decide([RESEARCH], snapshot(*records))
    second = decide([RESEARCH], snapshot(*reversed(records)))

    assert first == second == Command("researcher", "G-3")


def test_empty_section_condition_requires_the_section_to_be_empty():
    rule = RESEARCH.model_copy(update={"conditions": (EmptySection(section="Evidence"),)})

    result = decide(
        [rule],
        snapshot(
            record("H-1", empty_sections=frozenset({"Summary"})),
            record("H-2", empty_sections=frozenset({"Evidence"})),
        ),
    )

    assert result == Command("researcher", "H-2")


def test_empty_section_false_leaves_rule_idle():
    rule = RESEARCH.model_copy(update={"conditions": (EmptySection(section="Evidence"),)})

    assert isinstance(decide([rule], snapshot(record("H-1"))), Idle)


NO_LIVE_SOLUTION = RESEARCH.model_copy(
    update={
        "conditions": (NoLinked(kind="solution", statuses=frozenset({"proposed", "accepted"})),)
    }
)


def test_no_linked_is_blocked_by_linked_record_in_listed_status():
    result = decide(
        [NO_LIVE_SOLUTION],
        snapshot(record("H-1"), record("S-1", kind="solution", links=frozenset({"H-1"}))),
    )

    assert isinstance(result, Idle)


def test_no_linked_ignores_linked_record_in_other_status():
    result = decide(
        [NO_LIVE_SOLUTION],
        snapshot(
            record("H-1"),
            record("S-1", kind="solution", status="rejected", links=frozenset({"H-1"})),
        ),
    )

    assert result == Command("researcher", "H-1")


def test_no_linked_ignores_linked_record_of_other_kind():
    result = decide(
        [NO_LIVE_SOLUTION],
        snapshot(record("H-1"), record("R-1", kind="risk", links=frozenset({"H-1"}))),
    )

    assert result == Command("researcher", "H-1")


def test_no_linked_only_looks_at_links_to_the_candidate():
    result = decide(
        [NO_LIVE_SOLUTION],
        snapshot(
            record("H-1"),
            record("H-2"),
            record("S-1", kind="solution", links=frozenset({"H-1"})),
        ),
    )

    assert result == Command("researcher", "H-2")


def test_idle_reasons_distinguish_missing_unmet_and_pending():
    evidence = RESEARCH.model_copy(update={"conditions": (EmptySection(section="Evidence"),)})
    rules = [
        Rule(role="decider", kind="solution", statuses=frozenset({"proposed"})),
        evidence.model_copy(update={"role": "evidence-hunter"}),
        RESEARCH,
    ]

    result = decide(
        rules, snapshot(record("H-1"), record("H-2"), pending=frozenset({"H-1", "H-2"}))
    )

    assert isinstance(result, Idle)
    missing, unmet, pending = result.reasons
    assert "decider" in missing and "no solution" in missing
    assert "evidence-hunter" in unmet and "conditions" in unmet
    assert "researcher" in pending and "2" in pending and "pending" in pending
    assert "pending" not in unmet
