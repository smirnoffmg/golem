"""The kustomize manifests in deploy/k8s, rendered and checked without a cluster.

The Secrets are not in git; the External Secrets overlay names every Secret and key the
workloads expect, so it doubles as the list of placeholder values the settings parsers get.
"""

import ipaddress
import posixpath
import re
import shutil
import subprocess
from collections.abc import Callable, Iterator, Mapping
from functools import cache
from pathlib import Path
from typing import Any

import pytest
import yaml

from golem.mcp.settings import mcp_settings
from golem.orchestrator.jobs import APP_LABEL, APP_NAME
from golem.runtime import tools
from golem.settings import (
    adapter_settings,
    edge_settings,
    parse_agent_tools,
    parse_catalog_refs,
    parse_gitlab_projects,
    parse_label_agents,
    parse_registry,
    reconciler_settings,
    task_service_settings,
)

K8S = Path(__file__).parent.parent / "deploy" / "k8s"
BASE = K8S / "base"
EXTERNAL_SECRETS = K8S / "overlays" / "external-secrets"

SYSTEM = "golem-system"
JOBS = "golem-jobs"

SETTINGS: dict[str, Callable[[Mapping[str, str]], object]] = {
    "edge": edge_settings,
    "tasks": task_service_settings,
    "reconciler": reconciler_settings,
    "jira-adapter": adapter_settings,
    "mcp-tracker-read": mcp_settings,
    "mcp-wiki-read": mcp_settings,
}
FILE_PARSERS: dict[str, Callable[[str], object]] = {
    "GOLEM_CALL_REGISTRY_FILE": parse_registry,
    "GOLEM_CATALOGS_FILE": parse_catalog_refs,
    "GOLEM_AGENT_TOOLS_FILE": parse_agent_tools,
    "GOLEM_GITLAB_PROJECTS_FILE": parse_gitlab_projects,
    "GOLEM_JIRA_LABELS_FILE": parse_label_agents,
}
KUBERNETES_API_USERS = {"tasks": "golem-tasks", "reconciler": "golem-reconciler"}
# Who may call the task service, and on which of its ports (ADR 0009): NetworkPolicy admits a
# caller to a port, and each port serves only that caller's routes.
TASK_SERVICE_PORTS = {
    "golem-edge": "a2a",
    "golem-mcp": "internal-read",
    "golem-reconciler": "internal-write",
}
SERVICE_URL = re.compile(r"^http://([a-z0-9-]+)\.([a-z0-9-]+)\.svc:(\d+)(/.*)?$")


@cache
def _render(path: Path) -> str:
    kubectl = shutil.which("kubectl")
    assert kubectl, "kubectl (with built-in kustomize) is needed to render deploy/k8s"
    result = subprocess.run(
        [kubectl, "kustomize", str(path)], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


def render(path: Path) -> list[dict[str, Any]]:
    return [doc for doc in yaml.safe_load_all(_render(path)) if doc]


def of_kind(objects: list[dict], kind: str) -> list[dict]:
    return [o for o in objects if o["kind"] == kind]


def find(objects: list[dict], kind: str, name: str, namespace: str | None = None) -> dict:
    [found] = [
        o
        for o in of_kind(objects, kind)
        if o["metadata"]["name"] == name
        and (namespace is None or o["metadata"].get("namespace") == namespace)
    ]
    return found


def secret_keys(objects: list[dict]) -> dict[tuple[str, str], list[str]]:
    """(namespace, Secret name) to its keys, as the ExternalSecrets produce them."""
    return {
        (e["metadata"]["namespace"], e["spec"]["target"]["name"]): [
            d["secretKey"] for d in e["spec"]["data"]
        ]
        for e in of_kind(objects, "ExternalSecret")
    }


def container(deployment: dict) -> dict:
    [only] = deployment["spec"]["template"]["spec"]["containers"]
    return only


def env_of(deployment: dict, objects: list[dict], secrets: Mapping) -> dict[str, str]:
    namespace = deployment["metadata"]["namespace"]
    env: dict[str, str] = {}
    for source in container(deployment).get("envFrom", []):
        if "configMapRef" in source:
            env |= find(objects, "ConfigMap", source["configMapRef"]["name"], namespace)["data"]
        else:
            keys = secrets[(namespace, source["secretRef"]["name"])]
            env |= {key: f"placeholder-{key.lower()}" for key in keys}
    env |= {e["name"]: e["value"] for e in container(deployment).get("env", []) if "value" in e}
    return env


def mounted(deployment: dict, path: str) -> tuple[str, str, str]:
    """The (kind, name, key) of the ConfigMap or Secret file a container sees at ``path``."""
    directory, filename = posixpath.split(path)
    [mount] = [m for m in container(deployment)["volumeMounts"] if m["mountPath"] == directory]
    [volume] = [
        v for v in deployment["spec"]["template"]["spec"]["volumes"] if v["name"] == mount["name"]
    ]
    for kind, ref, name_field in (
        ("ConfigMap", "configMap", "name"),
        ("Secret", "secret", "secretName"),
    ):
        if ref in volume:
            items = volume[ref].get("items")
            keys = {i["path"]: i["key"] for i in items} if items else {filename: filename}
            return kind, volume[ref][name_field], keys[filename]
    raise AssertionError(f"{path} is not a ConfigMap or Secret file")


def deployments() -> list[dict]:
    return of_kind(render(BASE), "Deployment")


# --- Rendering and settings ---------------------------------------------------------------------


def test_the_base_and_the_external_secrets_overlay_render() -> None:
    assert render(BASE)
    assert of_kind(render(EXTERNAL_SECRETS), "ExternalSecret")


def test_there_is_one_deployment_per_process() -> None:
    assert sorted(d["metadata"]["name"] for d in deployments()) == sorted(SETTINGS)
    assert {d["metadata"]["namespace"] for d in deployments()} == {SYSTEM}


@pytest.mark.parametrize("name", sorted(SETTINGS))
def test_the_settings_parser_accepts_the_env_a_process_gets(name: str) -> None:
    objects = render(EXTERNAL_SECRETS)
    deployment = find(objects, "Deployment", name, SYSTEM)
    env = env_of(deployment, objects, secret_keys(objects))

    SETTINGS[name](env)


@pytest.mark.parametrize("name", sorted(SETTINGS))
def test_every_settings_file_is_mounted_and_parses(name: str) -> None:
    objects = render(EXTERNAL_SECRETS)
    deployment = find(objects, "Deployment", name, SYSTEM)
    env = env_of(deployment, objects, secret_keys(objects))
    files = {name: path for name, path in env.items() if name.endswith("_FILE")}

    for name, path in files.items():
        kind, source, key = mounted(deployment, path)
        if kind == "Secret":
            assert key in secret_keys(objects)[(SYSTEM, source)], f"{name}: {source}/{key}"
        else:
            FILE_PARSERS[name](find(objects, "ConfigMap", source, SYSTEM)["data"][key])


def test_every_in_cluster_url_names_a_service_and_its_port() -> None:
    objects = render(EXTERNAL_SECRETS)
    urls = [
        url.rstrip("/")
        for deployment in deployments()
        for value in env_of(deployment, objects, secret_keys(objects)).values()
        for url in value.split(",")
        if ".svc:" in url
    ]
    registry = yaml.safe_load(
        find(objects, "ConfigMap", "golem-mcp-registry", JOBS)["data"]["registry.yaml"]
    )
    urls += [group["url"] for group in registry.values()]

    assert urls
    for url in urls:
        match = SERVICE_URL.fullmatch(url)
        assert match, url
        service = find(objects, "Service", match[1], match[2])
        assert int(match[3]) in [p["port"] for p in service["spec"]["ports"]], url


def task_service_port(objects: list[dict], name: str) -> int:
    [port] = [
        p["port"]
        for p in find(objects, "Service", "tasks", SYSTEM)["spec"]["ports"]
        if p["name"] == name
    ]
    return port


def test_the_task_service_listens_where_its_service_and_settings_say() -> None:
    objects = render(EXTERNAL_SECRETS)
    deployment = find(objects, "Deployment", "tasks", SYSTEM)
    settings = task_service_settings(env_of(deployment, objects, secret_keys(objects)))
    container_ports = {p["name"]: p["containerPort"] for p in container(deployment)["ports"]}
    service_ports = find(objects, "Service", "tasks", SYSTEM)["spec"]["ports"]

    assert container_ports == {
        "a2a": settings.port,
        "internal-read": settings.internal_read_port,
        "internal-write": settings.internal_write_port,
    }
    assert {p["name"]: p["targetPort"] for p in service_ports} == {n: n for n in container_ports}
    assert {p["name"]: p["port"] for p in service_ports} == container_ports


@pytest.mark.parametrize("name", ["edge", "reconciler", "mcp-tracker-read", "mcp-wiki-read"])
def test_each_caller_of_the_task_service_is_pointed_at_its_own_port(name: str) -> None:
    objects = render(EXTERNAL_SECRETS)
    deployment = find(objects, "Deployment", name, SYSTEM)
    url = env_of(deployment, objects, secret_keys(objects))["GOLEM_TASK_SERVICE_URL"]
    app = deployment["spec"]["template"]["metadata"]["labels"][APP_LABEL]

    match = SERVICE_URL.fullmatch(url)
    assert match and (match[1], match[2]) == ("tasks", SYSTEM), url
    assert int(match[3]) == task_service_port(objects, TASK_SERVICE_PORTS[app])


def test_the_edge_and_the_task_service_get_the_edge_token_from_the_same_secret_key() -> None:
    objects = render(EXTERNAL_SECRETS)
    sources = {}
    for secret in of_kind(objects, "ExternalSecret"):
        for data in secret["spec"]["data"]:
            if data["secretKey"] == "GOLEM_EDGE_TOKEN":
                sources[secret["spec"]["target"]["name"]] = data["remoteRef"]

    assert set(sources) == {"golem-edge", "golem-tasks"}
    assert sources["golem-edge"] == sources["golem-tasks"]


def test_the_job_gets_the_mcp_registry_and_secret_the_task_service_names() -> None:
    objects = render(EXTERNAL_SECRETS)
    env = env_of(find(objects, "Deployment", "tasks"), objects, secret_keys(objects))
    namespace = env["GOLEM_KUBERNETES_NAMESPACE"]

    assert namespace == JOBS
    registry_map = find(objects, "ConfigMap", env["GOLEM_MCP_REGISTRY_CONFIGMAP"], namespace)
    registry = tools.parse_registry(yaml.safe_load(registry_map["data"]["registry.yaml"]))
    assert (namespace, env["GOLEM_JOB_SECRET"]) in secret_keys(objects)
    grants = parse_agent_tools(
        find(objects, "ConfigMap", "golem-config", SYSTEM)["data"]["agent-tools.yaml"]
    )
    assert {g for groups in grants.values() for g in groups} <= {g.name for g in registry.groups}


def test_each_mcp_server_serves_the_group_the_registry_routes_to_it() -> None:
    objects = render(EXTERNAL_SECRETS)
    registry = yaml.safe_load(
        find(objects, "ConfigMap", "golem-mcp-registry", JOBS)["data"]["registry.yaml"]
    )

    for group, entry in registry.items():
        service = find(objects, "Service", SERVICE_URL.fullmatch(entry["url"])[1], SYSTEM)
        [deployment] = [
            d
            for d in deployments()
            if service["spec"]["selector"].items()
            <= d["spec"]["template"]["metadata"]["labels"].items()
        ]
        assert env_of(deployment, objects, secret_keys(objects))["GOLEM_MCP_GROUP"] == group


# --- Pod security -------------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(SETTINGS))
def test_every_deployment_is_hardened_like_the_job(name: str) -> None:
    deployment = find(render(BASE), "Deployment", name, SYSTEM)
    pod = deployment["spec"]["template"]["spec"]
    security = container(deployment)["securityContext"]
    [tmp] = [m["name"] for m in container(deployment)["volumeMounts"] if m["mountPath"] == "/tmp"]

    assert pod["securityContext"]["runAsNonRoot"] is True
    assert pod["securityContext"]["seccompProfile"] == {"type": "RuntimeDefault"}
    assert security["readOnlyRootFilesystem"] is True
    assert security["allowPrivilegeEscalation"] is False
    assert security["capabilities"] == {"drop": ["ALL"]}
    assert {"name": tmp, "emptyDir": {}} in pod["volumes"]
    resources = container(deployment)["resources"]
    assert set(resources["requests"]) == set(resources["limits"]) == {"cpu", "memory"}


@pytest.mark.parametrize("name", sorted(SETTINGS))
def test_only_the_two_kubernetes_clients_get_a_service_account_token(name: str) -> None:
    pod = find(render(BASE), "Deployment", name, SYSTEM)["spec"]["template"]["spec"]

    if name in KUBERNETES_API_USERS:
        assert pod["serviceAccountName"] == KUBERNETES_API_USERS[name]
        assert pod["automountServiceAccountToken"] is True
    else:
        assert pod["serviceAccountName"] not in KUBERNETES_API_USERS.values()
        assert pod["automountServiceAccountToken"] is False


def test_nothing_is_granted_cluster_wide() -> None:
    kinds = {o["kind"] for o in render(BASE)}

    assert not kinds & {"ClusterRole", "ClusterRoleBinding"}
    for role in of_kind(render(BASE), "Role"):
        assert role["metadata"]["namespace"] == JOBS


# --- Network policies ---------------------------------------------------------------------------


def policies() -> list[dict]:
    return of_kind(render(BASE), "NetworkPolicy")


def peers(policy: dict) -> Iterator[dict]:
    for direction, side in (("ingress", "from"), ("egress", "to")):
        for rule in policy["spec"].get(direction, []):
            yield from rule.get(side, [])


@pytest.mark.parametrize("namespace", [SYSTEM, JOBS])
def test_each_namespace_denies_everything_by_default(namespace: str) -> None:
    deny = find(policies(), "NetworkPolicy", "default-deny-all", namespace)

    assert deny["spec"] == {"podSelector": {}, "policyTypes": ["Ingress", "Egress"]}


def test_no_allow_rule_opens_a_whole_network_or_every_namespace() -> None:
    """0.0.0.0/0 or an empty namespaceSelector would turn an allow-list back into allow-all."""
    all_peers = [peer for policy in policies() for peer in peers(policy)]

    assert all_peers
    for peer in all_peers:
        if "ipBlock" in peer:
            assert ipaddress.ip_network(peer["ipBlock"]["cidr"]).prefixlen > 0, peer
        elif "namespaceSelector" in peer:
            assert peer["namespaceSelector"].get("matchLabels"), peer
        else:
            # A podSelector alone selects pods of the policy's own namespace.
            assert peer["podSelector"].get("matchLabels"), peer


def test_every_policy_either_denies_all_or_selects_by_labels() -> None:
    for policy in policies():
        if policy["metadata"]["name"] != "default-deny-all":
            assert policy["spec"]["podSelector"].get("matchLabels"), policy["metadata"]["name"]


def ports_by_peer(rules: list[dict], side: str) -> dict[str, set[int]]:
    """The app label of each same-namespace pod peer, to the ports the rules open to it."""
    opened: dict[str, set[int]] = {}
    for rule in rules:
        for peer in rule.get(side, []):
            if set(peer) == {"podSelector"}:
                app = peer["podSelector"]["matchLabels"][APP_LABEL]
                opened.setdefault(app, set()).update(p["port"] for p in rule["ports"])
    return opened


def test_the_task_service_admits_each_caller_on_its_own_port_only() -> None:
    objects = render(BASE)
    policy = find(objects, "NetworkPolicy", "tasks", SYSTEM)

    assert ports_by_peer(policy["spec"]["ingress"], "from") == {
        app: {task_service_port(objects, port)} for app, port in TASK_SERVICE_PORTS.items()
    }


@pytest.mark.parametrize("app", sorted(TASK_SERVICE_PORTS))
def test_each_caller_may_send_to_its_own_task_service_port_only(app: str) -> None:
    objects = render(BASE)
    [policy] = [
        p
        for p in of_kind(objects, "NetworkPolicy")
        if p["spec"]["podSelector"].get("matchLabels") == {APP_LABEL: app}
    ]

    egress = ports_by_peer(policy["spec"]["egress"], "to")
    assert egress["golem-tasks"] == {task_service_port(objects, TASK_SERVICE_PORTS[app])}


def run_egress() -> list[dict]:
    selecting_runs = [
        p
        for p in policies()
        if p["metadata"]["namespace"] == JOBS
        and p["spec"]["podSelector"].get("matchLabels") == {APP_LABEL: APP_NAME}
    ]
    assert selecting_runs, "no NetworkPolicy selects the run pods"
    assert all("ingress" not in p["spec"] for p in selecting_runs), "runs accept no ingress"
    return [rule for p in selecting_runs for rule in p["spec"].get("egress", [])]


def test_runs_reach_exactly_the_five_destinations_of_adr_0004_plus_dns() -> None:
    egress = run_egress()

    assert len(egress) == 6
    in_cluster = [rule["to"][0] for rule in egress if "podSelector" in rule["to"][0]]
    assert {APP_LABEL: "golem-edge"} in [p["podSelector"]["matchLabels"] for p in in_cluster]
    assert {APP_LABEL: "golem-mcp"} in [p["podSelector"]["matchLabels"] for p in in_cluster]


def test_runs_resolve_names_through_kube_dns_only() -> None:
    """Once a policy selects a pod for Egress, DNS lookups are denied like any other traffic."""
    dns = [rule for rule in run_egress() if {"protocol": "UDP", "port": 53} in rule["ports"]]

    assert dns == [
        {
            "to": [
                {
                    "namespaceSelector": {
                        "matchLabels": {"kubernetes.io/metadata.name": "kube-system"}
                    },
                    "podSelector": {"matchLabels": {"k8s-app": "kube-dns"}},
                }
            ],
            "ports": [{"protocol": "UDP", "port": 53}, {"protocol": "TCP", "port": 53}],
        }
    ]
