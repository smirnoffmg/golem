"""Builds the lead's snapshot from an agent's context repository.

A context repository is a Git working tree of Markdown records. The format:

- One record per Markdown file (``*.md``) anywhere in the tree. Directories whose name starts
  with a dot are skipped, and so is any file that does not open with front matter (a README,
  for example), so the tree can hold ordinary documentation next to records.
- Front matter is YAML between a first line ``---`` and the next line ``---``. It must be a
  mapping with ``id``, ``kind`` and ``status`` (strings) and may have ``links``, a list of ids of
  other records. Other keys are allowed and ignored.
- An ``id`` matches ``^[A-Za-z][A-Za-z0-9-]*$`` and is unique across the repository; every link
  names an existing id.
- The body is split into sections by level-2 headings (``## Name``); text before the first one
  belongs to no section. A section is empty when, with HTML comments removed, only whitespace
  remains. Authors leave comments as prompts, so a template section stays empty until a role
  writes into it. A nested ``###`` heading is content.

The context repository's CI runs ``build_snapshot`` too, so every ``RecordError`` starts with
the file path relative to the repository root.
"""

import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from golem.runtime.lead import Record, Snapshot

ID = re.compile(r"[A-Za-z][A-Za-z0-9-]*")
DELIMITER = "---"
SECTION_HEADING = re.compile(r"^##[ \t]+(.+?)[ \t]*$", re.MULTILINE)
HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)


class RecordError(ValueError):
    pass


@dataclass(frozen=True)
class Located:
    source: str
    record: Record


def build_snapshot(root: Path, pending: frozenset[str]) -> Snapshot:
    located = tuple(read_records(root))
    check_unique_ids(located)
    check_links(located)
    return Snapshot(records=tuple(item.record for item in located), pending=pending)


def read_records(root: Path) -> Iterable[Located]:
    for path in markdown_files(root):
        text = path.read_text(encoding="utf-8")
        if has_front_matter(text):
            source = path.relative_to(root).as_posix()
            yield Located(source=source, record=parse_record(text, source))


def markdown_files(root: Path) -> tuple[Path, ...]:
    return tuple(
        sorted(
            path
            for path in root.rglob("*.md")
            if path.is_file() and not is_hidden(path.relative_to(root))
        )
    )


def is_hidden(relative: Path) -> bool:
    return any(part.startswith(".") for part in relative.parts)


def has_front_matter(text: str) -> bool:
    return text.splitlines()[:1] == [DELIMITER]


def parse_record(text: str, source: str) -> Record:
    front, body = split_front_matter(text, source)
    fields = load_front_matter(front, source)
    return Record(
        id=record_id(fields, source),
        kind=required_string(fields, "kind", source),
        status=required_string(fields, "status", source),
        links=record_links(fields, source),
        empty_sections=empty_sections(body),
    )


def split_front_matter(text: str, source: str) -> tuple[str, str]:
    if not has_front_matter(text):
        raise RecordError(f"{source}: no front matter: the first line must be {DELIMITER!r}")
    lines = text.splitlines(keepends=True)
    for index, line in enumerate(lines[1:], start=1):
        if line.rstrip("\r\n") == DELIMITER:
            return "".join(lines[1:index]), "".join(lines[index + 1 :])
    raise RecordError(f"{source}: front matter has no closing {DELIMITER!r} line")


def load_front_matter(front: str, source: str) -> dict[str, Any]:
    try:
        fields = yaml.safe_load(front)
    except yaml.YAMLError as error:
        raise RecordError(f"{source}: front matter is not valid YAML: {error}") from error
    if not isinstance(fields, dict):
        raise RecordError(f"{source}: front matter must be a mapping of keys to values")
    return fields


def required_string(fields: dict[str, Any], key: str, source: str) -> str:
    if key not in fields:
        raise RecordError(f"{source}: front matter is missing required key {key!r}")
    value = fields[key]
    if not isinstance(value, str) or not value.strip():
        raise RecordError(f"{source}: {key!r} must be a non-empty string, got {value!r}")
    return value


def record_id(fields: dict[str, Any], source: str) -> str:
    if "id" not in fields:
        raise RecordError(f"{source}: front matter is missing required key 'id'")
    value = fields["id"]
    if not isinstance(value, str) or not ID.fullmatch(value):
        raise RecordError(f"{source}: id {value!r} must match {ID.pattern!r}")
    return value


def record_links(fields: dict[str, Any], source: str) -> frozenset[str]:
    links = fields.get("links", [])
    if links is None:
        return frozenset()
    if not isinstance(links, list) or not all(isinstance(link, str) for link in links):
        raise RecordError(f"{source}: 'links' must be a list of record ids, got {links!r}")
    return frozenset(links)


def empty_sections(body: str) -> frozenset[str]:
    return frozenset(name for name, content in sections(body) if is_blank(content))


def sections(body: str) -> tuple[tuple[str, str], ...]:
    headings = list(SECTION_HEADING.finditer(body))
    ends = [heading.start() for heading in headings[1:]] + [len(body)]
    return tuple(
        (heading.group(1), body[heading.end() : end])
        for heading, end in zip(headings, ends, strict=False)
    )


def is_blank(content: str) -> bool:
    return not HTML_COMMENT.sub("", content).strip()


def check_unique_ids(located: tuple[Located, ...]) -> None:
    first_seen: dict[str, str] = {}
    for item in located:
        rid = item.record.id
        if rid in first_seen:
            raise RecordError(f"{item.source}: id {rid!r} is already used by {first_seen[rid]}")
        first_seen[rid] = item.source


def check_links(located: tuple[Located, ...]) -> None:
    known = {item.record.id for item in located}
    for item in located:
        missing = sorted(item.record.links - known)
        if missing:
            raise RecordError(f"{item.source}: links to unknown record ids: {', '.join(missing)}")
