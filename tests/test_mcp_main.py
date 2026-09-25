import base64
from pathlib import Path

import httpx
import yaml
from starlette.testclient import TestClient

from golem.catalog import DELEGATE_GROUP
from golem.mcp.__main__ import build_app, target_system, upstream_client
from golem.mcp.atlassian import JiraDeployment
from golem.mcp.groups import GROUPS
from golem.mcp.server import mcp_server
from golem.mcp.settings import mcp_settings

EXAMPLE_REGISTRY = Path(__file__).parent.parent / "examples" / "mcp-registry.yaml"
ENV = {
    "GOLEM_MCP_GROUP": "wiki.read",
    "GOLEM_MCP_UPSTREAM_URL": "https://example.atlassian.net/wiki",
    "GOLEM_MCP_UPSTREAM_USER": "bot@example.test",
    "GOLEM_MCP_UPSTREAM_TOKEN": "api-token",
    "GOLEM_TASK_SERVICE_URL": "http://127.0.0.1:1",
    "GOLEM_AUDIT_DSN": "host=127.0.0.1 port=1 dbname=golem_audit user=golem_mcp",
}


def test_the_groups_serve_what_the_example_registry_allows() -> None:
    registry = yaml.safe_load(EXAMPLE_REGISTRY.read_text())
    # Served by the runtime itself, not a platform MCP server (ADR 0014).
    registry.pop(DELEGATE_GROUP)

    assert {name: tuple(entry["tools"]) for name, entry in registry.items()} == {
        name: group.tools for name, group in GROUPS.items()
    }


async def test_each_server_offers_exactly_its_groups_tools() -> None:
    upstream = httpx.AsyncClient(base_url="https://unused.example.test")
    for group in GROUPS.values():
        server = mcp_server(group, upstream, JiraDeployment.CLOUD)
        tools = await server.list_tools()

        assert tuple(tool.name for tool in tools) == group.tools
        assert all(tool.annotations and tool.annotations.readOnlyHint for tool in tools)


def test_the_upstream_client_carries_the_servers_own_credentials() -> None:
    client = upstream_client(mcp_settings(ENV))

    expected = base64.b64encode(b"bot@example.test:api-token").decode()
    assert client.headers["authorization"] == f"Basic {expected}"
    assert str(client.base_url) == "https://example.atlassian.net/wiki/"


def test_the_target_system_names_the_system_and_its_host() -> None:
    assert target_system(mcp_settings(ENV)) == "confluence:example.atlassian.net"


def test_a_server_that_never_got_signing_keys_refuses_every_call(
    mcp_audit_dsn: str, audit_admin_dsn: str
) -> None:
    settings = mcp_settings({**ENV, "GOLEM_AUDIT_DSN": mcp_audit_dsn})
    with httpx.Client(timeout=1) as jwks_client:
        app = build_app(settings, jwks_client)
        with TestClient(app) as client:
            response = client.post(
                "/mcp",
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
                headers={"Authorization": "Bearer anything", "Accept": "application/json"},
            )

    assert response.status_code == 401
    assert response.json()["error_description"] == "signing keys unavailable"
