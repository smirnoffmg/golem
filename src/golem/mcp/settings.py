"""Settings of a platform MCP server, parsed from the environment.

Every missing variable is reported in one error, so a deployment is fixed in one round.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field

from golem.mcp.atlassian import JiraDeployment
from golem.mcp.gate import AUTH_FAILURE_RATE
from golem.mcp.groups import GROUPS
from golem.ratelimit import Network, Rate
from golem.settings import SettingsError, rate_setting, trusted_proxies_setting

DEFAULTS = {
    "GOLEM_MCP_RUN_STATUS_TTL_SECONDS": "10",
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


def mcp_settings(env: Env) -> McpSettings:
    v = {name: env.get(name, "").strip() for name in (*REQUIRED, JIRA_DEPLOYMENT)}
    needs_deployment = v["GOLEM_MCP_GROUP"] == "tracker.read"
    missing = [name for name in REQUIRED if not v[name]]
    if needs_deployment and not v[JIRA_DEPLOYMENT]:
        missing.insert(3, JIRA_DEPLOYMENT)
    if missing:
        raise SettingsError(f"missing environment variables: {', '.join(missing)}")
    return McpSettings(
        group=_group(v["GOLEM_MCP_GROUP"]),
        upstream_url=_base_url("GOLEM_MCP_UPSTREAM_URL", v["GOLEM_MCP_UPSTREAM_URL"]),
        upstream_user=env.get("GOLEM_MCP_UPSTREAM_USER", "").strip() or None,
        upstream_token=v["GOLEM_MCP_UPSTREAM_TOKEN"],
        jira_deployment=_deployment(v[JIRA_DEPLOYMENT]) if needs_deployment else None,
        task_service_url=_base_url("GOLEM_TASK_SERVICE_URL", v["GOLEM_TASK_SERVICE_URL"]),
        audit_dsn=v["GOLEM_AUDIT_DSN"],
        run_status_ttl_seconds=_seconds(env, "GOLEM_MCP_RUN_STATUS_TTL_SECONDS"),
        keys_refresh_seconds=_seconds(env, "GOLEM_MCP_KEYS_REFRESH_SECONDS"),
        port=_port(env),
        auth_failure_rate=rate_setting(env, "GOLEM_RATE_AUTH_FAILURES", AUTH_FAILURE_RATE),
        trusted_proxies=trusted_proxies_setting(env),
    )


def _group(name: str) -> str:
    if name not in GROUPS:
        raise SettingsError(f"GOLEM_MCP_GROUP must be one of {', '.join(GROUPS)}, got {name!r}")
    return name


def _deployment(value: str) -> JiraDeployment:
    try:
        return JiraDeployment(value)
    except ValueError as error:
        choices = ", ".join(d.value for d in JiraDeployment)
        raise SettingsError(f"{JIRA_DEPLOYMENT} must be one of {choices}, got {value!r}") from error


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
