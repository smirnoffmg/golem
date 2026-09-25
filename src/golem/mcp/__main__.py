"""A platform MCP server process: ``python -m golem.mcp``, one tool group per process."""

import logging
import os
import sys
from functools import partial
from urllib.parse import urlsplit

import httpx
import uvicorn
from starlette.types import ASGIApp

from golem.adapters.jira import jira_authorization
from golem.jwks import SigningKeys, fetch_jwks
from golem.mcp.auth import RunStatuses, run_token_verifier
from golem.mcp.gate import Gate
from golem.mcp.groups import GROUPS
from golem.mcp.server import create_mcp_app
from golem.mcp.settings import McpSettings, mcp_settings
from golem.ratelimit import Limiter
from golem.settings import SettingsError
from golem.tasks.app import RUN_KEYS_PATH

JWKS_TIMEOUT_SECONDS = 2
RUN_STATUS_TIMEOUT_SECONDS = 2
UPSTREAM_TIMEOUT_SECONDS = 20


def upstream_client(settings: McpSettings) -> httpx.AsyncClient:
    # The server's own credentials; the caller's run token is never forwarded (no passthrough).
    authorization = jira_authorization(user=settings.upstream_user, token=settings.upstream_token)
    return httpx.AsyncClient(
        base_url=settings.upstream_url,
        headers={"Authorization": authorization, "Accept": "application/json"},
        timeout=UPSTREAM_TIMEOUT_SECONDS,
    )


def target_system(settings: McpSettings) -> str:
    return f"{GROUPS[settings.group].system}:{urlsplit(settings.upstream_url).netloc}"


def build_app(settings: McpSettings, jwks_client: httpx.Client) -> ASGIApp:
    keys = SigningKeys(
        partial(fetch_jwks, jwks_client, f"{settings.task_service_url}{RUN_KEYS_PATH}"),
        min_refresh_seconds=settings.keys_refresh_seconds,
    )
    keys.refresh()
    gate = Gate(
        group=GROUPS[settings.group],
        target_system=target_system(settings),
        verify=run_token_verifier(keys),
        statuses=RunStatuses(
            httpx.AsyncClient(
                base_url=settings.task_service_url, timeout=RUN_STATUS_TIMEOUT_SECONDS
            ),
            ttl_seconds=settings.run_status_ttl_seconds,
        ),
        audit_dsn=settings.audit_dsn,
        auth_failures=Limiter(settings.auth_failure_rate),
        trusted_proxies=settings.trusted_proxies,
    )
    return create_mcp_app(
        gate=gate, upstream=upstream_client(settings), jira_deployment=settings.jira_deployment
    )


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        settings = mcp_settings(os.environ)
    except SettingsError as error:
        sys.exit(f"golem mcp: {error}")
    with httpx.Client(timeout=JWKS_TIMEOUT_SECONDS) as jwks_client:
        # The client address comes from golem.ratelimit with GOLEM_TRUSTED_PROXIES, not uvicorn.
        uvicorn.run(
            build_app(settings, jwks_client),
            host="0.0.0.0",
            port=settings.port,
            proxy_headers=False,
        )


if __name__ == "__main__":
    main()
