"""A platform MCP server for one tool group: FastMCP over streamable HTTP behind the gate."""

import json
from typing import Any

import httpx
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from starlette.types import ASGIApp

from golem.mcp import atlassian, writes
from golem.mcp.atlassian import Deployment, JiraDeployment, UpstreamError
from golem.mcp.gate import PROPOSAL_STATE_KEY, Gate, gated
from golem.mcp.groups import Group
from golem.metrics import Instrumented
from golem.proposal_token import ProposalClaims

READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=True)
# An apply replaces a page or posts for everyone to read, and asked again it writes nothing more.
WRITE = ToolAnnotations(
    readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=True
)


def tracker_tools(server: FastMCP, jira: httpx.AsyncClient, deployment: JiraDeployment) -> None:
    async def search_issues(jql: str, limit: int = 20) -> str:
        """Find Jira issues by JQL; one line per issue: key, status, type, summary, assignee."""
        return await atlassian.search_issues(jira, deployment, jql, limit)

    async def get_issue(key: str) -> str:
        """Read one Jira issue by key (PROJ-123): key fields and its description as text."""
        return await atlassian.get_issue(jira, key)

    for tool in (search_issues, get_issue):
        server.add_tool(tool, annotations=READ_ONLY, structured_output=False)


def wiki_tools(server: FastMCP, confluence: httpx.AsyncClient) -> None:
    async def search_pages(cql: str, limit: int = 20) -> str:
        """Find Confluence pages by CQL; one line per page: id, title, space, version."""
        return await atlassian.search_pages(confluence, cql, limit)

    async def get_page(page_id: str) -> str:
        """Read one Confluence page by its numeric id: title, space, URL and its text."""
        return await atlassian.get_page(confluence, page_id)

    async def get_page_source(page_id: str) -> str:
        """Read one Confluence page as Confluence stores it, to propose an edit of it: JSON with
        page_id, title, version and body (storage format, whole). Propose back that version."""
        return await atlassian.get_page_source(confluence, page_id)

    for tool in (search_pages, get_page, get_page_source):
        server.add_tool(tool, annotations=READ_ONLY, structured_output=False)


def decider(ctx: Context[Any, Any, Any]) -> str:
    """The person who decided, from the proposal token the gate verified for this request."""
    request = ctx.request_context.request
    claims = getattr(request, "scope", {}).get("state", {}).get(PROPOSAL_STATE_KEY)
    if not isinstance(claims, ProposalClaims):
        raise UpstreamError("the request carries no verified proposal token")
    return claims.decider


def wiki_write_tools(
    server: FastMCP, confluence: httpx.AsyncClient, deployment: Deployment, spaces: frozenset[str]
) -> None:
    async def preview_page_edit(proposal_id: str, payload: dict[str, Any]) -> str:
        """The page a wiki_edit proposal would replace, as Confluence holds it now: JSON with
        title, version and body (storage format)."""
        return json.dumps(await writes.preview_page_edit(confluence, deployment, spaces, payload))

    async def apply_page_edit(
        proposal_id: str, payload: dict[str, Any], ctx: Context[Any, Any, Any]
    ) -> str:
        """Write an accepted wiki_edit proposal as the page's next version; JSON with state
        (applied, stale or failed) and detail."""
        return json.dumps(
            await writes.apply_page_edit(
                confluence, deployment, spaces, proposal_id, payload, decider(ctx)
            )
        )

    server.add_tool(preview_page_edit, annotations=READ_ONLY, structured_output=False)
    server.add_tool(apply_page_edit, annotations=WRITE, structured_output=False)


def desk_write_tools(server: FastMCP, jira: httpx.AsyncClient, projects: frozenset[str]) -> None:
    async def apply_reply(proposal_id: str, payload: dict[str, Any], decided_at: str) -> str:
        """Post an accepted desk_reply proposal on its request; JSON with state and detail."""
        return json.dumps(
            await writes.apply_reply(jira, projects, proposal_id, payload, decided_at)
        )

    server.add_tool(apply_reply, annotations=WRITE, structured_output=False)


def tracker_write_tools(
    server: FastMCP, jira: httpx.AsyncClient, deployment: Deployment, projects: frozenset[str]
) -> None:
    async def apply_issue(proposal_id: str, payload: dict[str, Any], target: str | None) -> str:
        """Create the issue of an accepted tracker_issue proposal; JSON with state and
        detail."""
        return json.dumps(
            await writes.apply_issue(jira, deployment, projects, proposal_id, payload, target)
        )

    async def apply_comment(proposal_id: str, payload: dict[str, Any]) -> str:
        """Comment as an accepted tracker_issue proposal says; JSON with state and detail."""
        return json.dumps(await writes.apply_comment(jira, projects, proposal_id, payload))

    for tool in (apply_issue, apply_comment):
        server.add_tool(tool, annotations=WRITE, structured_output=False)


def mcp_server(
    group: Group,
    upstream: httpx.AsyncClient,
    jira_deployment: JiraDeployment | None,
    confluence_deployment: Deployment | None = None,
    allowed: frozenset[str] = frozenset(),
) -> FastMCP:
    server = FastMCP(
        f"golem-{group.name}",
        stateless_http=True,
        json_response=True,
        log_level="WARNING",
        # Host and Origin checks guard browsers against DNS rebinding; every request here needs a
        # run token no browser holds, and in-cluster callers use the Service's host name.
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
    if group.writes and not allowed:
        raise ValueError(f"tool group {group.name!r} needs the spaces or projects it writes to")
    if group.name in ("tracker.read", "tracker.write"):
        if jira_deployment is None:
            raise ValueError(f"tool group {group.name!r} needs the Jira deployment")
        if group.writes:
            tracker_write_tools(server, upstream, jira_deployment, allowed)
        else:
            tracker_tools(server, upstream, jira_deployment)
    elif group.name == "wiki.write":
        if confluence_deployment is None:
            raise ValueError(f"tool group {group.name!r} needs the Confluence deployment")
        wiki_write_tools(server, upstream, confluence_deployment, allowed)
    elif group.name == "desk.write":
        desk_write_tools(server, upstream, allowed)
    else:
        wiki_tools(server, upstream)
    return server


def create_mcp_app(
    *,
    gate: Gate,
    upstream: httpx.AsyncClient,
    jira_deployment: JiraDeployment | None,
    confluence_deployment: Deployment | None = None,
    allowed: frozenset[str] = frozenset(),
) -> ASGIApp:
    server = mcp_server(gate.group, upstream, jira_deployment, confluence_deployment, allowed)
    app = server.streamable_http_app()
    return Instrumented(gated(app, gate), routes=app.routes, metrics=gate.metrics)
