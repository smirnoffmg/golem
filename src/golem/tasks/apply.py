"""The write servers as the task service calls them (ADR 0015): one MCP call per apply or
preview, with a proposal token issued for that call alone.

A write server answers an apply with ``{"state": "applied" | "stale" | "failed", "detail"}`` and
a preview with the live page, ``{"title", "version", "body"}``. A server that cannot be reached
raises ``ApplyUnavailable``: the proposal stays accepted and the reconciler asks again, and the
apply is idempotent per proposal. A tool error is the upstream refusing: ``failed``.
"""

import json
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.types import TextContent

from golem.proposal_payload import WRITE_GROUPS
from golem.proposal_token import APPLY, PREVIEW, ProposalClaims, issue
from golem.run_token import SigningKey
from golem.tasks.ports import Applied, LivePage, ProposalDetail

KIND_GROUPS = WRITE_GROUPS
RESULTS = frozenset({"applied", "stale", "failed"})
MAX_DETAIL = 500


class ApplyUnavailable(RuntimeError):
    """The write server could not be asked, or answered nothing usable: try again later."""


@dataclass(frozen=True)
class WriteServer:
    url: str
    # The server's canonical URI, its tokens' audience (ADR 0016).
    resource: str


def apply_call(proposal: ProposalDetail) -> tuple[str, str, dict[str, Any]]:
    """The write group, the tool and its arguments that apply ``proposal``."""
    kind, payload = proposal.summary.kind, dict(proposal.payload)
    arguments: dict[str, Any] = {"proposal_id": proposal.summary.id, "payload": payload}
    match kind:
        case "wiki_edit":
            return KIND_GROUPS[kind], "apply_page_edit", arguments
        case "desk_reply":
            # The reply carries no marker the customer would read; a comment made after the
            # decision with the same text is this proposal already applied.
            return (
                KIND_GROUPS[kind],
                "apply_reply",
                {
                    **arguments,
                    "decided_at": proposal.summary.decided_at,
                },
            )
        case "tracker_issue" if payload.get("action") == "comment":
            return KIND_GROUPS[kind], "apply_comment", arguments
        case "tracker_issue":
            return KIND_GROUPS[kind], "apply_issue", {**arguments, "target": proposal.target}
    raise ValueError(f"proposals of kind {kind!r} are not applied by the platform")


def _default_http(token: str) -> httpx.AsyncClient:
    return httpx.AsyncClient(headers={"Authorization": f"Bearer {token}"}, timeout=15)


@dataclass(frozen=True)
class McpApplier:
    servers: Mapping[str, WriteServer]
    signing_key: SigningKey
    clock: Callable[[], float] = field(default=time.time)
    # A client that presents the token; tests put the server behind a transport.
    http: Callable[[str], httpx.AsyncClient] = field(default=_default_http)

    async def apply(self, decided: ProposalDetail) -> Applied:
        group, tool, arguments = apply_call(decided)
        server = self.servers.get(group)
        if server is None:
            return Applied("failed", f"no write server is configured for {group}")
        token = self._token(server, decided, decided.summary.decided_by or "", APPLY, group)
        text, failed = await self._call(server, token, tool, arguments)
        if failed:
            return Applied("failed", text[:MAX_DETAIL])
        answer = _object(text)
        state, detail = answer.get("state"), answer.get("detail")
        if state not in RESULTS:
            return Applied("failed", "the write server's answer is not one of ours")
        return Applied(state, detail[:MAX_DETAIL] if isinstance(detail, str) else None)

    async def preview(self, proposal: ProposalDetail, reader: str) -> LivePage:
        group = KIND_GROUPS[proposal.summary.kind]
        server = self.servers.get(group)
        if server is None:
            raise ApplyUnavailable(f"no write server is configured for {group}")
        token = self._token(server, proposal, reader, PREVIEW, group)
        arguments = {"proposal_id": proposal.summary.id, "payload": dict(proposal.payload)}
        text, failed = await self._call(server, token, "preview_page_edit", arguments)
        answer = {} if failed else _object(text)
        title, version, body = answer.get("title"), answer.get("version"), answer.get("body")
        if not (isinstance(title, str) and isinstance(version, int) and isinstance(body, str)):
            raise ApplyUnavailable(f"the live page could not be read: {text[:MAX_DETAIL]}")
        return LivePage(title=title, version=version, body=body)

    def _token(
        self, server: WriteServer, proposal: ProposalDetail, person: str, scope: str, group: str
    ) -> str:
        claims = ProposalClaims(
            audience=server.resource,
            decider=person,
            proposal_id=proposal.summary.id,
            scope=scope,
            group=group,
            digest=proposal.digest or "",
        )
        return issue(claims, self.signing_key, int(self.clock()))

    async def _call(
        self, server: WriteServer, token: str, tool: str, arguments: dict[str, Any]
    ) -> tuple[str, bool]:
        try:
            async with (
                self.http(token) as client,
                streamable_http_client(server.url, http_client=client) as (read, write, _),
                ClientSession(read, write) as session,
            ):
                await session.initialize()
                result = await session.call_tool(tool, arguments)
        except Exception as error:
            raise ApplyUnavailable(f"{server.url}: {type(error).__name__}: {error}") from error
        text = "".join(part.text for part in result.content if isinstance(part, TextContent))
        return text, bool(result.isError)


def _object(text: str) -> dict[str, Any]:
    try:
        parsed = json.loads(text)
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


class NoWriteServers:
    """A task service configured with no write server: an accept fails with the reason."""

    async def apply(self, decided: ProposalDetail) -> Applied:
        return Applied("failed", "no write server is configured")

    async def preview(self, proposal: ProposalDetail, reader: str) -> LivePage:
        raise ApplyUnavailable("no write server is configured")
