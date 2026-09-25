"""What the pages show of an A2A task, as pure functions of the task's JSON."""

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

from golem.adapters.common import TERMINAL_STATES, state_word

# The reconciler's outcome for a proposed run: "Run <id> succeeded; merge request: <url>".
MERGE_REQUEST = re.compile(r"merge request: (\S+)\s*$")
# The message metadata key naming the agent a task was started for. A2A tasks do not keep the
# tenant; the metadata is the caller's own claim, used only to address its own task.
AGENT_METADATA = "golemAgent"


@dataclass(frozen=True)
class TaskView:
    id: str
    agent: str
    state: str
    goal: str
    message: str
    updated: str
    merge_request: str | None
    finished: bool


def merge_request_link(text: str) -> str | None:
    """The merge request URL in an outcome, only if it is http(s) with a host: anything else
    stays text, so a crafted outcome cannot become a javascript: or data: link."""
    match = MERGE_REQUEST.search(text)
    if match is None:
        return None
    url = urlsplit(match[1])
    return match[1] if url.scheme in ("http", "https") and url.hostname else None


def shown_time(timestamp: str) -> str:
    """An RFC 3339 status timestamp to the minute in UTC; anything else as it came."""
    try:
        moment = datetime.fromisoformat(timestamp)
    except ValueError:
        return timestamp
    if moment.tzinfo is None:
        return timestamp
    return moment.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")


def task_view(task: Any, agents: tuple[str, ...]) -> TaskView | None:
    if not isinstance(task, dict) or not isinstance(task.get("id"), str) or not task["id"]:
        return None
    status = task.get("status")
    status = status if isinstance(status, dict) else {}
    state = status.get("state") if isinstance(status.get("state"), str) else ""
    message = message_text(status.get("message"))
    first = _first_user_message(task.get("history"))
    metadata = first.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    agent = metadata.get(AGENT_METADATA)
    return TaskView(
        id=task["id"],
        agent=agent if agent in agents else agents[0],
        state=state_word(state) if state else "unknown",
        goal=message_text(first),
        message=message,
        updated=shown_time(str(status.get("timestamp") or "")),
        merge_request=merge_request_link(message),
        finished=state in TERMINAL_STATES,
    )


def message_text(message: Any) -> str:
    if not isinstance(message, dict) or not isinstance(message.get("parts"), list):
        return ""
    return "\n".join(
        part["text"]
        for part in message["parts"]
        if isinstance(part, dict) and isinstance(part.get("text"), str)
    )


def _first_user_message(history: Any) -> dict[str, Any]:
    if not isinstance(history, list):
        return {}
    for message in history:
        if isinstance(message, dict) and message.get("role") == "ROLE_USER":
            return message
    return {}
