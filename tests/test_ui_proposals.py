"""What the board shows of proposals and reports (ADR 0018): the edge's answers checked field by
field, and a page edit's diff taken over the storage format's blocks, as pure functions."""

from typing import Any

import pytest

from golem.ui.proposals import (
    FOLD_CONTEXT,
    page_diff,
    page_lines,
    proposal_card,
    proposal_detail,
    report_card,
    report_detail,
    review_counts,
)

PROPOSAL_ID = "0c6f0d4e-6c43-4a8e-9a55-0f6c7b0f4a11"


def listed(**fields: Any) -> dict[str, Any]:
    return {
        "id": PROPOSAL_ID,
        "taskId": "task-1",
        "agent": "docs",
        "kind": "wiki_edit",
        "state": "pending",
        "summary": "Edit page Runbook",
        "url": None,
        "owner": "user:bob",
        "createdAt": "2026-09-29T10:00:00+00:00",
        "decidedBy": None,
        "decidedAt": None,
    } | fields


def test_a_page_is_split_into_lines_before_each_block() -> None:
    storage = (
        "<h1>Runbook</h1><p>Restart the <b>exporter</b>.</p><ul><li>one</li><li>two</li></ul>"
        '<table><tr><td>a</td></tr></table><ac:structured-macro ac:name="info"/>'
    )

    assert page_lines(storage) == [
        "<h1>Runbook</h1>",
        "<p>Restart the <b>exporter</b>.</p><ul>",
        "<li>one</li>",
        "<li>two</li></ul>",
        "<table>",
        "<tr><td>a</td></tr></table>",
        '<ac:structured-macro ac:name="info"/>',
    ]


def test_a_tag_that_only_starts_like_a_block_does_not_split() -> None:
    assert page_lines("<pre>x</pre><table-of-contents/><p>y</p>") == [
        "<pre>x</pre><table-of-contents/>",
        "<p>y</p>",
    ]


def test_a_changed_paragraph_is_one_deleted_and_one_inserted_line() -> None:
    live = "<p>one</p><p>two</p><p>three</p>"
    proposed = "<p>one</p><p>TWO</p><p>three</p>"

    assert page_diff(live, proposed) == [
        {"op": "equal", "lines": ["<p>one</p>"]},
        {"op": "delete", "lines": ["<p>two</p>"]},
        {"op": "insert", "lines": ["<p>TWO</p>"]},
        {"op": "equal", "lines": ["<p>three</p>"]},
    ]


def test_long_unchanged_runs_are_folded_around_the_change() -> None:
    before = "".join(f"<p>{n}</p>" for n in range(10))
    after_ = "".join(f"<p>{n}</p>" for n in range(10, 20))
    live = before + "<p>old</p>" + after_
    proposed = before + "<p>new</p>" + after_

    diff = page_diff(live, proposed)

    assert diff == [
        {"op": "fold", "count": 10 - FOLD_CONTEXT},
        {"op": "equal", "lines": [f"<p>{n}</p>" for n in range(10 - FOLD_CONTEXT, 10)]},
        {"op": "delete", "lines": ["<p>old</p>"]},
        {"op": "insert", "lines": ["<p>new</p>"]},
        {"op": "equal", "lines": [f"<p>{n}</p>" for n in range(10, 10 + FOLD_CONTEXT)]},
        {"op": "fold", "count": 10 - FOLD_CONTEXT},
    ]


def test_a_long_unchanged_run_between_changes_keeps_context_on_both_sides() -> None:
    middle = [f"<p>{n}</p>" for n in range(20)]
    live = "<p>a</p>" + "".join(middle) + "<p>b</p>"
    proposed = "<p>A</p>" + "".join(middle) + "<p>B</p>"

    diff = page_diff(live, proposed)

    assert diff[2] == {"op": "equal", "lines": middle[:FOLD_CONTEXT]}
    assert diff[3] == {"op": "fold", "count": 20 - 2 * FOLD_CONTEXT}
    assert diff[4] == {"op": "equal", "lines": middle[-FOLD_CONTEXT:]}


def test_an_unchanged_run_of_six_lines_is_shown_whole() -> None:
    six = "".join(f"<p>{n}</p>" for n in range(6))

    diff = page_diff("<p>a</p>" + six, "<p>A</p>" + six)

    assert diff[-1] == {"op": "equal", "lines": [f"<p>{n}</p>" for n in range(6)]}


def test_the_same_page_is_one_folded_run() -> None:
    page = "".join(f"<p>{n}</p>" for n in range(30))

    assert page_diff(page, page) == [{"op": "fold", "count": 30}]


def test_a_listed_proposal_becomes_a_card_waiting_for_review() -> None:
    shown = proposal_card(listed())

    assert shown == {
        "id": PROPOSAL_ID,
        "taskId": "task-1",
        "agent": "docs",
        "kind": "wiki_edit",
        "state": "pending",
        "column": "review",
        "summary": "Edit page Runbook",
        "url": None,
        "owner": "user:bob",
        "createdAt": "2026-09-29T10:00:00+00:00",
        "decidedBy": None,
        "decidedAt": None,
    }


@pytest.mark.parametrize(("state", "column"), [("applied", "archive"), ("failed", "review")])
def test_a_proposal_card_goes_where_its_state_puts_it(state: str, column: str) -> None:
    shown = proposal_card(listed(state=state))

    assert shown is not None
    assert shown["column"] == column


def test_a_merge_request_link_that_is_not_http_is_dropped() -> None:
    shown = proposal_card(listed(kind="merge_request", url="javascript:alert(1)"))

    assert shown is not None
    assert shown["url"] is None


@pytest.mark.parametrize(
    "item",
    [
        None,
        "x",
        listed(id=7),
        listed(id=""),
        listed(kind=None),
        listed(state=None),
        listed(summary=None),
        listed(owner=3),
    ],
)
def test_a_malformed_listed_proposal_is_no_card(item: Any) -> None:
    assert proposal_card(item) is None


def test_the_summary_is_cut_to_one_short_line() -> None:
    shown = proposal_card(listed(summary="x" * 1000))

    assert shown is not None
    assert len(shown["summary"]) == 280


def test_a_page_edit_is_shown_as_a_diff_against_the_live_page() -> None:
    body = listed() | {
        "payload": {"page_id": "123", "title": "Runbook", "version": 7, "body": "<p>New</p>"},
        "target": "docs-runbook",
        "reason": None,
        "detail": None,
        "report": "Found the page out of date.",
        "stage": False,
        "live": {"title": "Runbook", "version": 7, "body": "<p>Old</p>"},
    }

    shown = proposal_detail(body)

    assert shown is not None
    assert shown["diff"] == [
        {"op": "delete", "lines": ["<p>Old</p>"]},
        {"op": "insert", "lines": ["<p>New</p>"]},
    ]
    assert shown["live"] == {"title": "Runbook", "version": 7}
    assert shown["payload"] == body["payload"]
    assert (shown["report"], shown["stage"], shown["target"]) == (
        "Found the page out of date.",
        False,
        "docs-runbook",
    )


def test_a_page_edit_whose_live_page_could_not_be_read_has_no_diff() -> None:
    body = listed() | {
        "payload": {"page_id": "123", "title": "Runbook", "version": 7, "body": "<p>New</p>"},
        "live": None,
        "liveError": "space HR is not one this server writes to",
    }

    shown = proposal_detail(body)

    assert shown is not None
    assert (shown["diff"], shown["live"]) == (None, None)
    assert shown["liveError"] == "space HR is not one this server writes to"


def test_a_reply_is_shown_as_its_text_and_its_audience() -> None:
    payload = {"request": "SD-12", "public": True, "text": "The export works again."}
    shown = proposal_detail(listed(kind="desk_reply") | {"payload": payload, "detail": "x"})

    assert shown is not None
    assert (shown["payload"], shown["diff"], shown["detail"]) == (payload, None, "x")


def test_a_payload_that_is_not_an_object_is_no_proposal() -> None:
    assert proposal_detail(listed() | {"payload": "<script>"}) is None


def test_a_report_card_and_its_text_are_checked() -> None:
    item = {
        "taskId": "task-9",
        "agent": "investigator",
        "target": "alert-3f2a",
        "completedAt": "2026-09-29T09:00:00+00:00",
        "summary": "Disk grows 4% a day",
    }

    assert report_card(item) == item
    assert report_card(item | {"taskId": 1}) is None
    assert report_detail(item | {"text": "Disk grows 4% a day.\nSince the 20th."}) == {
        k: v for k, v in item.items() if k != "summary"
    } | {"text": "Disk grows 4% a day.\nSince the 20th."}
    assert report_detail(item | {"text": None}) is None


def test_review_counts_are_per_agent() -> None:
    items = [listed(agent="docs"), listed(agent="desk"), listed(agent="docs"), "junk"]

    assert review_counts(items) == {"desk": 1, "docs": 2}
