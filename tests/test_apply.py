"""The task service's client of the write servers (ADR 0015), against a FastMCP server that
stands in for one: the calls it makes, the proposal token it presents, and what it makes of the
answers. The write servers themselves are built separately."""

import json
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any

import httpx
import jwt
import pytest
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.types import ASGIApp, Receive, Scope, Send

from golem.proposal_payload import payload_digest
from golem.proposal_token import verify
from golem.run_token import SigningKey, public_jwks
from golem.tasks.apply import (
    UNKNOWN_SECONDS,
    ApplyUnavailable,
    McpApplier,
    WriteServer,
    apply_call,
)
from golem.tasks.ports import Applied, LivePage, ProposalDetail, ProposalSummary

KEY = SigningKey.generate("k1")
NOW = 1_800_000_000
RESOURCE = "https://wiki-write.golem-system.svc/mcp"
PAGE = {"page_id": "123", "title": "Home", "version": 7, "body": "<p>New</p>"}


def detail(kind: str, payload: dict[str, Any], state: str = "accepted") -> ProposalDetail:
    return ProposalDetail(
        summary=ProposalSummary(
            id="0c6f0d4e-6c43-4a8e-9a55-0f6c7b0f4a11",
            task_id="task-1",
            agent="docs",
            kind=kind,
            state=state,
            summary="",
            url=None,
            owner="service:jira",
            created_at="2026-09-29T10:00:00+00:00",
            decided_by="user:bob",
            decided_at="2026-09-29T11:00:00+00:00",
        ),
        payload=payload,
        digest=payload_digest(payload),
        target="alert-1",
        reason=None,
        detail=None,
        report=None,
        stage=False,
    )


@dataclass
class FakeWriteServer:
    """Answers each tool with what the test sets, and keeps what it was called with."""

    answers: dict[str, Any] = field(default_factory=dict)
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    tokens: list[str] = field(default_factory=list)

    def server(self) -> FastMCP:
        server = FastMCP(
            "fake-write",
            stateless_http=True,
            json_response=True,
            transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
        )

        def tool(name: str) -> Callable[..., str]:
            def answer(**arguments: Any) -> str:
                self.calls.append((name, arguments))
                reply = self.answers[name]
                if isinstance(reply, Exception):
                    raise reply
                return json.dumps(reply)

            return answer

        async def preview_page_edit(proposal_id: str, payload: dict) -> str:
            return tool("preview_page_edit")(proposal_id=proposal_id, payload=payload)

        async def apply_page_edit(proposal_id: str, payload: dict) -> str:
            return tool("apply_page_edit")(proposal_id=proposal_id, payload=payload)

        async def apply_reply(proposal_id: str, payload: dict, decided_at: str) -> str:
            return tool("apply_reply")(
                proposal_id=proposal_id, payload=payload, decided_at=decided_at
            )

        async def apply_issue(proposal_id: str, payload: dict, target: str | None) -> str:
            return tool("apply_issue")(proposal_id=proposal_id, payload=payload, target=target)

        async def apply_comment(proposal_id: str, payload: dict) -> str:
            return tool("apply_comment")(proposal_id=proposal_id, payload=payload)

        for handler in (
            preview_page_edit,
            apply_page_edit,
            apply_reply,
            apply_issue,
            apply_comment,
        ):
            server.add_tool(handler, structured_output=False)
        return server

    def recording(self, app: ASGIApp) -> ASGIApp:
        async def wrapped(scope: Scope, receive: Receive, send: Send) -> None:
            if scope["type"] == "http":
                headers = dict(scope["headers"])
                self.tokens.append(headers.get(b"authorization", b"").decode())
            await app(scope, receive, send)

        return wrapped


@asynccontextmanager
async def applier_for(fake: FakeWriteServer, **servers: str) -> AsyncIterator[McpApplier]:
    server = fake.server()
    app = fake.recording(server.streamable_http_app())
    async with server.session_manager.run():

        def http(token: str) -> httpx.AsyncClient:
            return httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                headers={"Authorization": f"Bearer {token}"},
            )

        yield McpApplier(
            servers={
                group: WriteServer(url="http://write.test/mcp", resource=resource)
                for group, resource in (servers or {"wiki.write": RESOURCE}).items()
            },
            signing_key=KEY,
            clock=lambda: NOW,
            http=http,
        )


def test_each_kind_calls_its_write_groups_tool_with_the_payload() -> None:
    reply = {"request": "SD-12", "public": True, "text": "Hi"}
    issue = {"action": "create", "project": "CORSAR", "issue_type": "Bug", "summary": "x"}
    comment = {"action": "comment", "issue": "CORSAR-1", "comment": "again"}

    assert apply_call(detail("wiki_edit", PAGE)) == (
        "wiki.write",
        "apply_page_edit",
        {"proposal_id": detail("wiki_edit", PAGE).summary.id, "payload": PAGE},
    )
    group, tool, arguments = apply_call(detail("desk_reply", reply))
    assert (group, tool, arguments["decided_at"]) == (
        "desk.write",
        "apply_reply",
        "2026-09-29T11:00:00+00:00",
    )
    group, tool, arguments = apply_call(detail("tracker_issue", issue | {"description": "d"}))
    # The target labels the issue, from the row, not from what the Job wrote (ADR 0015).
    assert (group, tool, arguments["target"]) == ("tracker.write", "apply_issue", "alert-1")
    assert apply_call(detail("tracker_issue", comment))[:2] == ("tracker.write", "apply_comment")


async def test_an_apply_presents_a_token_for_the_person_bound_to_the_payload() -> None:
    fake = FakeWriteServer(answers={"apply_page_edit": {"state": "applied"}})

    async with applier_for(fake) as applier:
        result = await applier.apply(detail("wiki_edit", PAGE))

    assert result == Applied("applied")
    [(name, arguments)] = fake.calls
    assert (name, arguments["payload"]) == ("apply_page_edit", PAGE)
    token = next(t for t in fake.tokens if t).removeprefix("Bearer ")
    claims = verify(token, jwt.PyJWKSet.from_dict(public_jwks([KEY])), RESOURCE, NOW)
    assert not isinstance(claims, Exception)
    assert (claims.decider, claims.scope, claims.group, claims.digest) == (
        "user:bob",
        "apply",
        "wiki.write",
        payload_digest(PAGE),
    )


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        ({"state": "stale"}, Applied("stale")),
        (
            {"state": "failed", "detail": "Confluence answered 403"},
            Applied("failed", "Confluence answered 403"),
        ),
        ({"state": "done"}, Applied("failed", "the write server's answer is not one of ours")),
    ],
)
async def test_the_write_servers_answer_is_the_result(answer: Any, expected: Applied) -> None:
    fake = FakeWriteServer(answers={"apply_page_edit": answer})

    async with applier_for(fake) as applier:
        assert await applier.apply(detail("wiki_edit", PAGE)) == expected


def decided_ago(seconds: int) -> ProposalDetail:
    issue = detail("tracker_issue", {"action": "create", "project": "OPS"})
    decided_at = datetime.fromtimestamp(NOW - seconds, UTC).isoformat()
    return replace(issue, summary=replace(issue.summary, decided_at=decided_at))


async def test_a_write_server_that_cannot_tell_leaves_the_proposal_for_a_retry() -> None:
    fake = FakeWriteServer(
        answers={"apply_issue": {"state": "unknown", "detail": "Jira answered 502"}}
    )

    async with applier_for(fake, **{"tracker.write": "x"}) as applier:
        with pytest.raises(ApplyUnavailable, match="Jira answered 502"):
            await applier.apply(decided_ago(60))


async def test_an_apply_that_cannot_tell_for_too_long_fails_with_the_reason() -> None:
    fake = FakeWriteServer(
        answers={"apply_issue": {"state": "unknown", "detail": "Jira answered 502"}}
    )

    async with applier_for(fake, **{"tracker.write": "x"}) as applier:
        result = await applier.apply(decided_ago(UNKNOWN_SECONDS + 1))

    assert result.state == "failed"
    assert "Jira answered 502" in (result.detail or "")


async def test_a_tool_error_is_a_failed_apply_with_its_text() -> None:
    fake = FakeWriteServer(answers={"apply_page_edit": ValueError("space DOCS is not writable")})

    async with applier_for(fake) as applier:
        result = await applier.apply(detail("wiki_edit", PAGE))

    assert result.state == "failed"
    assert "space DOCS is not writable" in (result.detail or "")


async def test_a_kind_without_a_configured_write_server_fails_without_a_call() -> None:
    fake = FakeWriteServer()

    async with applier_for(fake) as applier:
        result = await applier.apply(detail("desk_reply", {"request": "SD-1", "public": True}))

    assert result == Applied("failed", "no write server is configured for desk.write")
    assert fake.calls == []


async def test_an_unreachable_write_server_leaves_the_proposal_for_a_retry() -> None:
    def http(token: str) -> httpx.AsyncClient:
        def refuse(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused")

        return httpx.AsyncClient(transport=httpx.MockTransport(refuse))

    applier = McpApplier(
        servers={"wiki.write": WriteServer(url="http://write.test/mcp", resource=RESOURCE)},
        signing_key=KEY,
        clock=lambda: NOW,
        http=http,
    )

    with pytest.raises(ApplyUnavailable):
        await applier.apply(detail("wiki_edit", PAGE))


def answering(status: int, body: dict[str, Any]) -> Callable[[str], httpx.AsyncClient]:
    def http(token: str) -> httpx.AsyncClient:
        def answer(request: httpx.Request) -> httpx.Response:
            return httpx.Response(status, json=body)

        return httpx.AsyncClient(transport=httpx.MockTransport(answer))

    return http


@pytest.mark.parametrize(
    ("status", "body"),
    [
        (401, {"error": "invalid_token"}),
        (403, {"error": "insufficient_scope", "scope": "wiki.write"}),
    ],
)
async def test_a_write_server_refusing_the_token_fails_the_apply_instead_of_retrying_it(
    status: int, body: dict[str, Any]
) -> None:
    # A decider the token cannot name, or an audience set wrong, is refused by the gate on
    # every retry the same way: it ends failed with the reason, not accepted for ever.
    applier = McpApplier(
        servers={"wiki.write": WriteServer(url="http://write.test/mcp", resource=RESOURCE)},
        signing_key=KEY,
        clock=lambda: NOW,
        http=answering(status, body),
    )

    result = await applier.apply(detail("wiki_edit", PAGE))

    assert result.state == "failed"
    assert result.detail is not None
    assert f"refused the apply: {status}" in result.detail


@pytest.mark.parametrize("status", [429, 502, 503])
async def test_a_write_server_that_is_busy_or_down_leaves_the_apply_for_a_retry(
    status: int,
) -> None:
    applier = McpApplier(
        servers={"wiki.write": WriteServer(url="http://write.test/mcp", resource=RESOURCE)},
        signing_key=KEY,
        clock=lambda: NOW,
        http=answering(status, {"error": "busy"}),
    )

    with pytest.raises(ApplyUnavailable):
        await applier.apply(detail("wiki_edit", PAGE))


async def test_a_preview_reads_the_live_page_as_the_person_looking() -> None:
    live = {"title": "Home", "version": 8, "body": "<p>Someone else's edit</p>"}
    fake = FakeWriteServer(answers={"preview_page_edit": live})

    async with applier_for(fake) as applier:
        page = await applier.preview(detail("wiki_edit", PAGE, state="pending"), "user:carol")

    assert page == LivePage(title="Home", version=8, body="<p>Someone else's edit</p>")
    token = next(t for t in fake.tokens if t).removeprefix("Bearer ")
    claims = verify(token, jwt.PyJWKSet.from_dict(public_jwks([KEY])), RESOURCE, NOW)
    assert not isinstance(claims, Exception)
    assert (claims.decider, claims.scope) == ("user:carol", "preview")


async def test_a_preview_that_cannot_be_read_is_unavailable() -> None:
    fake = FakeWriteServer(answers={"preview_page_edit": {"title": "Home"}})

    async with applier_for(fake) as applier:
        with pytest.raises(ApplyUnavailable):
            await applier.preview(detail("wiki_edit", PAGE, state="pending"), "user:carol")
