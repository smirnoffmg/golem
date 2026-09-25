"""A platform MCP server for one tool group: FastMCP over streamable HTTP behind the gate."""

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from starlette.types import ASGIApp

from golem.mcp import atlassian
from golem.mcp.atlassian import JiraDeployment
from golem.mcp.gate import Gate, gated
from golem.mcp.groups import Group
from golem.metrics import Instrumented

READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=True)


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

    for tool in (search_pages, get_page):
        server.add_tool(tool, annotations=READ_ONLY, structured_output=False)


def mcp_server(
    group: Group, upstream: httpx.AsyncClient, jira_deployment: JiraDeployment | None
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
    if group.system == "jira":
        if jira_deployment is None:
            raise ValueError(f"tool group {group.name!r} needs the Jira deployment")
        tracker_tools(server, upstream, jira_deployment)
    else:
        wiki_tools(server, upstream)
    return server


def create_mcp_app(
    *, gate: Gate, upstream: httpx.AsyncClient, jira_deployment: JiraDeployment | None
) -> ASGIApp:
    server = mcp_server(gate.group, upstream, jira_deployment)
    app = server.streamable_http_app()
    return Instrumented(gated(app, gate), routes=app.routes, metrics=gate.metrics)
