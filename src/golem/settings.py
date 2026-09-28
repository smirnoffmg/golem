"""Process settings, parsed from the environment by pure functions.

Every missing variable is reported in one error, so a deployment is fixed in one round.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml
from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.fernet import Fernet

from golem.edge.policy import Registry
from golem.metrics import DEFAULT_METRICS_PORT
from golem.orchestrator.admission import Limits
from golem.orchestrator.jobs import CatalogRef
from golem.orchestrator.merge_requests import GitLabProject
from golem.orchestrator.proposals import MR_POLL_SECONDS
from golem.orchestrator.service import JobTemplate
from golem.ratelimit import (
    AUTH_FAILURE_RATE,
    CALLER_RATE,
    COMMAND_RATE,
    DIRECTORY_RATE,
    LOGIN_RATE,
    START_RATE,
    WEBHOOK_RATE,
    Network,
    Rate,
    parse_networks,
)
from golem.run_token import ISSUER as GOLEM_ISSUER
from golem.run_token import SigningKey

DEFAULT_PORT = "8000"
DEFAULT_INTERNAL_READ_PORT = "8001"
DEFAULT_INTERNAL_WRITE_PORT = "8002"
METRICS_PORT = "GOLEM_METRICS_PORT"

Env = Mapping[str, str]


class SettingsError(ValueError):
    pass


class Kubernetes(StrEnum):
    IN_CLUSTER = "in-cluster"
    KUBECONFIG = "kubeconfig"
    # Local development without a cluster: the task service starts but refuses every run.
    NONE = "none"


@dataclass(frozen=True)
class EdgeSettings:
    issuer: str
    audience: str
    jwks_url: str
    oidc_discovery_url: str
    registry_file: Path
    max_chain_depth: int
    audit_dsn: str = field(repr=False)
    task_service_url: str
    catalogs_dir: Path
    public_base_url: str
    port: int
    # Signs the public agent cards (ADR 0014); its own key, not the run tokens'.
    card_signing_key_file: Path
    card_signing_kid: str
    # Sent with every forwarded request; the task service trusts the principal header only
    # together with it.
    edge_token: str = field(repr=False)
    # The task service's internal read port: the orchestrator's public keys, which verify the
    # call tokens runs present when they delegate, and run statuses, which revoke them (ADR 0014).
    task_service_read_url: str = ""
    run_status_ttl_seconds: float = 10.0
    caller_rate: Rate = CALLER_RATE
    auth_failure_rate: Rate = AUTH_FAILURE_RATE
    directory_rate: Rate = DIRECTORY_RATE
    trusted_proxies: tuple[Network, ...] = ()
    metrics_port: int = DEFAULT_METRICS_PORT


@dataclass(frozen=True)
class TaskServiceSettings:
    runs_dsn: str = field(repr=False)
    tasks_db_url: str = field(repr=False)
    limits: Limits
    estimated_cost: Decimal
    template: JobTemplate
    catalogs_file: Path
    agent_tools_file: Path
    run_token_key_file: Path
    run_token_kid: str
    kubernetes: Kubernetes
    public_base_url: str
    # The edge's port (A2A); the MCP servers' port (run keys, run status); the reconciler's
    # port (run outcomes). ADR 0009.
    port: int
    internal_read_port: int
    internal_write_port: int
    edge_token: str = field(repr=False)
    push_allowed_prefixes: tuple[str, ...] = ()
    push_config_key: str | None = field(default=None, repr=False)
    # Scraped from the monitoring namespace only; no caller of the service reaches it (ADR 0013).
    metrics_port: int = DEFAULT_METRICS_PORT


@dataclass(frozen=True)
class ReconcilerSettings:
    runs_dsn: str = field(repr=False)
    interval_seconds: float
    task_service_url: str
    gitlab_url: str
    gitlab_token: str = field(repr=False)
    gitlab_projects_file: Path
    namespace: str
    kubernetes: Kubernetes
    metrics_port: int = DEFAULT_METRICS_PORT
    mr_poll_seconds: float = MR_POLL_SECONDS


def edge_settings(env: Env) -> EdgeSettings:
    v = _values(
        env,
        "GOLEM_OIDC_ISSUER",
        "GOLEM_OIDC_AUDIENCE",
        "GOLEM_OIDC_JWKS_URL",
        "GOLEM_OIDC_DISCOVERY_URL",
        "GOLEM_CALL_REGISTRY_FILE",
        "GOLEM_MAX_CHAIN_DEPTH",
        "GOLEM_AUDIT_DSN",
        "GOLEM_TASK_SERVICE_URL",
        "GOLEM_CATALOGS_DIR",
        "GOLEM_PUBLIC_BASE_URL",
        "GOLEM_EDGE_TOKEN",
        "GOLEM_TASK_SERVICE_READ_URL",
        "GOLEM_CARD_SIGNING_KEY_FILE",
        "GOLEM_CARD_SIGNING_KID",
    )
    if v["GOLEM_OIDC_ISSUER"] == GOLEM_ISSUER:
        raise SettingsError(
            f"GOLEM_OIDC_ISSUER must not be {GOLEM_ISSUER!r}: the edge tells Golem's call tokens"
            " from the identity provider's tokens by their issuer"
        )
    return EdgeSettings(
        issuer=v["GOLEM_OIDC_ISSUER"],
        audience=v["GOLEM_OIDC_AUDIENCE"],
        jwks_url=v["GOLEM_OIDC_JWKS_URL"],
        oidc_discovery_url=v["GOLEM_OIDC_DISCOVERY_URL"],
        registry_file=Path(v["GOLEM_CALL_REGISTRY_FILE"]),
        max_chain_depth=_positive_int(v, "GOLEM_MAX_CHAIN_DEPTH"),
        audit_dsn=v["GOLEM_AUDIT_DSN"],
        task_service_url=v["GOLEM_TASK_SERVICE_URL"],
        catalogs_dir=Path(v["GOLEM_CATALOGS_DIR"]),
        public_base_url=_base_url(v, "GOLEM_PUBLIC_BASE_URL"),
        port=_port(env),
        edge_token=v["GOLEM_EDGE_TOKEN"],
        card_signing_key_file=Path(v["GOLEM_CARD_SIGNING_KEY_FILE"]),
        card_signing_kid=v["GOLEM_CARD_SIGNING_KID"],
        task_service_read_url=_base_url(v, "GOLEM_TASK_SERVICE_READ_URL"),
        run_status_ttl_seconds=_non_negative_seconds(env, "GOLEM_RUN_STATUS_TTL_SECONDS", "10"),
        metrics_port=metrics_port_setting(env, _port(env)),
        caller_rate=rate_setting(env, "GOLEM_RATE_CALLER", CALLER_RATE),
        auth_failure_rate=rate_setting(env, "GOLEM_RATE_AUTH_FAILURES", AUTH_FAILURE_RATE),
        directory_rate=rate_setting(env, "GOLEM_RATE_DIRECTORY", DIRECTORY_RATE),
        trusted_proxies=trusted_proxies_setting(env),
    )


def rate_setting(env: Env, name: str, default: Rate) -> Rate:
    """``<name>_PER_MINUTE`` and ``<name>_BURST``, each defaulting to ``default``'s (ADR 0012)."""
    per_minute, burst = f"{name}_PER_MINUTE", f"{name}_BURST"
    values = {
        per_minute: env.get(per_minute, "").strip() or str(default.per_minute),
        burst: env.get(burst, "").strip() or str(default.burst),
    }
    return Rate(per_minute=_positive_int(values, per_minute), burst=_positive_int(values, burst))


def trusted_proxies_setting(env: Env) -> tuple[Network, ...]:
    """``GOLEM_TRUSTED_PROXIES``: the CIDRs whose ``X-Forwarded-For`` is believed; none by
    default, so a client's own header never picks its rate limit key."""
    try:
        return parse_networks(env.get("GOLEM_TRUSTED_PROXIES", ""))
    except ValueError as error:
        raise SettingsError(
            f"GOLEM_TRUSTED_PROXIES must be comma-separated CIDRs without host bits: {error}"
        ) from error


def _push_prefixes(env: Env) -> tuple[str, ...]:
    raw = env.get("GOLEM_PUSH_ALLOWED_PREFIXES", "")
    prefixes = tuple(p.strip() for p in raw.split(",") if p.strip())
    # Without the trailing slash "http://adapter:8080" would also allow "http://adapter:8080.evil".
    bad = [p for p in prefixes if not p.endswith("/")]
    if bad:
        raise SettingsError(f"GOLEM_PUSH_ALLOWED_PREFIXES: each prefix must end with '/': {bad}")
    return prefixes


def _push_config_key(env: Env) -> str | None:
    key = env.get("GOLEM_PUSH_CONFIG_KEY") or None
    if env.get("GOLEM_PUSH_ALLOWED_PREFIXES") and key is None:
        raise SettingsError(
            "GOLEM_PUSH_CONFIG_KEY is required with GOLEM_PUSH_ALLOWED_PREFIXES:"
            " push tokens are stored encrypted"
        )
    if key is not None:
        _fernet_key(key, "GOLEM_PUSH_CONFIG_KEY")
    return key


def _fernet_key(key: str, name: str) -> None:
    try:
        Fernet(key)
    except ValueError as error:
        raise SettingsError(
            f"{name} must be a Fernet key (32 url-safe base64-encoded bytes)"
        ) from error


def task_service_settings(env: Env) -> TaskServiceSettings:
    v = _values(
        env,
        "GOLEM_RUNS_DSN",
        "GOLEM_TASKS_DB_URL",
        "GOLEM_MAX_RUNS_PER_CALLER",
        "GOLEM_MAX_RUNS_PER_ROOT",
        "GOLEM_BUDGET_PER_ROOT",
        "GOLEM_ESTIMATED_RUN_COST",
        "GOLEM_JOB_IMAGE",
        "GOLEM_JOB_SECRET",
        "GOLEM_JOB_DEADLINE_SECONDS",
        "GOLEM_JOB_TTL_SECONDS",
        "GOLEM_JOB_CPU",
        "GOLEM_JOB_MEMORY",
        "GOLEM_CATALOGS_FILE",
        "GOLEM_AGENT_TOOLS_FILE",
        "GOLEM_RUN_TOKEN_KEY_FILE",
        "GOLEM_RUN_TOKEN_KID",
        "GOLEM_KUBERNETES_NAMESPACE",
        "GOLEM_KUBERNETES",
        "GOLEM_PUBLIC_BASE_URL",
        "GOLEM_EDGE_TOKEN",
    )
    port, read_port, write_port, metrics_port = _task_service_ports(env)
    return TaskServiceSettings(
        runs_dsn=v["GOLEM_RUNS_DSN"],
        tasks_db_url=v["GOLEM_TASKS_DB_URL"],
        limits=_checked(
            "GOLEM_MAX_RUNS_PER_CALLER, GOLEM_MAX_RUNS_PER_ROOT, GOLEM_BUDGET_PER_ROOT",
            lambda: Limits(
                max_runs_per_caller=_positive_int(v, "GOLEM_MAX_RUNS_PER_CALLER"),
                max_runs_per_root=_positive_int(v, "GOLEM_MAX_RUNS_PER_ROOT"),
                budget_per_root=_decimal(v, "GOLEM_BUDGET_PER_ROOT"),
            ),
        ),
        estimated_cost=_decimal(v, "GOLEM_ESTIMATED_RUN_COST"),
        template=JobTemplate(
            image=v["GOLEM_JOB_IMAGE"],
            namespace=v["GOLEM_KUBERNETES_NAMESPACE"],
            secret_name=v["GOLEM_JOB_SECRET"],
            active_deadline_seconds=_positive_int(v, "GOLEM_JOB_DEADLINE_SECONDS"),
            ttl_seconds_after_finished=_non_negative_int(v, "GOLEM_JOB_TTL_SECONDS"),
            cpu=v["GOLEM_JOB_CPU"],
            memory=v["GOLEM_JOB_MEMORY"],
            mcp_registry_configmap=env.get("GOLEM_MCP_REGISTRY_CONFIGMAP", "").strip() or None,
        ),
        catalogs_file=Path(v["GOLEM_CATALOGS_FILE"]),
        agent_tools_file=Path(v["GOLEM_AGENT_TOOLS_FILE"]),
        run_token_key_file=Path(v["GOLEM_RUN_TOKEN_KEY_FILE"]),
        run_token_kid=v["GOLEM_RUN_TOKEN_KID"],
        kubernetes=_kubernetes(v),
        public_base_url=_base_url(v, "GOLEM_PUBLIC_BASE_URL"),
        port=port,
        internal_read_port=read_port,
        internal_write_port=write_port,
        edge_token=v["GOLEM_EDGE_TOKEN"],
        push_allowed_prefixes=_push_prefixes(env),
        push_config_key=_push_config_key(env),
        metrics_port=metrics_port,
    )


def reconciler_settings(env: Env) -> ReconcilerSettings:
    v = _values(
        env,
        "GOLEM_RUNS_DSN",
        "GOLEM_RECONCILE_INTERVAL_SECONDS",
        "GOLEM_TASK_SERVICE_URL",
        "GOLEM_GITLAB_URL",
        "GOLEM_GITLAB_TOKEN",
        "GOLEM_GITLAB_PROJECTS_FILE",
        "GOLEM_KUBERNETES_NAMESPACE",
        "GOLEM_KUBERNETES",
    )
    interval = _parsed(v, "GOLEM_RECONCILE_INTERVAL_SECONDS", float, "a number of seconds")
    if not interval > 0:
        raise SettingsError(f"GOLEM_RECONCILE_INTERVAL_SECONDS must be positive: {interval}")
    poll = {"GOLEM_MR_POLL_SECONDS": env.get("GOLEM_MR_POLL_SECONDS", "").strip()}
    mr_poll_seconds = (
        _parsed(poll, "GOLEM_MR_POLL_SECONDS", float, "a number of seconds")
        if poll["GOLEM_MR_POLL_SECONDS"]
        else MR_POLL_SECONDS
    )
    if not mr_poll_seconds > 0:
        raise SettingsError(f"GOLEM_MR_POLL_SECONDS must be positive: {mr_poll_seconds}")
    return ReconcilerSettings(
        runs_dsn=v["GOLEM_RUNS_DSN"],
        interval_seconds=interval,
        task_service_url=v["GOLEM_TASK_SERVICE_URL"],
        gitlab_url=_base_url(v, "GOLEM_GITLAB_URL"),
        gitlab_token=v["GOLEM_GITLAB_TOKEN"],
        gitlab_projects_file=Path(v["GOLEM_GITLAB_PROJECTS_FILE"]),
        namespace=v["GOLEM_KUBERNETES_NAMESPACE"],
        kubernetes=_kubernetes(v),
        metrics_port=metrics_port_setting(env),
        mr_poll_seconds=mr_poll_seconds,
    )


def parse_registry(text: str) -> Registry:
    """``callee: [caller, ...]``; a caller may be a wildcard such as ``user:*``."""
    data = _yaml_mapping(text, "call registry")
    allowed: dict[str, frozenset[str]] = {}
    for callee, callers in data.items():
        if not isinstance(callers, list) or not all(isinstance(c, str) for c in callers):
            raise SettingsError(f"call registry: {callee!r} must map to a list of caller names")
        allowed[str(callee)] = frozenset(callers)
    return Registry(allowed_callers=allowed)


def parse_catalog_refs(text: str) -> dict[str, CatalogRef]:
    """``agent: <git url>#<revision>``."""
    refs: dict[str, CatalogRef] = {}
    for agent, value in _yaml_mapping(text, "catalogs").items():
        url, sep, revision = str(value).rpartition("#")
        if not sep or not url or not revision:
            raise SettingsError(f"catalogs: {agent!r} must be '<url>#<revision>', got {value!r}")
        refs[str(agent)] = CatalogRef(url=url, revision=revision)
    return refs


def parse_agent_tools(text: str) -> dict[str, tuple[str, ...]]:
    """``agent: [tool group, ...]``: the platform's grant, whatever the agent's catalog asks for."""
    grants: dict[str, tuple[str, ...]] = {}
    for agent, groups in _yaml_mapping(text, "agent tools").items():
        if not isinstance(groups, list) or not all(isinstance(g, str) and g for g in groups):
            raise SettingsError(f"agent tools: {agent!r} must map to a list of tool group names")
        grants[str(agent)] = tuple(groups)
    return grants


def parse_signing_key(pem: str, kid: str, variable: str = "GOLEM_RUN_TOKEN_KEY_FILE") -> SigningKey:
    try:
        return SigningKey.from_pem(pem, kid)
    except (ValueError, TypeError, UnsupportedAlgorithm) as error:
        raise SettingsError(
            f"{variable} must hold an unencrypted EC P-256 private key: {error}"
        ) from error


def parse_gitlab_projects(text: str) -> dict[str, GitLabProject]:
    """``agent: {project: <group/name>, target_branch: <branch>}``."""
    projects: dict[str, GitLabProject] = {}
    for agent, value in _yaml_mapping(text, "GitLab projects").items():
        if not isinstance(value, dict) or not all(
            isinstance(value.get(k), str) and value[k] for k in ("project", "target_branch")
        ):
            raise SettingsError(
                f"GitLab projects: {agent!r} needs 'project' and 'target_branch', got {value!r}"
            )
        projects[str(agent)] = GitLabProject(
            path=value["project"], target_branch=value["target_branch"]
        )
    return projects


def _values(env: Env, *names: str) -> dict[str, str]:
    missing = [name for name in names if not env.get(name, "").strip()]
    if missing:
        raise SettingsError(f"missing environment variables: {', '.join(missing)}")
    return {name: env[name].strip() for name in names}


def _parsed[T](v: Mapping[str, str], name: str, parse: Callable[[str], T], kind: str) -> T:
    try:
        return parse(v[name])
    except (ValueError, InvalidOperation) as error:
        raise SettingsError(f"{name} must be {kind}, got {v[name]!r}") from error


def _checked[T](names: str, build: Callable[[], T]) -> T:
    try:
        return build()
    except SettingsError:
        raise
    except ValueError as error:
        raise SettingsError(f"{names}: {error}") from error


def _positive_int(v: Mapping[str, str], name: str) -> int:
    value = _parsed(v, name, int, "an integer")
    if value <= 0:
        raise SettingsError(f"{name} must be positive, got {value}")
    return value


def _non_negative_int(v: Mapping[str, str], name: str) -> int:
    value = _parsed(v, name, int, "an integer")
    if value < 0:
        raise SettingsError(f"{name} must not be negative, got {value}")
    return value


def _non_negative_seconds(env: Env, name: str, default: str) -> float:
    raw = env.get(name, "").strip() or default
    value = _parsed({name: raw}, name, float, "a number of seconds")
    if not value >= 0:
        raise SettingsError(f"{name} must not be negative, got {value}")
    return value


def _decimal(v: Mapping[str, str], name: str) -> Decimal:
    value = _parsed(v, name, Decimal, "a decimal number")
    if not value.is_finite() or value < 0:
        raise SettingsError(f"{name} must be a non-negative number, got {v[name]!r}")
    return value


def _port(env: Env, name: str = "GOLEM_PORT", default: str = DEFAULT_PORT) -> int:
    port = _parsed({name: env.get(name, default)}, name, int, "a port")
    if not 0 < port < 65536:
        raise SettingsError(f"{name} must be a TCP port, got {port}")
    return port


def _task_service_ports(env: Env) -> tuple[int, int, int, int]:
    ports = (
        _port(env),
        _port(env, "GOLEM_INTERNAL_READ_PORT", DEFAULT_INTERNAL_READ_PORT),
        _port(env, "GOLEM_INTERNAL_WRITE_PORT", DEFAULT_INTERNAL_WRITE_PORT),
        _port(env, METRICS_PORT, str(DEFAULT_METRICS_PORT)),
    )
    if len(set(ports)) != len(ports):
        raise SettingsError(
            "GOLEM_PORT, GOLEM_INTERNAL_READ_PORT, GOLEM_INTERNAL_WRITE_PORT and"
            f" {METRICS_PORT} must be distinct, got {ports}"
        )
    return ports


def metrics_port_setting(env: Env, *taken: int) -> int:
    """``GOLEM_METRICS_PORT`` (9090): never one of ``taken``, the ports callers are admitted to."""
    port = _port(env, METRICS_PORT, str(DEFAULT_METRICS_PORT))
    if port in taken:
        raise SettingsError(f"{METRICS_PORT} must differ from the process's other ports: {port}")
    return port


def _base_url(v: Mapping[str, str], name: str) -> str:
    if v[name].endswith("/"):
        raise SettingsError(f"{name} must not end with a slash: {v[name]!r}")
    return v[name]


def _kubernetes(v: Mapping[str, str]) -> Kubernetes:
    choices = ", ".join(mode.value for mode in Kubernetes)
    return _parsed(v, "GOLEM_KUBERNETES", Kubernetes, f"one of {choices}")


def _yaml_mapping(text: str, what: str) -> dict[Any, Any]:
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as error:
        raise SettingsError(f"{what}: not valid YAML: {error}") from error
    if not isinstance(data, dict):
        raise SettingsError(f"{what}: must be a mapping at the top level")
    return data


@dataclass(frozen=True)
class AdapterSettings:
    edge_url: str
    token_url: str
    client_id: str
    client_secret: str = field(repr=False)
    jira_url: str
    # Set: Basic auth with the account's email and an API token (Jira Cloud).
    # Unset: the token is a personal access token sent as Bearer (Jira Data Center).
    jira_user: str | None
    jira_token: str = field(repr=False)
    webhook_secret: bytes = field(repr=False)
    push_secret: bytes = field(repr=False)
    labels_file: Path
    public_base_url: str
    port: int
    webhook_rate: Rate = WEBHOOK_RATE
    trusted_proxies: tuple[Network, ...] = ()
    metrics_port: int = DEFAULT_METRICS_PORT


def adapter_settings(env: Env) -> AdapterSettings:
    v = _values(
        env,
        "GOLEM_EDGE_URL",
        "GOLEM_OIDC_TOKEN_URL",
        "GOLEM_OIDC_CLIENT_ID",
        "GOLEM_OIDC_CLIENT_SECRET",
        "GOLEM_JIRA_URL",
        "GOLEM_JIRA_TOKEN",
        "GOLEM_JIRA_WEBHOOK_SECRET",
        "GOLEM_PUSH_TOKEN_SECRET",
        "GOLEM_JIRA_LABELS_FILE",
        "GOLEM_PUBLIC_BASE_URL",
    )
    return AdapterSettings(
        edge_url=_base_url(v, "GOLEM_EDGE_URL"),
        token_url=v["GOLEM_OIDC_TOKEN_URL"],
        client_id=v["GOLEM_OIDC_CLIENT_ID"],
        client_secret=v["GOLEM_OIDC_CLIENT_SECRET"],
        jira_url=_base_url(v, "GOLEM_JIRA_URL"),
        jira_user=env.get("GOLEM_JIRA_USER", "").strip() or None,
        jira_token=v["GOLEM_JIRA_TOKEN"],
        webhook_secret=v["GOLEM_JIRA_WEBHOOK_SECRET"].encode(),
        push_secret=v["GOLEM_PUSH_TOKEN_SECRET"].encode(),
        labels_file=Path(v["GOLEM_JIRA_LABELS_FILE"]),
        public_base_url=_base_url(v, "GOLEM_PUBLIC_BASE_URL"),
        port=_port(env),
        webhook_rate=rate_setting(env, "GOLEM_RATE_WEBHOOK", WEBHOOK_RATE),
        metrics_port=metrics_port_setting(env, _port(env)),
        trusted_proxies=trusted_proxies_setting(env),
    )


def parse_label_agents(text: str) -> dict[str, str]:
    """``<jira label>: <agent name>``."""
    labels: dict[str, str] = {}
    for label, agent in _yaml_mapping(text, "Jira labels").items():
        if not isinstance(agent, str) or not agent:
            raise SettingsError(f"Jira labels: {label!r} must map to an agent name, got {agent!r}")
        labels[str(label)] = agent
    return labels


@dataclass(frozen=True)
class MattermostAdapterSettings:
    edge_url: str
    token_url: str
    client_id: str
    client_secret: str = field(repr=False)
    mattermost_url: str
    bot_token: str = field(repr=False)
    command_token: bytes = field(repr=False)
    push_secret: bytes = field(repr=False)
    agents: frozenset[str]
    teams: frozenset[str]
    # Empty: every channel of the allowed teams.
    channels: frozenset[str]
    public_base_url: str
    port: int
    command_rate: Rate = COMMAND_RATE
    trusted_proxies: tuple[Network, ...] = ()
    metrics_port: int = DEFAULT_METRICS_PORT


def mattermost_adapter_settings(env: Env) -> MattermostAdapterSettings:
    v = _values(
        env,
        "GOLEM_EDGE_URL",
        "GOLEM_OIDC_TOKEN_URL",
        "GOLEM_OIDC_CLIENT_ID",
        "GOLEM_OIDC_CLIENT_SECRET",
        "GOLEM_PUSH_TOKEN_SECRET",
        "GOLEM_PUBLIC_BASE_URL",
        "GOLEM_MATTERMOST_URL",
        "GOLEM_MATTERMOST_BOT_TOKEN",
        "GOLEM_MATTERMOST_COMMAND_TOKEN",
        "GOLEM_MATTERMOST_AGENTS",
        "GOLEM_MATTERMOST_TEAMS",
    )
    return MattermostAdapterSettings(
        edge_url=_base_url(v, "GOLEM_EDGE_URL"),
        token_url=v["GOLEM_OIDC_TOKEN_URL"],
        client_id=v["GOLEM_OIDC_CLIENT_ID"],
        client_secret=v["GOLEM_OIDC_CLIENT_SECRET"],
        mattermost_url=_base_url(v, "GOLEM_MATTERMOST_URL"),
        bot_token=v["GOLEM_MATTERMOST_BOT_TOKEN"],
        command_token=v["GOLEM_MATTERMOST_COMMAND_TOKEN"].encode(),
        push_secret=v["GOLEM_PUSH_TOKEN_SECRET"].encode(),
        agents=_names(v, "GOLEM_MATTERMOST_AGENTS"),
        teams=_names(v, "GOLEM_MATTERMOST_TEAMS"),
        channels=_names(env, "GOLEM_MATTERMOST_CHANNELS", required=False),
        public_base_url=_base_url(v, "GOLEM_PUBLIC_BASE_URL"),
        port=_port(env),
        command_rate=rate_setting(env, "GOLEM_RATE_COMMAND", COMMAND_RATE),
        metrics_port=metrics_port_setting(env, _port(env)),
        trusted_proxies=trusted_proxies_setting(env),
    )


def _names(env: Mapping[str, str], name: str, *, required: bool = True) -> frozenset[str]:
    names = frozenset(n.strip() for n in env.get(name, "").split(",") if n.strip())
    if required and not names:
        raise SettingsError(f"{name} must list at least one name, separated by commas")
    return names


@dataclass(frozen=True)
class UiSettings:
    issuer: str
    discovery_url: str
    client_id: str
    client_secret: str = field(repr=False)
    redirect_url: str
    edge_url: str
    dsn: str = field(repr=False)
    session_key: str = field(repr=False)
    agents: tuple[str, ...]
    public_base_url: str
    port: int
    login_rate: Rate = LOGIN_RATE
    start_rate: Rate = START_RATE
    trusted_proxies: tuple[Network, ...] = ()
    metrics_port: int = DEFAULT_METRICS_PORT


UI_CALLBACK_PATH = "/callback"
LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def ui_settings(env: Env) -> UiSettings:
    v = _values(
        env,
        "GOLEM_OIDC_ISSUER",
        "GOLEM_OIDC_DISCOVERY_URL",
        "GOLEM_OIDC_CLIENT_ID",
        "GOLEM_OIDC_CLIENT_SECRET",
        "GOLEM_OIDC_REDIRECT_URL",
        "GOLEM_EDGE_URL",
        "GOLEM_UI_DSN",
        "GOLEM_UI_SESSION_KEY",
        "GOLEM_UI_AGENTS",
        "GOLEM_PUBLIC_BASE_URL",
    )
    public_base_url = _base_url(v, "GOLEM_PUBLIC_BASE_URL")
    public = urlsplit(public_base_url)
    # The session cookie is Secure with the __Host- prefix, which browsers accept over plain
    # HTTP only on localhost.
    if public.scheme != "https" and not (
        public.scheme == "http" and public.hostname in LOCAL_HOSTS
    ):
        raise SettingsError(
            "GOLEM_PUBLIC_BASE_URL must be https (plain http only on localhost):"
            f" {public_base_url!r}"
        )
    if v["GOLEM_OIDC_REDIRECT_URL"] != f"{public_base_url}{UI_CALLBACK_PATH}":
        raise SettingsError(
            f"GOLEM_OIDC_REDIRECT_URL must be GOLEM_PUBLIC_BASE_URL + {UI_CALLBACK_PATH!r},"
            f" got {v['GOLEM_OIDC_REDIRECT_URL']!r}"
        )
    _fernet_key(v["GOLEM_UI_SESSION_KEY"], "GOLEM_UI_SESSION_KEY")
    agents = tuple(a.strip() for a in v["GOLEM_UI_AGENTS"].split(",") if a.strip())
    if not agents:
        raise SettingsError("GOLEM_UI_AGENTS must list at least one name, separated by commas")
    return UiSettings(
        issuer=v["GOLEM_OIDC_ISSUER"],
        discovery_url=v["GOLEM_OIDC_DISCOVERY_URL"],
        client_id=v["GOLEM_OIDC_CLIENT_ID"],
        client_secret=v["GOLEM_OIDC_CLIENT_SECRET"],
        redirect_url=v["GOLEM_OIDC_REDIRECT_URL"],
        edge_url=_base_url(v, "GOLEM_EDGE_URL"),
        dsn=v["GOLEM_UI_DSN"],
        session_key=v["GOLEM_UI_SESSION_KEY"],
        agents=tuple(dict.fromkeys(agents)),
        public_base_url=public_base_url,
        port=_port(env),
        login_rate=rate_setting(env, "GOLEM_RATE_LOGIN", LOGIN_RATE),
        start_rate=rate_setting(env, "GOLEM_RATE_START", START_RATE),
        metrics_port=metrics_port_setting(env, _port(env)),
        trusted_proxies=trusted_proxies_setting(env),
    )
