"""Confluence, Jira Service Management and Jira faked at their HTTP boundary, with the shapes of
their REST documentation: what the write servers' tests and the demo stand apply proposals to
(ADR 0015). One page (id 123, space OPS), one request (SD-12) and project OPS."""

import json
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs

import httpx

from golem.mcp.atlassian import Deployment

# When a reply the fake desk posts was created: after the tests' decision time.
DECIDED_MS = 1_790_679_600_000


def body_of(request: httpx.Request) -> Any:
    return json.loads(request.content)


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


@dataclass
class Jira:
    issues: dict[str, dict[str, Any]] = field(default_factory=dict)
    comments: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    create_status: int = 201
    # The issue is created, and the answer is lost on the way back: Jira answers this status
    # instead, or the connection drops.
    lose_create_answer: int | None = None
    drop_create_answer: bool = False
    # Cloud's search is eventually consistent: a new issue is left out of this many searches.
    search_lag: int = 0
    requests: list[httpx.Request] = field(default_factory=list)
    unseen: dict[str, int] = field(default_factory=dict)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path in ("/rest/api/2/search/jql", "/rest/api/2/search"):
            label = re.fullmatch(r'labels = "([^"]+)"', request.url.params["jql"])
            assert label is not None
            found = [
                {"key": key}
                for key, fields in self.issues.items()
                if label.group(1) in fields["labels"] and self._seen(key)
            ]
            return httpx.Response(200, json={"issues": found, "isLast": True})
        if path == "/rest/api/2/issue" and request.method == "POST":
            if self.create_status != 201:
                return httpx.Response(self.create_status, json={"errorMessages": ["refused"]})
            key = f"OPS-{len(self.issues) + 100}"
            self.issues[key] = body_of(request)["fields"]
            self.unseen[key] = self.search_lag
            if self.drop_create_answer:
                raise httpx.RemoteProtocolError("connection dropped", request=request)
            if self.lose_create_answer is not None:
                return httpx.Response(self.lose_create_answer, text="bad gateway")
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

    def _seen(self, key: str) -> bool:
        if self.unseen.get(key, 0) > 0:
            self.unseen[key] -= 1
            return False
        return True

    def posts(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.method == "POST"]
