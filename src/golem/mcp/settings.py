"""Settings of a platform MCP server, parsed from the environment.

Every missing variable is reported in one error, so a deployment is fixed in one round.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from golem.mcp.atlassian import Deployment, JiraDeployment
from golem.mcp.groups import GROUPS
from golem.metrics import DEFAULT_METRICS_PORT
from golem.ratelimit import AUTH_FAILURE_RATE, Network, Rate
from golem.settings import (
    SettingsError,
    metrics_port_setting,
    rate_setting,
    trusted_proxies_setting,
)

DEFAULTS = {
    "GOLEM_MCP_RUN_STATUS_TTL_SECONDS": "10",
    "GOLEM_MCP_PROPOSAL_STATUS_TTL_SECONDS": "10",
    "GOLEM_MCP_KEYS_REFRESH_SECONDS": "60",
    "GOLEM_PORT": "8000",
}
REQUIRED = (
    "GOLEM_MCP_GROUP",
    "GOLEM_MCP_UPSTREAM_URL",
    "GOLEM_MCP_UPSTREAM_TOKEN",
    "GOLEM_TASK_SERVICE_URL",
    "GOLEM_AUDIT_DSN",
)
JIRA_DEPLOYMENT = "GOLEM_MCP_JIRA_DEPLOYMENT"
CONFLUENCE_DEPLOYMENT = "GOLEM_MCP_CONFLUENCE_DEPLOYMENT"
RESOURCE = "GOLEM_MCP_RESOURCE"
# Where a write server may write, narrowed again on the server (ADR 0015).
ALLOWED = {
    "wiki.write": "GOLEM_MCP_WIKI_SPACES",
    "desk.write": "GOLEM_MCP_DESK_PROJECTS",
    "tracker.write": "GOLEM_MCP_TRACKER_PROJECTS",
}
# The groups that call Jira's search, whose path differs between Cloud and Data Center.
JIRA_SEARCHING = frozenset({"tracker.read", "tracker.write"})
KEY = re.compile(r"^[A-Za-z][A-Za-z0-9_~]*$")

Env = Mapping[str, str]


@dataclass(frozen=True)
class McpSettings:
    group: str
    upstream_url: str
    # Set: Basic auth with the account's email and an API token (Atlassian Cloud).
    # Unset: the token is a personal access token sent as Bearer (Data Center).
    upstream_user: str | None
    upstream_token: str = field(repr=False)
    jira_deployment: JiraDeployment | None
    task_service_url: str
    audit_dsn: str = field(repr=False)
    run_status_ttl_seconds: float
    keys_refresh_seconds: float
    port: int
    auth_failure_rate: Rate = AUTH_FAILURE_RATE
    trusted_proxies: tuple[Network, ...] = ()
    metrics_port: int = DEFAULT_METRICS_PORT
    # A write server only: Cloud's v2 or Data Center's page API, its tokens' audience (the
    # canonical URI of its /mcp endpoint, ADR 0016), and the spaces or projects it writes to.
    confluence_deployment: Deployment | None = None
    resource: str | None = None
    allowed: frozenset[str] = frozenset()
    proposal_status_ttl_seconds: float = 10.0


def mcp_settings(env: Env) -> McpSettings:
    group = env.get("GOLEM_MCP_GROUP", "").strip()
    writes = group in ALLOWED
    needed = [
        *([JIRA_DEPLOYMENT] if group in JIRA_SEARCHING else []),
        *([CONFLUENCE_DEPLOYMENT] if group == "wiki.write" else []),
        *([RESOURCE, ALLOWED[group]] if writes else []),
    ]
    v = {name: env.get(name, "").strip() for name in (*REQUIRED, *needed)}
    missing = [name for name in REQUIRED if not v[name]]
    missing[3:3] = [name for name in needed if not v[name]]
    if missing:
        raise SettingsError(f"missing environment variables: {', '.join(missing)}")
    return McpSettings(
        group=_group(group),
        upstream_url=_base_url("GOLEM_MCP_UPSTREAM_URL", v["GOLEM_MCP_UPSTREAM_URL"]),
        upstream_user=env.get("GOLEM_MCP_UPSTREAM_USER", "").strip() or None,
        upstream_token=v["GOLEM_MCP_UPSTREAM_TOKEN"],
        jira_deployment=(
            _deployment(JIRA_DEPLOYMENT, v[JIRA_DEPLOYMENT]) if group in JIRA_SEARCHING else None
        ),
        task_service_url=_base_url("GOLEM_TASK_SERVICE_URL", v["GOLEM_TASK_SERVICE_URL"]),
        audit_dsn=v["GOLEM_AUDIT_DSN"],
        run_status_ttl_seconds=_seconds(env, "GOLEM_MCP_RUN_STATUS_TTL_SECONDS"),
        keys_refresh_seconds=_seconds(env, "GOLEM_MCP_KEYS_REFRESH_SECONDS"),
        port=_port(env),
        auth_failure_rate=rate_setting(env, "GOLEM_RATE_AUTH_FAILURES", AUTH_FAILURE_RATE),
        trusted_proxies=trusted_proxies_setting(env),
        metrics_port=metrics_port_setting(env, _port(env)),
        confluence_deployment=(
            _deployment(CONFLUENCE_DEPLOYMENT, v[CONFLUENCE_DEPLOYMENT])
            if group == "wiki.write"
            else None
        ),
        resource=_resource(v[RESOURCE]) if writes else None,
        allowed=_keys(ALLOWED[group], v[ALLOWED[group]]) if writes else frozenset(),
        proposal_status_ttl_seconds=_seconds(env, "GOLEM_MCP_PROPOSAL_STATUS_TTL_SECONDS"),
    )


def _group(name: str) -> str:
    if name not in GROUPS:
        raise SettingsError(f"GOLEM_MCP_GROUP must be one of {', '.join(GROUPS)}, got {name!r}")
    return name


def _deployment(name: str, value: str) -> Deployment:
    try:
        return Deployment(value)
    except ValueError as error:
        choices = ", ".join(d.value for d in Deployment)
        raise SettingsError(f"{name} must be one of {choices}, got {value!r}") from error


def _resource(value: str) -> str:
    # MCP authorization: the canonical URI has a lowercase scheme and host, no fragment, and is
    # used without the trailing slash; the token's audience must equal it exactly.
    parts = urlsplit(value)
    canonical = (
        parts.scheme in ("http", "https")
        and parts.netloc == parts.netloc.lower()
        and bool(parts.netloc)
        and not parts.fragment
        and not value.endswith("/")
    )
    if not canonical:
        raise SettingsError(f"{RESOURCE} must be the canonical URI of the server, got {value!r}")
    return value


def _keys(name: str, value: str) -> frozenset[str]:
    keys = [key.strip() for key in value.split(",") if key.strip()]
    if not keys:
        raise SettingsError(f"{name} must name at least one space or project key")
    for key in keys:
        if not KEY.match(key):
            raise SettingsError(f"{name}: {key!r} is not a space or project key")
    return frozenset(keys)


def _base_url(name: str, value: str) -> str:
    if not value.startswith(("http://", "https://")):
        raise SettingsError(f"{name} must be an http(s) URL, got {value!r}")
    if value.endswith("/"):
        raise SettingsError(f"{name} must not end with a slash: {value!r}")
    return value


def _raw(env: Env, name: str) -> str:
    return env.get(name, "").strip() or DEFAULTS[name]


def _seconds(env: Env, name: str) -> float:
    raw = _raw(env, name)
    try:
        value = float(raw)
    except ValueError as error:
        raise SettingsError(f"{name} must be a number of seconds, got {raw!r}") from error
    if not value >= 0:
        raise SettingsError(f"{name} must not be negative, got {value}")
    return value


def _port(env: Env) -> int:
    raw = _raw(env, "GOLEM_PORT")
    try:
        port = int(raw)
    except ValueError as error:
        raise SettingsError(f"GOLEM_PORT must be a port, got {raw!r}") from error
    if not 0 < port < 65536:
        raise SettingsError(f"GOLEM_PORT must be a TCP port, got {port}")
    return port
