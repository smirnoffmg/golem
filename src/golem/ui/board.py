"""What the board shows of an A2A task (ADR 0018), as pure functions of the task's JSON."""

import re
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

GOAL_CHARS = 280
MESSAGE_CHARS = 500
PROPOSAL_METADATA = "golemProposal"
PROCESS_METADATA = "golemProcess"
NEEDS_REASON = "needs_reason"
PROCESS_TEXTS = ("state", "stage")
PROCESS_COUNTS = ("index", "count", "attempt", "maxAttempts", "staleReruns")
# Open proposals wait for a person: pending, being applied, or refused by the target.
OPEN_PROPOSAL_STATES = frozenset({"pending", "accepted", "failed"})
# A poll that read the list just before a task's update committed still sees it next time.
CURSOR_OVERLAP = timedelta(seconds=30)
# RFC 3339 in UTC as protobuf's JSON writes a Timestamp: up to nine fractional digits, "Z".
TIMESTAMP = re.compile(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d{1,9}))?Z$")

COLUMNS = {
    "submitted": "in_progress",
    "working": "in_progress",
    "input-required": "waiting",
    "auth-required": "waiting",
    "failed": "failed",
    "rejected": "failed",
    "canceled": "archive",
}


def column(state: str, proposal_state: str | None, process_state: str | None = None) -> str:
    if process_state is not None and state in ("submitted", "working"):
        # A process's task stays working while its stages run; where it waits is the
        # process's (ADR 0019).
        if process_state == NEEDS_REASON:
            return "waiting"
        return "review" if proposal_state in OPEN_PROPOSAL_STATES else "in_progress"
    if state == "completed":
        return "review" if proposal_state in OPEN_PROPOSAL_STATES else "archive"
    # A state the board does not know yet is shown as still going, not hidden.
    return COLUMNS.get(state, "in_progress")


def safe_link(url: str) -> str | None:
    """Only http(s) with a host: a crafted URL cannot become a javascript: or data: link."""
    parts = urlsplit(url)
    return url if parts.scheme in ("http", "https") and parts.hostname else None


def card(task: Any) -> dict[str, Any] | None:
    if not isinstance(task, dict) or not isinstance(task.get("id"), str) or not task["id"]:
        return None
    status = task.get("status")
    status = status if isinstance(status, dict) else {}
    raw_state = status.get("state")
    state = state_name(raw_state) if isinstance(raw_state, str) and raw_state else "unknown"
    metadata = task.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    shown_process = process_of(metadata)
    if shown_process is None:
        shown_proposal = proposal_of(metadata)
    else:
        # golemProposal on a process task can outlive its stage; the process view cannot.
        shown_proposal = proposal_of(
            {PROPOSAL_METADATA: metadata[PROCESS_METADATA].get("proposal")}
        )
    timestamp = status.get("timestamp")
    return {
        "id": task["id"],
        "state": state,
        "column": column(
            state,
            shown_proposal["state"] if shown_proposal else None,
            shown_process["state"] if shown_process else None,
        ),
        "goal": message_text(first_user_message(task.get("history")))[:GOAL_CHARS],
        "message": message_text(status.get("message"))[:MESSAGE_CHARS],
        "updated": timestamp if isinstance(timestamp, str) else "",
        "proposal": shown_proposal,
        "process": shown_process,
    }


def detail(task: Any) -> dict[str, Any] | None:
    shown = card(task)
    if shown is None:
        return None
    history = task.get("history")
    artifacts = task.get("artifacts")
    return shown | {
        "history": [
            {"role": "user" if m.get("role") == "ROLE_USER" else "agent", "text": message_text(m)}
            for m in (history if isinstance(history, list) else [])
            if isinstance(m, dict)
        ],
        "artifacts": [
            {"name": a["name"] if isinstance(a.get("name"), str) else "", "text": message_text(a)}
            for a in (artifacts if isinstance(artifacts, list) else [])
            if isinstance(a, dict)
        ],
    }


def state_name(state: str) -> str:
    """``TASK_STATE_INPUT_REQUIRED`` as A2A's documents write it: ``input-required``."""
    return state.removeprefix("TASK_STATE_").lower().replace("_", "-")


def proposal_of(metadata: Mapping[str, Any]) -> dict[str, Any] | None:
    value = metadata.get(PROPOSAL_METADATA)
    if not isinstance(value, dict):
        return None
    fields = {name: value.get(name) for name in ("id", "kind", "state", "url")}
    if not all(isinstance(v, str) for v in fields.values()):
        return None
    if not fields["id"] or not fields["kind"] or not fields["state"]:
        return None
    return fields | {"url": safe_link(str(fields["url"]))}


def process_of(metadata: Mapping[str, Any]) -> dict[str, Any] | None:
    value = metadata.get(PROCESS_METADATA)
    if not isinstance(value, dict):
        return None
    texts = {name: value.get(name) for name in PROCESS_TEXTS}
    counts = {name: count_of(value.get(name)) for name in PROCESS_COUNTS}
    reason = value.get("reason")
    if not all(isinstance(v, str) and v for v in texts.values()):
        return None
    if any(v is None for v in counts.values()):
        return None
    if reason is not None and not isinstance(reason, str):
        return None
    return texts | counts | {"reason": reason[:MESSAGE_CHARS] if reason else None}


def count_of(value: Any) -> int | None:
    """A whole number; task metadata is a protobuf Struct, which reads every number back as
    a double."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return int(value) if value == int(value) else None


def message_text(message: Any) -> str:
    if not isinstance(message, dict) or not isinstance(message.get("parts"), list):
        return ""
    return "\n".join(
        part["text"]
        for part in message["parts"]
        if isinstance(part, dict) and isinstance(part.get("text"), str)
    )


def first_user_message(history: Any) -> dict[str, Any]:
    if not isinstance(history, list):
        return {}
    for message in history:
        if isinstance(message, dict) and message.get("role") == "ROLE_USER":
            return message
    return {}


def parse_cursor(text: str) -> datetime | None:
    match = TIMESTAMP.fullmatch(text)
    if match is None:
        return None
    try:
        moment = datetime.fromisoformat(match[1]).replace(tzinfo=UTC)
    except ValueError:
        return None
    # Python keeps microseconds; the nanoseconds a Timestamp may carry are cut, not rounded.
    fraction = (match[2] or "").ljust(6, "0")[:6]
    return moment.replace(microsecond=int(fraction))


def format_cursor(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def cursor_after(cards: Iterable[Mapping[str, Any]], previous: str | None) -> str | None:
    """The newest status timestamp among ``cards`` minus the overlap, never before
    ``previous``: the next delta asks for every task updated since."""
    seen = [parse_cursor(str(shown.get("updated", ""))) for shown in cards]
    moments = [moment for moment in seen if moment is not None]
    if not moments:
        return previous
    candidate = max(moments) - CURSOR_OVERLAP
    earlier = parse_cursor(previous) if previous else None
    if earlier is not None and earlier >= candidate:
        return previous
    return format_cursor(candidate)
