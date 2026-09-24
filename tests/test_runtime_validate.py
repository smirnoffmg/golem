from pathlib import Path

from golem.catalog import Role
from golem.runtime.lead import Record
from golem.runtime.snapshot import Located
from golem.runtime.validate import (
    ContextState,
    broken_context,
    changed_statuses,
    no_changes,
    outside_writes,
    read_state,
    target_untouched,
    validate,
)

RESEARCHER = Role(name="researcher", writes="hypotheses/")


def located(source: str, rid: str, status: str = "proposed") -> Located:
    record = Record(
        id=rid, kind="hypothesis", status=status, links=frozenset(), empty_sections=frozenset()
    )
    return Located(source=source, record=record)


def state(*records: Located, error: str | None = None) -> ContextState:
    return ContextState(records=records, error=error)


def test_paths_under_writes_are_allowed():
    assert outside_writes(("hypotheses/H-2.md", "hypotheses/new/H-9.md"), "hypotheses/") == ()


def test_paths_outside_writes_are_each_named():
    violations = outside_writes(
        ("hypotheses/H-2.md", "solutions/S-1.md", "README.md"), "hypotheses/"
    )

    assert violations == (
        "solutions/S-1.md is outside the role's writes directory hypotheses/",
        "README.md is outside the role's writes directory hypotheses/",
    )


def test_writes_without_trailing_slash_is_a_directory_not_a_prefix():
    assert outside_writes(("hypotheses-old/H-1.md",), "hypotheses") == (
        "hypotheses-old/H-1.md is outside the role's writes directory hypotheses",
    )


def test_writes_dot_allows_the_whole_tree():
    assert outside_writes(("a/b.md", "c.md"), ".") == ()


def test_a_valid_context_has_no_violation():
    assert broken_context(state(located("hypotheses/H-2.md", "H-2"))) == ()


def test_a_broken_context_reports_the_record_error():
    broken = state(error="hypotheses/H-2.md: front matter has no closing '---' line")

    assert broken_context(broken) == (
        "the context no longer builds: hypotheses/H-2.md: front matter has no closing '---' line",
    )


def test_read_state_of_a_valid_tree(tmp_path: Path):
    (tmp_path / "H-1.md").write_text("---\nid: H-1\nkind: hypothesis\nstatus: proposed\n---\n")

    result = read_state(tmp_path)

    assert result.error is None
    assert [item.record.id for item in result.records] == ["H-1"]


def test_read_state_catches_dangling_links(tmp_path: Path):
    text = "---\nid: H-1\nkind: hypothesis\nstatus: proposed\nlinks: [X-9]\n---\n"
    (tmp_path / "H-1.md").write_text(text)

    result = read_state(tmp_path)

    assert result.error == "H-1.md: links to unknown record ids: X-9"


def test_read_state_catches_duplicate_ids(tmp_path: Path):
    for name in ("a.md", "b.md"):
        (tmp_path / name).write_text("---\nid: H-1\nkind: hypothesis\nstatus: proposed\n---\n")

    assert "already used" in (read_state(tmp_path).error or "")


def test_read_state_catches_unparseable_records(tmp_path: Path):
    (tmp_path / "H-1.md").write_text("---\nid: H-1\nkind: hypothesis\n---\n")

    assert "missing required key 'status'" in (read_state(tmp_path).error or "")


def test_unchanged_statuses_have_no_violation():
    before = state(located("h/H-1.md", "H-1"), located("h/H-2.md", "H-2"))
    after = state(located("h/H-1.md", "H-1"), located("h/H-2.md", "H-2"), located("h/N.md", "N"))

    assert changed_statuses(before, after) == ()


def test_every_status_change_is_named():
    before = state(located("h/H-1.md", "H-1"), located("h/H-2.md", "H-2", "validated"))
    after = state(located("h/H-1.md", "H-1", "validated"), located("h/H-2.md", "H-2", "rejected"))

    assert changed_statuses(before, after) == (
        "H-1 changed status from 'proposed' to 'validated'; status changes are human decisions",
        "H-2 changed status from 'validated' to 'rejected'; status changes are human decisions",
    )


def test_no_changes_is_a_violation():
    assert no_changes(()) == ("the role changed no files",)
    assert no_changes(("a.md",)) == ()


def test_target_must_change():
    after = state(located("hypotheses/H-2.md", "H-2"), located("hypotheses/H-3.md", "H-3"))

    assert target_untouched(("hypotheses/H-3.md",), "H-2", "hypotheses/H-2.md", after) == (
        "neither the target H-2 (hypotheses/H-2.md) nor a changed record linking to it changed",
    )
    assert target_untouched(("hypotheses/H-2.md",), "H-2", "hypotheses/H-2.md", after) == ()


def linking(source: str, rid: str, target: str) -> Located:
    record = Record(
        id=rid,
        kind="solution",
        status="proposed",
        links=frozenset({target}),
        empty_sections=frozenset(),
    )
    return Located(source=source, record=record)


def test_a_changed_record_linking_to_the_target_counts_as_working_on_it():
    after = state(located("hypotheses/H-3.md", "H-3"), linking("solutions/S-2.md", "S-2", "H-3"))

    assert target_untouched(("solutions/S-2.md",), "H-3", "hypotheses/H-3.md", after) == ()


def test_an_unchanged_record_linking_to_the_target_does_not_count():
    after = state(
        located("hypotheses/H-3.md", "H-3"),
        linking("solutions/S-1.md", "S-1", "H-3"),
        located("solutions/S-2.md", "S-2"),
    )

    assert target_untouched(("solutions/S-2.md",), "H-3", "hypotheses/H-3.md", after) != ()


def test_validate_collects_every_violation():
    before = state(located("hypotheses/H-2.md", "H-2"), located("solutions/S-1.md", "S-1"))
    after = state(
        located("hypotheses/H-2.md", "H-2"), located("solutions/S-1.md", "S-1", "accepted")
    )

    violations = validate(
        role=RESEARCHER,
        target_id="H-2",
        target_source="hypotheses/H-2.md",
        changed=("solutions/S-1.md",),
        before=before,
        after=after,
    )

    assert violations == (
        "solutions/S-1.md is outside the role's writes directory hypotheses/",
        "S-1 changed status from 'proposed' to 'accepted'; status changes are human decisions",
        "neither the target H-2 (hypotheses/H-2.md) nor a changed record linking to it changed",
    )


def test_validate_passes_a_clean_change():
    records = state(located("hypotheses/H-2.md", "H-2"))

    assert (
        validate(
            role=RESEARCHER,
            target_id="H-2",
            target_source="hypotheses/H-2.md",
            changed=("hypotheses/H-2.md",),
            before=records,
            after=records,
        )
        == ()
    )
