from pathlib import Path

import pytest

from golem.catalog import load_catalog
from golem.runtime.lead import Command, Record, Snapshot, decide
from golem.runtime.snapshot import (
    RecordError,
    build_snapshot,
    empty_sections,
    markdown_files,
    parse_record,
)

EXAMPLES = Path(__file__).parent.parent / "examples"


def document(front: str, body: str = "") -> str:
    return f"---\n{front}---\n{body}"


def write(root: Path, relative: str, text: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def test_parse_record_reads_front_matter_and_body():
    text = document(
        "id: H-1\nkind: hypothesis\nstatus: proposed\nlinks: [E-1]\n",
        "## Problem\nUsers leave.\n\n## Evidence\n",
    )

    assert parse_record(text, "h.md") == Record(
        id="H-1",
        kind="hypothesis",
        status="proposed",
        links=frozenset({"E-1"}),
        empty_sections=frozenset({"Evidence"}),
    )


def test_links_are_optional():
    record = parse_record(document("id: H-1\nkind: hypothesis\nstatus: proposed\n"), "h.md")

    assert record.links == frozenset()


def test_unknown_keys_are_ignored():
    text = document("id: H-1\nkind: hypothesis\nstatus: proposed\nowner: anna\ntags: [x]\n")

    assert parse_record(text, "h.md").id == "H-1"


@pytest.mark.parametrize("missing", ["id", "kind", "status"])
def test_missing_required_field_names_file_and_field(missing):
    fields = {"id": "H-1", "kind": "hypothesis", "status": "proposed"}
    del fields[missing]
    front = "".join(f"{key}: {value}\n" for key, value in fields.items())

    with pytest.raises(RecordError, match=rf"h\.md: .*{missing}"):
        parse_record(document(front), "h.md")


def test_malformed_yaml_names_file():
    with pytest.raises(RecordError, match=r"h\.md: .*YAML"):
        parse_record(document("id: [H-1\nkind: hypothesis\n"), "h.md")


def test_front_matter_must_be_a_mapping():
    with pytest.raises(RecordError, match=r"h\.md: .*mapping"):
        parse_record(document("- id\n- kind\n"), "h.md")


def test_unclosed_front_matter_is_an_error():
    with pytest.raises(RecordError, match=r"h\.md: .*closing"):
        parse_record("---\nid: H-1\nkind: hypothesis\nstatus: proposed\n", "h.md")


def test_text_without_front_matter_is_an_error():
    with pytest.raises(RecordError, match=r"h\.md: .*front matter"):
        parse_record("# Notes\n", "h.md")


@pytest.mark.parametrize("bad_id", ["1-H", "H_1", "H 1", "", "-H"])
def test_id_must_match_pattern(bad_id):
    with pytest.raises(RecordError, match=r"h\.md: .*id"):
        parse_record(document(f"id: '{bad_id}'\nkind: hypothesis\nstatus: proposed\n"), "h.md")


def test_non_string_id_is_rejected():
    with pytest.raises(RecordError, match=r"h\.md: .*id"):
        parse_record(document("id: 12\nkind: hypothesis\nstatus: proposed\n"), "h.md")


def test_links_must_be_a_list_of_strings():
    with pytest.raises(RecordError, match=r"h\.md: .*links"):
        parse_record(document("id: H-1\nkind: hypothesis\nstatus: proposed\nlinks: H-2\n"), "h.md")


def test_section_with_only_whitespace_is_empty():
    assert empty_sections("## Evidence\n   \n\t\n## Problem\ntext\n") == frozenset({"Evidence"})


def test_section_with_only_html_comments_is_empty():
    body = "## Evidence\n<!-- Where did you see it?\n  Link sources. -->\n<!-- more -->\n"

    assert empty_sections(body) == frozenset({"Evidence"})


def test_text_next_to_a_comment_is_content():
    assert empty_sections("## Evidence\n<!-- prompt --> Interviews: 5 of 8.\n") == frozenset()


def test_nested_heading_counts_as_content():
    assert empty_sections("## Evidence\n### Interviews\n") == frozenset()


def test_text_before_first_section_is_not_a_section():
    assert empty_sections("Intro text.\n\n## Evidence\n") == frozenset({"Evidence"})


def test_last_section_at_end_of_body_is_empty():
    assert empty_sections("## Problem\nx\n## Evidence") == frozenset({"Evidence"})


def test_level_one_and_three_headings_do_not_start_sections():
    assert empty_sections("# Title\n### Sub\n") == frozenset()


def test_markdown_files_walks_nested_dirs_and_skips_dot_dirs_and_other_files(tmp_path):
    write(tmp_path, "a.md", "")
    write(tmp_path, "hypotheses/deep/b.md", "")
    write(tmp_path, ".git/c.md", "")
    write(tmp_path, "x/.drafts/d.md", "")
    write(tmp_path, "notes.txt", "")

    found = markdown_files(tmp_path)

    assert [p.relative_to(tmp_path).as_posix() for p in found] == [
        "a.md",
        "hypotheses/deep/b.md",
    ]


def test_build_snapshot_skips_files_without_front_matter(tmp_path):
    write(tmp_path, "README.md", "# Context\n")
    write(tmp_path, "h/H-1.md", document("id: H-1\nkind: hypothesis\nstatus: proposed\n"))

    snapshot = build_snapshot(tmp_path, frozenset({"H-1"}))

    assert [r.id for r in snapshot.records] == ["H-1"]
    assert snapshot.pending == frozenset({"H-1"})


def test_parse_error_names_the_relative_path(tmp_path):
    write(tmp_path, "h/H-1.md", document("id: H-1\nkind: hypothesis\n"))

    with pytest.raises(RecordError, match=r"^h/H-1\.md: "):
        build_snapshot(tmp_path, frozenset())


def test_duplicate_ids_name_both_files(tmp_path):
    front = "id: H-1\nkind: hypothesis\nstatus: proposed\n"
    write(tmp_path, "a/one.md", document(front))
    write(tmp_path, "b/two.md", document(front))

    with pytest.raises(RecordError, match=r"a/one\.md.*b/two\.md|b/two\.md.*a/one\.md") as error:
        build_snapshot(tmp_path, frozenset())
    assert "H-1" in str(error.value)


def test_dangling_link_names_file_and_missing_id(tmp_path):
    write(tmp_path, "s.md", document("id: S-1\nkind: solution\nstatus: proposed\nlinks: [H-9]\n"))

    with pytest.raises(RecordError, match=r"s\.md: .*H-9"):
        build_snapshot(tmp_path, frozenset())


@pytest.mark.parametrize(
    ("pending", "expected"),
    [
        (frozenset(), Command(role="researcher", target_id="H-2")),
        (frozenset({"H-2"}), Command(role="designer", target_id="H-3")),
        (frozenset({"H-2", "H-3"}), Command(role="reviewer", target_id="S-1")),
    ],
)
def test_example_context_drives_the_discovery_rules(pending, expected):
    catalog = load_catalog(EXAMPLES / "discovery" / "agent.yaml")
    snapshot = build_snapshot(EXAMPLES / "context", pending)

    assert decide(catalog.rules, snapshot) == expected


def test_snapshot_is_a_lead_snapshot(tmp_path):
    assert build_snapshot(tmp_path, frozenset()) == Snapshot(records=(), pending=frozenset())
