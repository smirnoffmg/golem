"""deploy/k8s/netcheck, rendered: the operator's network check after deploy.

The Jobs stand in for the processes (their labels put them under each process's policy) and
try the traffic matrix that tests/test_k8s_network_k3s.py proves on k3s. These tests check the
check itself: that each client is governed by the policy it claims, that it never takes a real
Service's traffic, and that no "closed" verdict can pass because nothing was listening.
"""

import re

import pytest
from test_k8s_render import BASE, JOBS, K8S, POSTGRES_PLACEHOLDER, SYSTEM, find, of_kind, render

from golem.orchestrator.jobs import APP_LABEL, APP_NAME

NETCHECK = K8S / "netcheck"
LISTENER = "netcheck-run-listener"
CANARY = "netcheck-canary"
CANARY_NAMESPACE = "golem-netcheck"
# Each client Job and the policy of the process it stands in for.
CLIENTS = {
    "netcheck-run": (JOBS, "golem-run-egress"),
    "netcheck-edge": (SYSTEM, "edge"),
    "netcheck-mcp": (SYSTEM, "mcp"),
    "netcheck-reconciler": (SYSTEM, "reconciler"),
}
# Prometheus's stand-in: no process's labels, in a namespace the scrape policy admits.
SCRAPER = "netcheck-monitoring"
MONITORING_NAMESPACE = "golem-netcheck-monitoring"
TASKS = "tasks.golem-system.svc"
POSTGRES = "$(NETCHECK_POSTGRES)"
LISTENER_HOST = f"{LISTENER}.{JOBS}.svc.cluster.local"
DNS = "dns:kubernetes.default.svc.cluster.local"
SETTLED = f"settled:{CANARY}.{CANARY_NAMESPACE}.svc.cluster.local:8000"
# The matrix of ADR 0009, from the side of each caller.
MATRIX = {
    "netcheck-run": {
        SETTLED,
        DNS,
        f"dns:{LISTENER_HOST}",
        "open:edge.golem-system.svc:8000",
        "open:mcp-tracker-read.golem-system.svc:8000",
        "open:mcp-wiki-read.golem-system.svc:8000",
        f"closed:{TASKS}:8000",
        f"closed:{TASKS}:8001",
        f"closed:{TASKS}:8002",
        f"closed:{POSTGRES}",
        f"closed:{LISTENER_HOST}:8000",
    },
    "netcheck-edge": {
        SETTLED,
        DNS,
        f"open:{TASKS}:8000",
        # The run keys, for call tokens (ADR 0014).
        f"open:{TASKS}:8001",
        f"closed:{TASKS}:8002",
        "closed:mcp-tracker-read.golem-system.svc:8000",
    },
    "netcheck-mcp": {
        SETTLED,
        DNS,
        f"open:{TASKS}:8001",
        f"closed:{TASKS}:8000",
        f"closed:{TASKS}:8002",
    },
    "netcheck-reconciler": {
        SETTLED,
        DNS,
        f"open:{TASKS}:8002",
        f"open:{POSTGRES}",
        f"closed:{TASKS}:8000",
        f"closed:{TASKS}:8001",
    },
    # ADR 0013: the metrics port of each process, and none of the ports its callers use.
    SCRAPER: {
        DNS,
        "open:edge.golem-system.svc:9090",
        f"open:{TASKS}:9090",
        "open:mcp-tracker-read.golem-system.svc:9090",
        "open:mcp-wiki-read.golem-system.svc:9090",
        "closed:edge.golem-system.svc:8000",
        f"closed:{TASKS}:8000",
        f"closed:{TASKS}:8001",
        f"closed:{TASKS}:8002",
        "closed:mcp-tracker-read.golem-system.svc:8000",
    },
}
# Every client Job and its namespace.
NAMESPACES = {
    **{name: namespace for name, (namespace, _) in CLIENTS.items()},
    SCRAPER: MONITORING_NAMESPACE,
}
SERVICE_TARGET = re.compile(r"^([a-z0-9-]+)\.([a-z0-9-]+)\.svc(?:\.cluster\.local)?:(\d+)$")


def jobs() -> list[dict]:
    return of_kind(render(NETCHECK), "Job")


def job(name: str) -> dict:
    return find(render(NETCHECK), "Job", name)


def pod_of(job: dict) -> dict:
    return job["spec"]["template"]


def checks(name: str) -> set[str]:
    [container] = pod_of(job(name))["spec"]["containers"]
    return set(container["args"])


def test_the_network_check_renders_one_job_per_caller_a_listener_and_a_canary() -> None:
    assert {j["metadata"]["name"] for j in jobs()} == {*NAMESPACES, LISTENER, CANARY}
    for name, namespace in NAMESPACES.items():
        assert job(name)["metadata"]["namespace"] == namespace


@pytest.mark.parametrize("name", sorted(NAMESPACES))
def test_each_client_tries_the_matrix_of_its_process(name: str) -> None:
    assert checks(name) == MATRIX[name]


@pytest.mark.parametrize("name", sorted(CLIENTS))
def test_each_client_is_governed_by_the_policy_of_the_process_it_stands_in_for(name: str) -> None:
    namespace, policy_name = CLIENTS[name]
    policy = find(of_kind(render(BASE), "NetworkPolicy"), "NetworkPolicy", policy_name, namespace)
    labels = pod_of(job(name))["metadata"]["labels"]

    assert policy["spec"]["podSelector"]["matchLabels"].items() <= labels.items()
    if namespace == SYSTEM:
        dns = find(of_kind(render(BASE), "NetworkPolicy"), "NetworkPolicy", "allow-dns", SYSTEM)
        assert dns["spec"]["podSelector"]["matchLabels"].items() <= labels.items()


def test_the_listener_is_a_run_nothing_may_reach() -> None:
    labels = pod_of(job(LISTENER))["metadata"]["labels"]
    service = find(render(NETCHECK), "Service", LISTENER, JOBS)

    assert labels[APP_LABEL] == APP_NAME
    assert service["spec"]["clusterIP"] == "None"
    assert service["spec"]["selector"].items() <= labels.items()
    # A headless Service resolves to Ready pods only: resolving it proves the listener is up.
    assert "readinessProbe" in pod_of(job(LISTENER))["spec"]["containers"][0]


def test_only_a_clients_own_egress_rules_can_refuse_the_canary() -> None:
    """No policy anywhere selects the canary's namespace, so while a client reaches it, the
    client's egress rules are not in force yet (netcheck.sh, settled)."""
    objects = render(NETCHECK)
    namespace = find(objects, "Namespace", CANARY_NAMESPACE)
    selected = [
        peer["namespaceSelector"]["matchLabels"]
        for policy in of_kind(render(BASE), "NetworkPolicy")
        for direction, side in (("ingress", "from"), ("egress", "to"))
        for rule in policy["spec"].get(direction, [])
        for peer in rule.get(side, [])
        if "namespaceSelector" in peer
    ]
    labels = {**namespace["metadata"]["labels"], "kubernetes.io/metadata.name": CANARY_NAMESPACE}

    assert not of_kind(objects, "NetworkPolicy")
    assert selected
    assert not [s for s in selected if s.items() <= labels.items()]
    service = find(objects, "Service", CANARY, CANARY_NAMESPACE)
    assert service["spec"]["clusterIP"] == "None"
    assert service["spec"]["selector"].items() <= pod_of(job(CANARY))["metadata"]["labels"].items()


@pytest.mark.parametrize("name", sorted(CLIENTS))
def test_no_client_ever_takes_the_traffic_of_a_real_service(name: str) -> None:
    """The edge client wears the edge's labels, so it must never be Ready while it runs."""
    spec = job(name)["spec"]
    [container] = spec["template"]["spec"]["containers"]

    assert container["readinessProbe"]["initialDelaySeconds"] > spec["activeDeadlineSeconds"]


def test_every_closed_target_is_proven_listening() -> None:
    """A "closed" verdict on a dead target would pass: each one is another check's "open", or
    the listener, whose name resolves only while it is Ready."""
    opened = {c.removeprefix("open:") for name in NAMESPACES for c in checks(name) if "open:" in c}
    closed = {
        c.removeprefix("closed:") for name in NAMESPACES for c in checks(name) if "closed:" in c
    }

    assert closed - opened == {f"{LISTENER_HOST}:8000"}
    assert f"dns:{LISTENER_HOST}" in checks("netcheck-run")


def test_every_service_target_is_a_service_port() -> None:
    objects = render(BASE) + render(NETCHECK)
    targets = {
        c.split(":", 1)[1]
        for name in NAMESPACES
        for c in checks(name)
        if "svc" in c and not c.startswith("dns:")
    }

    assert targets
    for target in targets - {f"{LISTENER_HOST}:8000"}:
        match = SERVICE_TARGET.fullmatch(target)
        assert match, target
        service = find(objects, "Service", match[1], match[2])
        assert int(match[3]) in [p["port"] for p in service["spec"]["ports"]], target


def test_the_scraper_is_admitted_by_the_scrape_policy_alone() -> None:
    """Its namespace has the monitoring label and no policies, so the processes' ingress rules
    alone decide what it reaches; it wears no process's labels, so no process policy governs it."""
    objects = render(NETCHECK)
    namespace = find(objects, "Namespace", MONITORING_NAMESPACE)
    scrape = find(of_kind(render(BASE), "NetworkPolicy"), "NetworkPolicy", "allow-metrics-scrape")
    [peer] = scrape["spec"]["ingress"][0]["from"]
    labels = pod_of(job(SCRAPER))["metadata"]["labels"]

    assert (
        peer["namespaceSelector"]["matchLabels"].items() <= namespace["metadata"]["labels"].items()
    )
    assert not [p for p in of_kind(objects, "NetworkPolicy")]
    assert APP_LABEL not in labels
    assert MONITORING_NAMESPACE != CANARY_NAMESPACE


@pytest.mark.parametrize("namespace", [SYSTEM, JOBS])
def test_the_postgres_target_defaults_to_the_policies_placeholder(namespace: str) -> None:
    config = find(render(NETCHECK), "ConfigMap", "golem-netcheck", namespace)

    host, port = config["data"]["NETCHECK_POSTGRES"].rsplit(":", 1)
    assert (f"{host}/32", port) == (POSTGRES_PLACEHOLDER, "5432")
    assert config["data"]["netcheck.sh"].startswith("#!/bin/sh")


def test_every_job_runs_once_and_fails_on_the_first_failure() -> None:
    for j in jobs():
        assert j["spec"]["backoffLimit"] == 0
        assert j["spec"]["template"]["spec"]["restartPolicy"] == "Never"


@pytest.mark.parametrize("name", sorted([*NAMESPACES, LISTENER, CANARY]))
def test_every_job_is_hardened_like_the_platform(name: str) -> None:
    pod = pod_of(job(name))["spec"]
    [container] = pod["containers"]
    security = container["securityContext"]

    assert pod["automountServiceAccountToken"] is False
    assert pod["securityContext"]["runAsNonRoot"] is True
    assert pod["securityContext"]["seccompProfile"] == {"type": "RuntimeDefault"}
    assert security["readOnlyRootFilesystem"] is True
    assert security["allowPrivilegeEscalation"] is False
    assert security["capabilities"] == {"drop": ["ALL"]}
    resources = container["resources"]
    assert set(resources["requests"]) == set(resources["limits"]) == {"cpu", "memory"}
