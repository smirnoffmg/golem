"""The write servers' upstream calls (ADR 0015): a page edit, a service desk reply, a new issue
and a comment, against Confluence (Cloud v2 and Data Center), Jira Service Management and Jira
faked at their HTTP boundary, with the shapes of their REST documentation.

Each apply is idempotent per proposal: asked twice, it writes once."""

import asyncio
from typing import Any

import httpx
import pytest
from support.atlassian import DECIDED_MS, Confluence, Desk, Jira, body_of

from golem.mcp.atlassian import Deployment, UpstreamError
from golem.mcp.writes import (
    CLOCK_SKEW_MS,
    apply_comment,
    apply_issue,
    apply_page_edit,
    apply_reply,
    preview_page_edit,
    within_deadline,
)

PROPOSAL = "0c6f0d4e-6c43-4a8e-9a55-0f6c7b0f4a11"
LABEL = "golem-0c6f0d4e6c43"
DECIDED_AT = "2026-09-29T11:00:00+00:00"
PAGE = {"page_id": "123", "title": "Runbook", "version": 7, "body": "<p>New</p>"}
REPLY = {"request": "SD-12", "public": True, "text": "The export works again."}
ISSUE = {
    "action": "create",
    "project": "OPS",
    "issue_type": "Bug",
    "summary": "Disk grows 4% a day",
    "description": "Since the 20th.",
}
COMMENT = {"action": "comment", "issue": "OPS-7", "comment": "It grew again."}


# Confluence


def confluence_client(fake: Confluence) -> httpx.AsyncClient:
    base = (
        "https://acme.atlassian.net/wiki"
        if fake.deployment is Deployment.CLOUD
        else ("https://confluence.example.test")
    )
    return httpx.AsyncClient(transport=httpx.MockTransport(fake), base_url=base)


async def apply_page(fake: Confluence, payload: dict[str, Any] = PAGE) -> dict[str, Any]:
    return await apply_page_edit(
        confluence_client(fake),
        fake.deployment,
        frozenset({"OPS"}),
        PROPOSAL,
        payload,
        "user:bob",
    )


@pytest.mark.parametrize("deployment", [Deployment.CLOUD, Deployment.DATA_CENTER])
async def test_a_page_edit_writes_the_next_version_with_the_proposals_marker(
    deployment: Deployment,
) -> None:
    fake = Confluence(deployment)

    result = await apply_page(fake)

    assert result == {"state": "applied", "detail": "Page 123 is at version 8."}
    [put] = fake.puts()
    sent = body_of(put)
    assert sent["version"] == {"number": 8, "message": f"golem:{PROPOSAL} accepted by user:bob"}
    assert (sent["id"], sent["title"]) == ("123", "Runbook")
    if deployment is Deployment.CLOUD:
        assert sent["status"] == "current"
        assert sent["body"] == {"representation": "storage", "value": "<p>New</p>"}
    else:
        assert sent["type"] == "page"
        assert sent["body"] == {"storage": {"value": "<p>New</p>", "representation": "storage"}}
    assert fake.body == "<p>New</p>"


@pytest.mark.parametrize("deployment", [Deployment.CLOUD, Deployment.DATA_CENTER])
async def test_a_page_edit_asked_twice_writes_once(deployment: Deployment) -> None:
    fake = Confluence(deployment)

    first = await apply_page(fake)
    second = await apply_page(fake)

    assert (first["state"], second["state"]) == ("applied", "applied")
    assert len(fake.puts()) == 1


@pytest.mark.parametrize("deployment", [Deployment.CLOUD, Deployment.DATA_CENTER])
async def test_a_page_changed_since_the_role_read_it_is_stale_and_left_alone(
    deployment: Deployment,
) -> None:
    fake = Confluence(deployment, version=9, message="fixed a typo")

    result = await apply_page(fake)

    assert result == {
        "state": "stale",
        "detail": "Page 123 is at version 9; the proposal was made on version 7.",
    }
    assert fake.puts() == []


async def test_a_write_whose_answer_is_lost_is_read_back_and_found_applied() -> None:
    fake = Confluence(Deployment.DATA_CENTER, lose_put_answer=True)

    result = await apply_page(fake)

    assert result["state"] == "applied"
    assert len(fake.puts()) == 1


async def test_a_refused_write_on_an_unmoved_page_fails_with_the_reason() -> None:
    fake = Confluence(Deployment.CLOUD, put_status=400)

    result = await apply_page(fake)

    assert result["state"] == "failed"
    assert "400" in result["detail"]


async def test_a_page_outside_the_allowed_spaces_is_refused_before_any_write() -> None:
    fake = Confluence(Deployment.CLOUD, space="HR")

    with pytest.raises(UpstreamError, match="space HR is not one this server writes to"):
        await apply_page(fake)

    assert fake.puts() == []


async def test_a_preview_answers_the_live_page() -> None:
    fake = Confluence(Deployment.CLOUD, version=8, body="<p>Live</p>")

    live = await preview_page_edit(
        confluence_client(fake), Deployment.CLOUD, frozenset({"OPS"}), PAGE
    )

    assert live == {"title": "Runbook", "version": 8, "body": "<p>Live</p>"}
    assert fake.puts() == []


# Jira Service Management


def desk_client(fake: Desk) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.MockTransport(fake), base_url="https://jira.example.test"
    )


async def reply(fake: Desk, payload: dict[str, Any] = REPLY) -> dict[str, Any]:
    return await apply_reply(desk_client(fake), frozenset({"SD"}), PROPOSAL, payload, DECIDED_AT)


async def test_a_reply_is_posted_with_its_visibility_and_no_marker() -> None:
    fake = Desk()

    result = await reply(fake)

    assert result["state"] == "applied"
    [post] = fake.posts()
    assert body_of(post) == {"body": "The export works again.", "public": True}


async def test_a_reply_asked_twice_is_posted_once() -> None:
    fake = Desk()

    await reply(fake)
    second = await reply(fake)

    assert second["state"] == "applied"
    assert len(fake.posts()) == 1


async def test_the_same_text_from_someone_else_or_well_before_the_decision_is_not_this_reply() -> (
    None
):
    fake = Desk(me={"key": "golem", "name": "golem"})
    before = DECIDED_MS - CLOCK_SKEW_MS - 1
    fake.comments = [
        fake.comment(REPLY["text"], True, {"key": "ann", "name": "ann"}, DECIDED_MS + 1),
        fake.comment(REPLY["text"], True, {"key": "golem", "name": "golem"}, before),
        fake.comment(REPLY["text"], False, {"key": "golem", "name": "golem"}, DECIDED_MS + 1),
    ]

    result = await reply(fake)

    assert result["state"] == "applied"
    assert len(fake.posts()) == 1


async def test_a_reply_stamped_by_a_jira_clock_behind_the_database_is_still_this_reply() -> None:
    # The apply posted it a second after the decision, Jira's clock is 3 s behind Postgres's,
    # and the 201 was lost: asked again, the reply must be found, not posted twice.
    fake = Desk()
    fake.comments = [fake.comment(REPLY["text"], True, fake.me, DECIDED_MS - 2_000)]

    result = await reply(fake)

    assert result["state"] == "applied"
    assert fake.posts() == []


async def test_a_reply_whose_post_fails_and_is_not_found_fails() -> None:
    fake = Desk(post_status=403)

    result = await reply(fake)

    assert result == {
        "state": "failed",
        "detail": "Jira Service Management answered 403: refused",
    }


async def test_a_reply_to_a_desk_not_allowed_is_refused_before_any_call() -> None:
    fake = Desk()

    with pytest.raises(UpstreamError, match="project HR is not one this server writes to"):
        await reply(fake, {**REPLY, "request": "HR-3"})

    assert fake.requests == []


# Jira


def jira_client(fake: Jira) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.MockTransport(fake), base_url="https://jira.example.test"
    )


async def new_issue(fake: Jira, target: str | None = "alert-3f2a") -> dict[str, Any]:
    return await apply_issue(
        jira_client(fake), Deployment.CLOUD, frozenset({"OPS"}), PROPOSAL, ISSUE, target
    )


async def test_a_new_issue_carries_the_proposals_label_and_the_targets() -> None:
    fake = Jira()

    result = await new_issue(fake)

    assert result == {"state": "applied", "detail": "Created OPS-100."}
    fields = fake.issues["OPS-100"]
    assert fields == {
        "project": {"key": "OPS"},
        "issuetype": {"name": "Bug"},
        "summary": "Disk grows 4% a day",
        "description": "Since the 20th.",
        "labels": [LABEL, "golem-alert-3f2a"],
    }


async def test_a_new_issue_asked_twice_is_created_once() -> None:
    fake = Jira()

    await new_issue(fake, target=None)
    second = await new_issue(fake, target=None)

    assert second == {"state": "applied", "detail": "OPS-100 was created already."}
    assert len(fake.posts()) == 1
    assert fake.issues["OPS-100"]["labels"] == [LABEL]


async def test_data_center_searches_on_its_own_path() -> None:
    fake = Jira()

    await apply_issue(
        jira_client(fake), Deployment.DATA_CENTER, frozenset({"OPS"}), PROPOSAL, ISSUE, None
    )

    assert fake.requests[0].url.path == "/rest/api/2/search"


async def test_a_refused_create_fails_with_the_reason() -> None:
    fake = Jira(create_status=400)

    result = await new_issue(fake)

    assert result["state"] == "failed"
    assert "refused" in result["detail"]


async def test_an_issue_in_a_project_not_allowed_is_refused_before_any_call() -> None:
    fake = Jira()

    with pytest.raises(UpstreamError, match="project HR is not one this server writes to"):
        await apply_issue(
            jira_client(fake),
            Deployment.CLOUD,
            frozenset({"OPS"}),
            PROPOSAL,
            {**ISSUE, "project": "HR"},
            None,
        )

    assert fake.requests == []


async def test_a_comment_ends_with_the_proposals_marker_and_is_posted_once() -> None:
    fake = Jira()

    first = await apply_comment(jira_client(fake), frozenset({"OPS"}), PROPOSAL, COMMENT)
    second = await apply_comment(jira_client(fake), frozenset({"OPS"}), PROPOSAL, COMMENT)

    assert (first["state"], second["state"]) == ("applied", "applied")
    [posted] = fake.comments["OPS-7"]
    assert posted == {"body": f"It grew again.\n\nGolem proposal {PROPOSAL}"}


async def test_a_comment_that_only_quotes_the_marker_is_not_this_one() -> None:
    fake = Jira(comments={"OPS-7": [{"body": f"Golem proposal {PROPOSAL} was wrong, see below"}]})

    result = await apply_comment(jira_client(fake), frozenset({"OPS"}), PROPOSAL, COMMENT)

    assert result["state"] == "applied"
    assert len(fake.comments["OPS-7"]) == 2


# The whole of one apply


async def test_an_apply_that_outlives_its_deadline_fails_and_writes_nothing_more() -> None:
    started = asyncio.Event()

    async def hanging() -> dict[str, str]:
        started.set()
        await asyncio.Event().wait()
        raise AssertionError("never")

    result = await within_deadline(hanging(), seconds=0.05)

    assert started.is_set()
    assert result == {"state": "failed", "detail": "the apply did not finish in 0.05 s"}


async def test_an_apply_within_its_deadline_answers_as_it_did() -> None:
    async def quick() -> dict[str, str]:
        return {"state": "applied", "detail": "done"}

    assert await within_deadline(quick(), seconds=1) == {"state": "applied", "detail": "done"}
