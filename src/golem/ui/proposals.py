"""What the board shows of proposals and reports (ADR 0018), as pure functions of the edge's JSON.

Everything here was written in an untrusted Job or read from Confluence: it stays text, and the
board renders it as text nodes, never as HTML.
"""

import difflib
import re
from collections import Counter
from collections.abc import Iterable
from typing import Any

from golem.ui.board import GOAL_CHARS, OPEN_PROPOSAL_STATES, safe_link

# A changed paragraph is one changed line, not one changed page.
BLOCK_START = re.compile(r"(?=<(?:p|h[1-6]|li|tr|table|ac:structured-macro)(?=[\s>/]))")
# Unchanged runs longer than twice this are folded, keeping this many lines next to a change.
FOLD_CONTEXT = 3
SUMMARY_TEXTS = ("id", "taskId", "agent", "kind", "state", "summary", "owner", "createdAt")
OPTIONAL_TEXTS = ("url", "decidedBy", "decidedAt")
REPORT_TEXTS = ("taskId", "agent", "completedAt")


def page_lines(storage: str) -> list[str]:
    return [
        line for chunk in BLOCK_START.split(storage) for line in chunk.splitlines() if line.strip()
    ]


def page_diff(live: str, proposed: str) -> list[dict[str, Any]]:
    """``live`` against ``proposed`` as runs of equal, deleted and inserted lines; a replaced
    run is its deletion then its insertion."""
    before, after = page_lines(live), page_lines(proposed)
    matcher = difflib.SequenceMatcher(None, before, after, autojunk=False)
    opcodes = matcher.get_opcodes()
    runs: list[dict[str, Any]] = []
    for index, (tag, i1, i2, j1, j2) in enumerate(opcodes):
        if tag == "equal":
            first, last = index == 0, index == len(opcodes) - 1
            runs.extend(folded(before[i1:i2], keep_head=not first, keep_tail=not last))
            continue
        if tag in ("replace", "delete"):
            runs.append({"op": "delete", "lines": before[i1:i2]})
        if tag in ("replace", "insert"):
            runs.append({"op": "insert", "lines": after[j1:j2]})
    return runs


def folded(lines: list[str], *, keep_head: bool, keep_tail: bool) -> list[dict[str, Any]]:
    """An unchanged run, folded when it is long: the lines next to a change stay visible."""
    if len(lines) <= 2 * FOLD_CONTEXT:
        return [{"op": "equal", "lines": lines}]
    head = lines[:FOLD_CONTEXT] if keep_head else []
    tail = lines[-FOLD_CONTEXT:] if keep_tail else []
    runs: list[dict[str, Any]] = []
    if head:
        runs.append({"op": "equal", "lines": head})
    runs.append({"op": "fold", "count": len(lines) - len(head) - len(tail)})
    if tail:
        runs.append({"op": "equal", "lines": tail})
    return runs


def proposal_card(item: Any) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    texts = {name: item.get(name) for name in SUMMARY_TEXTS}
    if not all(isinstance(value, str) for value in texts.values()) or not texts["id"]:
        return None
    optional = {name: item.get(name) for name in OPTIONAL_TEXTS}
    if not all(value is None or isinstance(value, str) for value in optional.values()):
        return None
    url = optional["url"]
    return {
        "id": texts["id"],
        "taskId": texts["taskId"],
        "agent": texts["agent"],
        "kind": texts["kind"],
        "state": texts["state"],
        "column": "review" if texts["state"] in OPEN_PROPOSAL_STATES else "archive",
        "summary": str(texts["summary"])[:GOAL_CHARS],
        "url": safe_link(url) if url else None,
        "owner": texts["owner"],
        "createdAt": texts["createdAt"],
        "decidedBy": optional["decidedBy"],
        "decidedAt": optional["decidedAt"],
    }


def proposal_cards(items: Any) -> list[dict[str, Any]]:
    shown = (proposal_card(item) for item in (items if isinstance(items, list) else []))
    return [each for each in shown if each is not None]


def proposal_detail(body: Any) -> dict[str, Any] | None:
    """One proposal to decide on: its payload as the Job wrote it, and for a page edit the diff
    against the page as Confluence holds it now."""
    shown = proposal_card(body)
    payload = body.get("payload") if isinstance(body, dict) else None
    if shown is None or not isinstance(payload, dict):
        return None
    live = body.get("live")
    diff = None
    live_page = None
    if isinstance(live, dict) and isinstance(live.get("body"), str):
        live_page = {"title": live.get("title"), "version": live.get("version")}
        if isinstance(payload.get("body"), str):
            diff = page_diff(live["body"], payload["body"])
    return shown | {
        "payload": payload,
        "target": text_or_none(body.get("target")),
        "reason": text_or_none(body.get("reason")),
        "detail": text_or_none(body.get("detail")),
        "report": text_or_none(body.get("report")),
        "stage": body.get("stage") is True,
        "live": live_page,
        "liveError": text_or_none(body.get("liveError")),
        "diff": diff,
    }


def report_card(item: Any) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    texts = {name: item.get(name) for name in (*REPORT_TEXTS, "summary")}
    target = item.get("target")
    if not all(isinstance(value, str) for value in texts.values()) or not (
        target is None or isinstance(target, str)
    ):
        return None
    return {**texts, "target": target, "summary": str(texts["summary"])[:GOAL_CHARS]}


def report_cards(items: Any) -> list[dict[str, Any]]:
    shown = (report_card(item) for item in (items if isinstance(items, list) else []))
    return [each for each in shown if each is not None]


def report_detail(body: Any) -> dict[str, Any] | None:
    if not isinstance(body, dict) or not isinstance(body.get("text"), str):
        return None
    shown = report_card(body | {"summary": ""})
    if shown is None:
        return None
    del shown["summary"]
    return shown | {"text": body["text"]}


def review_counts(items: Iterable[Any]) -> dict[str, int]:
    counted = Counter(card["agent"] for card in map(proposal_card, items) if card is not None)
    return dict(sorted(counted.items()))


def text_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) else None
