"""What a run proposes when a person decides it in Golem and the platform applies it (ADR 0015).

The runtime writes ``golem-proposal.json`` at its branch's root: the kind, the kind's fields and
the paths of its body files. The reconciler reads the same file at the branch's head commit and
checks it again, since it comes from an untrusted Job. Both turn it into the payload with
``payload_of``; the payload is what a person decides on, what the write server applies, and what
the proposal's digest binds a token to.
"""

import hashlib
import re
from collections.abc import Callable, Mapping
from pathlib import PurePosixPath
from typing import Any

PROPOSAL_FILE = "golem-proposal.json"
MERGE_REQUEST = "merge_request"
WIKI_EDIT = "wiki_edit"
DESK_REPLY = "desk_reply"
TRACKER_ISSUE = "tracker_issue"
# The kinds a person decides in Golem and the platform applies, and the write group that
# applies each; a merge request is merged.
WRITE_GROUPS = {WIKI_EDIT: "wiki.write", DESK_REPLY: "desk.write", TRACKER_ISSUE: "tracker.write"}
APPLIED_KINDS = frozenset(WRITE_GROUPS)

MAX_PAGE_BODY = 200_000
MAX_REPLY = 30_000
MAX_DESCRIPTION = 30_000
MAX_COMMENT = 30_000
MAX_TITLE = 255
MAX_SUMMARY = 255
MAX_ISSUE_TYPE = 64
MAX_PATH = 512
# The largest integer a JSON number carries exactly (RFC 8785 3.2.2.3 serializes as IEEE 754).
MAX_EXACT_INT = 2**53 - 1

PAGE_ID = re.compile(r"^[0-9]{1,20}$")
PROJECT = re.compile(r"^[A-Z][A-Z0-9_]{0,19}$")
ISSUE_KEY = re.compile(r"^[A-Z][A-Z0-9_]{0,19}-[1-9][0-9]{0,9}$")

ReadFile = Callable[[str], str]


class ProposalError(ValueError):
    pass


def payload_of(
    kind: str, manifest: object, read: ReadFile, *, under: str | None = None
) -> dict[str, Any]:
    """The payload ``manifest`` proposes for ``kind``, its body files read with ``read``;
    raises ProposalError for anything off. ``under`` requires the body files in that directory.
    """
    if not isinstance(manifest, dict):
        raise ProposalError(f"{PROPOSAL_FILE} must hold a JSON object")
    if manifest.get("kind") != kind:
        raise ProposalError(f"{PROPOSAL_FILE} names kind {manifest.get('kind')!r}, not {kind!r}")
    fields = {key: value for key, value in manifest.items() if key != "kind"}
    body = _Body(read, under)
    match kind:
        case "wiki_edit":
            return _wiki_edit(fields, body)
        case "desk_reply":
            return _desk_reply(fields, body)
        case "tracker_issue":
            return _tracker_issue(fields, body)
    raise ProposalError(f"proposals of kind {kind!r} have no {PROPOSAL_FILE}")


def _wiki_edit(fields: dict[str, Any], body: "_Body") -> dict[str, Any]:
    _only(fields, "page_id", "title", "version", "body_file")
    return {
        "page_id": _matching(fields, "page_id", PAGE_ID),
        "title": _line(fields, "title", MAX_TITLE),
        "version": _count(fields, "version"),
        "body": body.text(fields, "body_file", MAX_PAGE_BODY),
    }


def _desk_reply(fields: dict[str, Any], body: "_Body") -> dict[str, Any]:
    _only(fields, "request", "public", "text_file")
    public = fields.get("public")
    if not isinstance(public, bool):
        # No default: whether the customer reads it is the one thing that must be said.
        raise ProposalError("public must be true (the customer reads it) or false (internal)")
    return {
        "request": _matching(fields, "request", ISSUE_KEY),
        "public": public,
        "text": body.text(fields, "text_file", MAX_REPLY),
    }


def _tracker_issue(fields: dict[str, Any], body: "_Body") -> dict[str, Any]:
    match fields.get("action"):
        case "create":
            _only(fields, "action", "project", "issue_type", "summary", "description_file")
            return {
                "action": "create",
                "project": _matching(fields, "project", PROJECT),
                "issue_type": _line(fields, "issue_type", MAX_ISSUE_TYPE),
                "summary": _line(fields, "summary", MAX_SUMMARY),
                "description": body.text(fields, "description_file", MAX_DESCRIPTION),
            }
        case "comment":
            _only(fields, "action", "issue", "comment_file")
            return {
                "action": "comment",
                "issue": _matching(fields, "issue", ISSUE_KEY),
                "comment": body.text(fields, "comment_file", MAX_COMMENT),
            }
    raise ProposalError("action must be 'create' (a new issue) or 'comment'")


def _only(fields: Mapping[str, Any], *names: str) -> None:
    unknown = sorted(set(fields) - set(names))
    if unknown:
        raise ProposalError(f"unknown field {unknown[0]!r}")


def _matching(fields: Mapping[str, Any], name: str, pattern: re.Pattern[str]) -> str:
    value = fields.get(name)
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise ProposalError(f"{name} must match {pattern.pattern}")
    return value


def _line(fields: Mapping[str, Any], name: str, limit: int) -> str:
    value = fields.get(name)
    if not isinstance(value, str) or not value.strip() or len(value) > limit or "\n" in value:
        raise ProposalError(f"{name} must be one line of 1 to {limit} characters")
    return value


def _count(fields: Mapping[str, Any], name: str) -> int:
    value = fields.get(name)
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MAX_EXACT_INT:
        raise ProposalError(f"{name} must be a positive integer")
    return value


class _Body:
    def __init__(self, read: ReadFile, under: str | None) -> None:
        self._read = read
        self._under = PurePosixPath(under.strip("/")) if under else None

    def text(self, fields: Mapping[str, Any], name: str, limit: int) -> str:
        path = fields.get(name)
        if not isinstance(path, str) or not _inside(path):
            raise ProposalError(f"{name} must name a file inside the repository")
        if self._under is not None and self._under not in PurePosixPath(path).parents:
            raise ProposalError(f"{name} must name a file under {self._under}/")
        try:
            text = self._read(path)
        except (FileNotFoundError, KeyError) as error:
            raise ProposalError(f"{name} {path!r} is missing") from error
        if len(text) > limit:
            # Refused, never cut: a cut body would be applied as the whole of it.
            raise ProposalError(f"{name} {path!r} holds {len(text)} characters; at most {limit}")
        if not text.strip():
            raise ProposalError(f"{name} {path!r} is empty")
        return text


def _inside(path: str) -> bool:
    parsed = PurePosixPath(path)
    return (
        0 < len(path) <= MAX_PATH
        and not parsed.is_absolute()
        and ".." not in parsed.parts
        and parsed.name not in ("", ".")
    )


def summary_of(kind: str, payload: Mapping[str, Any]) -> str:
    """One line naming what a person decides on."""
    match kind:
        case "wiki_edit":
            return f"Edit page {payload['title']}"
        case "desk_reply":
            return f"Reply to {payload['request']}"
        case "tracker_issue" if payload.get("action") == "comment":
            return f"Comment on {payload['issue']}"
        case "tracker_issue":
            return f"New {payload['project']} issue: {payload['summary']}"
    return kind


def payload_digest(payload: Mapping[str, Any]) -> str:
    """SHA-256 of the payload's RFC 8785 form: what a proposal token binds a decision to."""
    return hashlib.sha256(canonical_json(payload).encode()).hexdigest()


def canonical_json(value: object) -> str:
    """RFC 8785 (JCS) for the values a payload holds: objects with string keys, arrays,
    strings, booleans, null and integers a double carries exactly. Anything else is refused
    rather than approximated, so two readers of one payload never disagree on its digest."""
    match value:
        case None:
            return "null"
        case bool():
            return "true" if value else "false"
        case int():
            if abs(value) > MAX_EXACT_INT:
                raise ProposalError(f"{value} is beyond what a JSON number carries exactly")
            return str(value)
        case str():
            return _string(value)
        case list() | tuple():
            return "[" + ",".join(canonical_json(item) for item in value) + "]"
        case dict():
            if not all(isinstance(key, str) for key in value):
                raise ProposalError("object keys must be strings")
            # 3.2.3: members sorted by their names' UTF-16 code units.
            keys = sorted(value, key=lambda key: key.encode("utf-16-be"))
            return "{" + ",".join(f"{_string(k)}:{canonical_json(value[k])}" for k in keys) + "}"
    raise ProposalError(f"{type(value).__name__} has no canonical JSON form here")


SHORT_ESCAPES = {'"': '\\"', "\\": "\\\\", "\b": "\\b", "\f": "\\f", "\n": "\\n", "\r": "\\r"}
SHORT_ESCAPES["\t"] = "\\t"


def _string(text: str) -> str:
    try:
        text.encode("utf-8")
    except UnicodeEncodeError as error:
        raise ProposalError("a string holds a lone surrogate") from error
    escaped = "".join(
        SHORT_ESCAPES.get(char) or (f"\\u{ord(char):04x}" if ord(char) < 0x20 else char)
        for char in text
    )
    return f'"{escaped}"'
