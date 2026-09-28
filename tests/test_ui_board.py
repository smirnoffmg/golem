"""What the board shows of an A2A task: cards, columns and the delta cursor (ADR 0018), as pure
functions of the task's JSON."""

from datetime import UTC, datetime
from typing import Any

import pytest

from golem.ui.board import (
    GOAL_CHARS,
    MESSAGE_CHARS,
    card,
    column,
    cursor_after,
    detail,
    parse_cursor,
    safe_link,
)


def task(
    state: str = "TASK_STATE_WORKING",
    *,
    goal: str = "fix the test",
    message: str = "",
    timestamp: str = "2026-09-28T10:00:00.123456Z",
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    status: dict[str, Any] = {"state": state, "timestamp": timestamp}
    if message:
        status["message"] = {"role": "ROLE_AGENT", "parts": [{"text": message}]}
    return {
        "id": "task-1",
        "contextId": "ctx-1",
        "status": status,
        "history": [
            {"role": "ROLE_USER", "messageId": "ui:n", "parts": [{"text": goal}]},
            {"role": "ROLE_AGENT", "messageId": "a", "parts": [{"text": "on it"}]},
        ],
        "metadata": metadata or {},
    }


def proposal(state: str = "pending", url: str = "https://gitlab.example.test/p/-/mr/7") -> Any:
    return {"id": "p-1", "kind": "merge_request", "state": state, "url": url}


@pytest.mark.parametrize(
    ("state", "proposal_state", "expected"),
    [
        ("submitted", None, "in_progress"),
        ("working", None, "in_progress"),
        ("input-required", None, "waiting"),
        ("auth-required", None, "waiting"),
        ("completed", "pending", "review"),
        ("completed", "accepted", "review"),
        ("completed", "failed", "review"),
        ("completed", "applied", "archive"),
        ("completed", "rejected", "archive"),
        ("completed", "stale", "archive"),
        ("completed", None, "archive"),
        ("failed", None, "failed"),
        ("rejected", None, "failed"),
        ("canceled", None, "archive"),
        ("something-new", None, "in_progress"),
    ],
)
def test_the_column_follows_the_task_and_its_proposal(
    state: str, proposal_state: str | None, expected: str
) -> None:
    assert column(state, proposal_state) == expected


def test_a_card_shows_the_goal_the_status_message_and_the_proposal() -> None:
    shown = card(
        task(
            "TASK_STATE_COMPLETED",
            goal="write the H-2 evidence",
            message="Run 1 succeeded",
            metadata={"golemAgent": "discovery", "golemProposal": proposal()},
        )
    )

    assert shown == {
        "id": "task-1",
        "state": "completed",
        "column": "review",
        "goal": "write the H-2 evidence",
        "message": "Run 1 succeeded",
        "updated": "2026-09-28T10:00:00.123456Z",
        "proposal": proposal(),
    }


def test_long_texts_are_cut() -> None:
    shown = card(task(goal="g" * 5000, message="m" * 5000))

    assert shown is not None
    assert shown["goal"] == "g" * GOAL_CHARS
    assert shown["message"] == "m" * MESSAGE_CHARS


@pytest.mark.parametrize(
    "value",
    [
        "not a proposal",
        {"id": "p-1", "kind": "merge_request"},
        {"id": 7, "kind": "merge_request", "state": "pending", "url": ""},
    ],
)
def test_a_malformed_proposal_is_no_proposal(value: Any) -> None:
    shown = card(task("TASK_STATE_COMPLETED", metadata={"golemProposal": value}))

    assert shown is not None
    assert shown["proposal"] is None
    assert shown["column"] == "archive"


@pytest.mark.parametrize(
    ("url", "link"),
    [
        (
            "https://gitlab.example.test/p/-/merge_requests/7",
            "https://gitlab.example.test/p/-/merge_requests/7",
        ),
        ("http://gitlab.local/mr/1", "http://gitlab.local/mr/1"),
        ("javascript:alert(1)", None),
        ("data:text/html,<b>x</b>", None),
        ("https://", None),
        ("", None),
    ],
)
def test_only_an_http_url_is_a_link(url: str, link: str | None) -> None:
    assert safe_link(url) == link
    shown = card(task("TASK_STATE_COMPLETED", metadata={"golemProposal": proposal(url=url)}))
    assert shown is not None and shown["proposal"] is not None
    assert shown["proposal"]["url"] == link


@pytest.mark.parametrize("value", [None, "text", {"id": ""}, {"status": {}}])
def test_something_that_is_not_a_task_has_no_card(value: Any) -> None:
    assert card(value) is None


def test_the_detail_adds_the_conversation_and_the_artifacts() -> None:
    shown = task(goal="look closer")
    shown["artifacts"] = [{"name": "report", "parts": [{"text": "found it"}]}]

    detailed = detail(shown)

    assert detailed is not None
    assert detailed["goal"] == "look closer"
    assert detailed["history"] == [
        {"role": "user", "text": "look closer"},
        {"role": "agent", "text": "on it"},
    ]
    assert detailed["artifacts"] == [{"name": "report", "text": "found it"}]


def test_the_cursor_is_the_newest_update_minus_thirty_seconds() -> None:
    cards = [
        {"updated": "2026-09-28T10:00:00Z"},
        {"updated": "2026-09-28T10:05:00.123456789Z"},
        {"updated": "not a time"},
    ]

    assert cursor_after(cards, None) == "2026-09-28T10:04:30.123456Z"


def test_without_updates_the_cursor_stays() -> None:
    assert cursor_after([], "2026-09-28T10:00:00Z") == "2026-09-28T10:00:00Z"


def test_the_cursor_never_moves_back() -> None:
    cards = [{"updated": "2026-09-28T09:00:00Z"}]

    assert cursor_after(cards, "2026-09-28T10:00:00Z") == "2026-09-28T10:00:00Z"


@pytest.mark.parametrize(
    ("text", "parsed"),
    [
        ("2026-09-28T10:04:30.123456Z", datetime(2026, 9, 28, 10, 4, 30, 123456, tzinfo=UTC)),
        ("2026-09-28T10:04:30Z", datetime(2026, 9, 28, 10, 4, 30, tzinfo=UTC)),
        ("2026-09-28T10:04:30", None),
        ("2026-09-28T12:04:30+02:00", None),
        ("yesterday", None),
        ("", None),
        ("2026-09-28T10:04:30Z" + "0" * 300, None),
    ],
)
def test_only_a_cursor_the_board_issued_is_accepted(text: str, parsed: datetime | None) -> None:
    assert parse_cursor(text) == parsed
