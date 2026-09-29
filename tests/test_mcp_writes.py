"""The write servers' upstream calls (ADR 0015): a page edit, a service desk reply, a new issue
and a comment, against Confluence (Cloud v2 and Data Center), Jira Service Management and Jira
faked at their HTTP boundary, with the shapes of their REST documentation.

Each apply is idempotent per proposal: asked twice, it writes once."""

import json
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest

from golem.mcp.atlassian import Deployment, UpstreamError
from golem.mcp.writes import (
    apply_comment,
    apply_issue,
    apply_page_edit,
    apply_reply,
    preview_page_edit,
)

PROPOSAL = "0c6f0d4e-6c43-4a8e-9a55-0f6c7b0f4a11"
LABEL = "golem-0c6f0d4e6c43"
DECIDED_AT = "2026-09-29T11:00:00+00:00"
DECIDED_MS = 1_790_679_600_000
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


def body_of(request: httpx.Request) -> Any:
    return json.loads(request.content)


# Confluence


@dataclass
class Confluence:
    deployment: Deployment
    version: int = 7
    message: str = ""
    title: str = "Runbook"
    body: str = "<p>Old</p>"
    space: str = "OPS"
    put_status: int = 200
    # The page is written, and the answer is lost on the way back.
    lose_put_answer: bool = False
    requests: list[httpx.Request] = field(default_factory=list)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if self.deployment is Deployment.CLOUD:
            if path == "/wiki/api/v2/spaces/9001":
                return httpx.Response(200, json={"id": "9001", "key": self.space})
            if path == "/wiki/api/v2/pages/123" and request.method == "GET":
                assert request.url.params["body-format"] == "storage"
                return httpx.Response(200, json=self._cloud_page())
            if path == "/wiki/api/v2/pages/123" and request.method == "PUT":
                return self._put(body_of(request), cloud=True)
        else:
            if path == "/rest/api/content/123" and request.method == "GET":
                assert set(request.url.params["expand"].split(",")) >= {
                    "body.storage",
                    "version",
                    "space",
                }
                return httpx.Response(200, json=self._dc_page())
            if path == "/rest/api/content/123" and request.method == "PUT":
                return self._put(body_of(request), cloud=False)
        return httpx.Response(404, json={"message": "no such resource"})

    def _cloud_page(self) -> dict[str, Any]:
        return {
            "id": "123",
            "status": "current",
            "title": self.title,
            "spaceId": "9001",
            "version": {"number": self.version, "message": self.message},
            "body": {"storage": {"representation": "storage", "value": self.body}},
        }

    def _dc_page(self) -> dict[str, Any]:
        return {
            "id": "123",
            "type": "page",
            "title": self.title,
            "space": {"key": self.space},
            "version": {"number": self.version, "message": self.message},
            "body": {"storage": {"representation": "storage", "value": self.body}},
        }

    def _put(self, sent: dict[str, Any], *, cloud: bool) -> httpx.Response:
        if self.put_status != 200:
            return httpx.Response(self.put_status, json={"message": "refused"})
        if sent["version"]["number"] != self.version + 1:
            return httpx.Response(409, json={"message": "version conflict"})
        self.version = sent["version"]["number"]
        self.message = sent["version"].get("message", "")
        self.title = sent["title"]
        self.body = sent["body"]["value"] if cloud else sent["body"]["storage"]["value"]
        if self.lose_put_answer:
            return httpx.Response(502, text="bad gateway")
        return httpx.Response(200, json=self._cloud_page() if cloud else self._dc_page())

    def puts(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.method == "PUT"]


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


@dataclass
class Desk:
    comments: list[dict[str, Any]] = field(default_factory=list)
    # Cloud names an account by accountId, Data Center by key and name.
    me: dict[str, Any] = field(default_factory=lambda: {"accountId": "557058:golem"})
    post_status: int = 201
    requests: list[httpx.Request] = field(default_factory=list)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path == "/rest/api/2/myself":
            return httpx.Response(200, json=self.me)
        if path == "/rest/servicedeskapi/request/SD-12/comment" and request.method == "GET":
            start = int(request.url.params.get("start", "0"))
            limit = int(request.url.params.get("limit", "50"))
            page = self.comments[start : start + limit]
            return httpx.Response(
                200,
                json={
                    "start": start,
                    "limit": limit,
                    "size": len(page),
                    "isLastPage": start + limit >= len(self.comments),
                    "values": page,
                },
            )
        if path == "/rest/servicedeskapi/request/SD-12/comment" and request.method == "POST":
            if self.post_status != 201:
                return httpx.Response(self.post_status, json={"errorMessage": "refused"})
            sent = body_of(request)
            comment = self.comment(sent["body"], sent["public"], self.me, DECIDED_MS + 5_000)
            self.comments.append(comment)
            return httpx.Response(201, json=comment)
        return httpx.Response(404, json={"errorMessage": "no such resource"})

    def comment(
        self, text: str, public: bool, author: dict[str, Any], at_ms: int
    ) -> dict[str, Any]:
        return {
            "id": str(1000 + len(self.comments)),
            "body": text,
            "public": public,
            "author": author,
            "created": {"epochMillis": at_ms, "iso8601": "2026-09-29T11:00:05+0000"},
        }

    def posts(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.method == "POST"]


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


async def test_the_same_text_from_someone_else_or_before_the_decision_is_not_this_reply() -> None:
    fake = Desk(me={"key": "golem", "name": "golem"})
    fake.comments = [
        fake.comment(REPLY["text"], True, {"key": "ann", "name": "ann"}, DECIDED_MS + 1),
        fake.comment(REPLY["text"], True, {"key": "golem", "name": "golem"}, DECIDED_MS - 1),
        fake.comment(REPLY["text"], False, {"key": "golem", "name": "golem"}, DECIDED_MS + 1),
    ]

    result = await reply(fake)

    assert result["state"] == "applied"
    assert len(fake.posts()) == 1


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


@dataclass
class Jira:
    issues: dict[str, dict[str, Any]] = field(default_factory=dict)
    comments: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    create_status: int = 201
    requests: list[httpx.Request] = field(default_factory=list)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path in ("/rest/api/2/search/jql", "/rest/api/2/search"):
            label = re.fullmatch(r'labels = "([^"]+)"', request.url.params["jql"])
            assert label is not None
            found = [
                {"key": key}
                for key, fields in self.issues.items()
                if label.group(1) in fields["labels"]
            ]
            return httpx.Response(200, json={"issues": found, "isLast": True})
        if path == "/rest/api/2/issue" and request.method == "POST":
            if self.create_status != 201:
                return httpx.Response(self.create_status, json={"errorMessages": ["refused"]})
            key = f"OPS-{len(self.issues) + 100}"
            self.issues[key] = body_of(request)["fields"]
            return httpx.Response(201, json={"id": "1", "key": key, "self": "x"})
        match = re.fullmatch(r"/rest/api/2/issue/([A-Z]+-\d+)/comment", path)
        if match and request.method == "GET":
            found = self.comments.get(match.group(1), [])
            start = int(parse_qs(request.url.query.decode()).get("startAt", ["0"])[0])
            return httpx.Response(
                200, json={"comments": found[start:], "startAt": start, "total": len(found)}
            )
        if match and request.method == "POST":
            self.comments.setdefault(match.group(1), []).append(body_of(request))
            return httpx.Response(201, json={"id": "10"})
        return httpx.Response(404, json={"errorMessages": ["no such resource"]})

    def posts(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.method == "POST"]


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
