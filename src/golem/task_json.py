"""Reading A2A tasks as JSON-RPC answers carry them, for callers that hold no SDK types."""

from typing import Any


def status_text(status: dict[str, Any]) -> str:
    message = status.get("message")
    parts = message.get("parts") if isinstance(message, dict) else None
    if not isinstance(parts, list):
        return ""
    return " ".join(
        p["text"] for p in parts if isinstance(p, dict) and isinstance(p.get("text"), str)
    )
