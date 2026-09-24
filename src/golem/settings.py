"""Process settings, parsed from the environment by pure functions.

Every missing variable is reported in one error, so a deployment is fixed in one round.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml

from golem.edge.policy import Registry
from golem.orchestrator.admission import Limits
from golem.orchestrator.jobs import CatalogRef
from golem.orchestrator.merge_requests import GitLabProject
from golem.orchestrator.service import JobTemplate

DEFAULT_PORT = "8000"

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
    audit_dsn: str
    task_service_url: str
    catalogs_dir: Path
    public_base_url: str
    port: int


@dataclass(frozen=True)
class TaskServiceSettings:
    runs_dsn: str
    tasks_db_url: str
    limits: Limits
    estimated_cost: Decimal
    template: JobTemplate
    catalogs_file: Path
    kubernetes: Kubernetes
    public_base_url: str
    port: int


@dataclass(frozen=True)
class ReconcilerSettings:
    runs_dsn: str
    interval_seconds: float
    task_service_url: str
    gitlab_url: str
    gitlab_token: str
    gitlab_projects_file: Path
    namespace: str
    kubernetes: Kubernetes


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
    )


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
        "GOLEM_KUBERNETES_NAMESPACE",
        "GOLEM_KUBERNETES",
        "GOLEM_PUBLIC_BASE_URL",
    )
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
        ),
        catalogs_file=Path(v["GOLEM_CATALOGS_FILE"]),
        kubernetes=_kubernetes(v),
        public_base_url=_base_url(v, "GOLEM_PUBLIC_BASE_URL"),
        port=_port(env),
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
    return ReconcilerSettings(
        runs_dsn=v["GOLEM_RUNS_DSN"],
        interval_seconds=interval,
        task_service_url=v["GOLEM_TASK_SERVICE_URL"],
        gitlab_url=_base_url(v, "GOLEM_GITLAB_URL"),
        gitlab_token=v["GOLEM_GITLAB_TOKEN"],
        gitlab_projects_file=Path(v["GOLEM_GITLAB_PROJECTS_FILE"]),
        namespace=v["GOLEM_KUBERNETES_NAMESPACE"],
        kubernetes=_kubernetes(v),
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


def _decimal(v: Mapping[str, str], name: str) -> Decimal:
    value = _parsed(v, name, Decimal, "a decimal number")
    if not value.is_finite() or value < 0:
        raise SettingsError(f"{name} must be a non-negative number, got {v[name]!r}")
    return value


def _port(env: Env) -> int:
    port = _parsed({"GOLEM_PORT": env.get("GOLEM_PORT", DEFAULT_PORT)}, "GOLEM_PORT", int, "a port")
    if not 0 < port < 65536:
        raise SettingsError(f"GOLEM_PORT must be a TCP port, got {port}")
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
