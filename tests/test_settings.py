from decimal import Decimal
from pathlib import Path

import pytest

from golem.edge.policy import Registry
from golem.orchestrator.jobs import CatalogRef
from golem.orchestrator.merge_requests import GitLabProject
from golem.settings import (
    Kubernetes,
    SettingsError,
    edge_settings,
    parse_catalog_refs,
    parse_gitlab_projects,
    parse_registry,
    reconciler_settings,
    task_service_settings,
)

EDGE_ENV = {
    "GOLEM_OIDC_ISSUER": "https://idp.example.test/realms/golem",
    "GOLEM_OIDC_AUDIENCE": "golem-edge",
    "GOLEM_OIDC_JWKS_URL": "https://idp.example.test/realms/golem/certs",
    "GOLEM_OIDC_DISCOVERY_URL": "https://idp.example.test/realms/golem/.well-known/openid-configuration",
    "GOLEM_CALL_REGISTRY_FILE": "/etc/golem/registry.yaml",
    "GOLEM_MAX_CHAIN_DEPTH": "4",
    "GOLEM_AUDIT_DSN": "host=db dbname=golem_audit user=golem_edge",
    "GOLEM_TASK_SERVICE_URL": "http://tasks:8000",
    "GOLEM_CATALOGS_DIR": "/app/examples",
    "GOLEM_PUBLIC_BASE_URL": "https://golem.example.test",
}

TASKS_ENV = {
    "GOLEM_RUNS_DSN": "host=db dbname=golem_runs user=golem_runs",
    "GOLEM_TASKS_DB_URL": "postgresql+asyncpg://golem_tasks@db/golem_tasks",
    "GOLEM_MAX_RUNS_PER_CALLER": "5",
    "GOLEM_MAX_RUNS_PER_ROOT": "10",
    "GOLEM_BUDGET_PER_ROOT": "100.50",
    "GOLEM_ESTIMATED_RUN_COST": "2",
    "GOLEM_JOB_IMAGE": "registry.example.test/golem:0.1.0",
    "GOLEM_JOB_SECRET": "golem-run-secrets",
    "GOLEM_JOB_DEADLINE_SECONDS": "3600",
    "GOLEM_JOB_TTL_SECONDS": "600",
    "GOLEM_JOB_CPU": "1",
    "GOLEM_JOB_MEMORY": "1Gi",
    "GOLEM_CATALOGS_FILE": "/etc/golem/catalogs.yaml",
    "GOLEM_KUBERNETES_NAMESPACE": "team-jobs",
    "GOLEM_KUBERNETES": "in-cluster",
    "GOLEM_PUBLIC_BASE_URL": "https://golem.example.test",
}

RECONCILER_ENV = {
    "GOLEM_RUNS_DSN": "host=db dbname=golem_runs user=golem_runs",
    "GOLEM_RECONCILE_INTERVAL_SECONDS": "2.5",
    "GOLEM_TASK_SERVICE_URL": "http://tasks:8000",
    "GOLEM_GITLAB_URL": "https://gitlab.example.test",
    "GOLEM_GITLAB_TOKEN": "glpat-test",
    "GOLEM_GITLAB_PROJECTS_FILE": "/etc/golem/projects.yaml",
    "GOLEM_KUBERNETES_NAMESPACE": "team-jobs",
    "GOLEM_KUBERNETES": "kubeconfig",
}


def test_edge_settings_are_parsed_from_the_environment() -> None:
    settings = edge_settings(EDGE_ENV)

    assert settings.issuer == "https://idp.example.test/realms/golem"
    assert settings.audience == "golem-edge"
    assert settings.registry_file == Path("/etc/golem/registry.yaml")
    assert settings.max_chain_depth == 4
    assert settings.catalogs_dir == Path("/app/examples")
    assert settings.port == 8000


def test_every_missing_variable_is_named_at_once() -> None:
    env = {k: v for k, v in EDGE_ENV.items() if k not in {"GOLEM_OIDC_ISSUER", "GOLEM_AUDIT_DSN"}}
    env["GOLEM_TASK_SERVICE_URL"] = "  "

    with pytest.raises(SettingsError) as error:
        edge_settings(env)

    message = str(error.value)
    for name in ("GOLEM_OIDC_ISSUER", "GOLEM_AUDIT_DSN", "GOLEM_TASK_SERVICE_URL"):
        assert name in message
    assert "GOLEM_OIDC_AUDIENCE" not in message


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("GOLEM_MAX_CHAIN_DEPTH", "deep"),
        ("GOLEM_MAX_CHAIN_DEPTH", "0"),
        ("GOLEM_PORT", "http"),
        ("GOLEM_PUBLIC_BASE_URL", "https://golem.example.test/"),
    ],
)
def test_an_invalid_value_names_its_variable(name: str, value: str) -> None:
    with pytest.raises(SettingsError, match=name):
        edge_settings({**EDGE_ENV, name: value})


def test_task_service_settings_build_limits_and_job_template() -> None:
    settings = task_service_settings(TASKS_ENV)

    assert settings.limits.max_runs_per_caller == 5
    assert settings.limits.budget_per_root == Decimal("100.50")
    assert settings.estimated_cost == Decimal("2")
    assert settings.template.namespace == "team-jobs"
    assert settings.template.active_deadline_seconds == 3600
    assert settings.kubernetes is Kubernetes.IN_CLUSTER
    assert settings.tasks_db_url == "postgresql+asyncpg://golem_tasks@db/golem_tasks"


def test_task_service_accepts_no_cluster() -> None:
    settings = task_service_settings({**TASKS_ENV, "GOLEM_KUBERNETES": "none"})

    assert settings.kubernetes is Kubernetes.NONE


def test_an_unknown_cluster_access_mode_is_refused() -> None:
    with pytest.raises(SettingsError, match="GOLEM_KUBERNETES"):
        task_service_settings({**TASKS_ENV, "GOLEM_KUBERNETES": "maybe"})


def test_task_service_missing_variables_are_all_named() -> None:
    with pytest.raises(SettingsError) as error:
        task_service_settings({})

    assert all(name in str(error.value) for name in TASKS_ENV)


def test_reconciler_settings() -> None:
    settings = reconciler_settings(RECONCILER_ENV)

    assert settings.interval_seconds == 2.5
    assert settings.gitlab_url == "https://gitlab.example.test"
    assert settings.gitlab_projects_file == Path("/etc/golem/projects.yaml")
    assert settings.kubernetes is Kubernetes.KUBECONFIG


def test_a_non_positive_interval_is_refused() -> None:
    with pytest.raises(SettingsError, match="GOLEM_RECONCILE_INTERVAL_SECONDS"):
        reconciler_settings({**RECONCILER_ENV, "GOLEM_RECONCILE_INTERVAL_SECONDS": "0"})


def test_registry_maps_each_callee_to_its_allowed_callers() -> None:
    registry = parse_registry("discovery:\n  - user:*\n  - agent:reviewer\nreviewer: []\n")

    assert registry == Registry(
        allowed_callers={
            "discovery": frozenset({"user:*", "agent:reviewer"}),
            "reviewer": frozenset(),
        }
    )


@pytest.mark.parametrize("text", ["- discovery\n", "discovery: user:*\n", "discovery: [1]\n"])
def test_a_malformed_registry_is_refused(text: str) -> None:
    with pytest.raises(SettingsError, match="registry"):
        parse_registry(text)


def test_catalog_refs_split_url_and_revision_at_the_last_hash() -> None:
    refs = parse_catalog_refs("discovery: https://git.example.test/agents/discovery.git#v1\n")

    assert refs == {
        "discovery": CatalogRef(url="https://git.example.test/agents/discovery.git", revision="v1")
    }


@pytest.mark.parametrize("text", ["discovery: https://git.example.test/a.git\n", "[]\n"])
def test_a_catalog_ref_without_revision_is_refused(text: str) -> None:
    with pytest.raises(SettingsError, match="catalogs"):
        parse_catalog_refs(text)


def test_gitlab_projects_name_the_project_and_target_branch() -> None:
    projects = parse_gitlab_projects(
        "discovery:\n  project: product/discovery-context\n  target_branch: main\n"
    )

    assert projects == {
        "discovery": GitLabProject(path="product/discovery-context", target_branch="main")
    }


def test_a_gitlab_project_without_target_branch_is_refused() -> None:
    with pytest.raises(SettingsError, match="discovery"):
        parse_gitlab_projects("discovery:\n  project: product/discovery-context\n")
