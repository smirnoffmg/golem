"""What the rendered network policies left to the cluster, exercised on k3s.

tests/test_k8s_manifests.py sends real traffic between stand-ins. Three things it did not
exercise (ADR 0009): kubelet probes into pods under the default deny, egress to the Kubernetes
API server for the processes that call it, and the operator's network check in
deploy/k8s/netcheck, which must pass against the manifests and fail when a policy is broken.

The environment is what an operator's overlay makes of the base: the Kubernetes API placeholder
replaced by the API server's endpoint, Postgres by an in-cluster Postgres (the README's
namespaceSelector and podSelector), and the processes the checks talk to replaced by busybox
stand-ins inside their own Deployments, with their labels, ports and probes untouched.
"""

import copy
import datetime
import json
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass

import pytest
from kubernetes.client import (
    ApiClient,
    AppsV1Api,
    BatchV1Api,
    CoreV1Api,
    DiscoveryV1Api,
    NetworkingV1Api,
)
from kubernetes.client.exceptions import ApiException
from test_k8s_manifests import (
    BUSYBOX,
    apply,
    hardened_pod,
    has_finished,
    http_server,
    is_ready,
    pod_log,
    pod_state,
    wait_until,
)
from test_k8s_netcheck import CANARY_NAMESPACE, CLIENTS, MATRIX, NETCHECK, SETTLED
from test_k8s_render import BASE, JOBS, POSTGRES_PLACEHOLDER, SYSTEM, find, of_kind, render

from golem.orchestrator.jobs import APP_LABEL

# Minutes of real traffic in k3s: a separate CI stage keeps the fast checks fast.
pytestmark = pytest.mark.e2e


API_PLACEHOLDER = "192.0.2.1/32"
POSTGRES_NAMESPACE = "golem-postgres"
POSTGRES_ADDRESS = f"postgres.{POSTGRES_NAMESPACE}.svc:5432"
# The processes a check talks to; the others have no role in these tests and are not deployed.
STAND_INS = ("edge", "tasks", "mcp-tracker-read", "mcp-wiki-read")
CURL = "curlimages/curl:8.16.0"
TIMEOUT = 180


@dataclass(frozen=True)
class Cluster:
    objects: list[dict]
    api_endpoint: tuple[str, int]


def wait_for_namespaces_gone(core: CoreV1Api, names: tuple[str, ...]) -> None:
    deadline = time.monotonic() + TIMEOUT
    while time.monotonic() < deadline:
        present = {n.metadata.name for n in core.list_namespace().items} & set(names)
        if not present:
            return
        time.sleep(1)
    pytest.fail(f"namespaces {present} still terminating after {TIMEOUT}s")


def api_server_endpoint(api_client: ApiClient) -> tuple[str, int]:
    """What the README tells the operator to look up: the ``kubernetes`` EndpointSlice."""
    [endpoints] = (
        DiscoveryV1Api(api_client)
        .list_namespaced_endpoint_slice(
            "default", label_selector="kubernetes.io/service-name=kubernetes"
        )
        .items
    )
    return endpoints.endpoints[0].addresses[0], endpoints.ports[0].port


def replace_placeholders(objects: list[dict], api_address: str) -> list[dict]:
    """The operator's overlay, as the README describes it, applied to the rendered base."""
    replacements = {
        API_PLACEHOLDER: {"ipBlock": {"cidr": f"{api_address}/32"}},
        POSTGRES_PLACEHOLDER: {
            "namespaceSelector": {
                "matchLabels": {"kubernetes.io/metadata.name": POSTGRES_NAMESPACE}
            },
            "podSelector": {"matchLabels": {APP_LABEL: "postgres"}},
        },
    }
    replaced = copy.deepcopy(objects)
    for policy in of_kind(replaced, "NetworkPolicy"):
        for direction, side in (("ingress", "from"), ("egress", "to")):
            for rule in policy["spec"].get(direction, []):
                rule[side] = [
                    replacements.get(peer.get("ipBlock", {}).get("cidr"), peer)
                    for peer in rule.get(side, [])
                ]
    return replaced


def stand_in(deployment: dict) -> dict:
    """The Deployment with busybox serving each container port; labels and probes untouched."""
    replaced = copy.deepcopy(deployment)
    pod = replaced["spec"]["template"]["spec"]
    [container] = pod["containers"]
    *background, foreground = [p["containerPort"] for p in container["ports"]]
    script = "; ".join(
        [
            # The task service's probe path.
            "mkdir -p /tmp/www/internal && echo ok > /tmp/www/internal/run-keys",
            *(f"httpd -p {port} -h /tmp/www" for port in background),
            f"exec httpd -f -p {foreground} -h /tmp/www",
        ]
    )
    container |= {"image": BUSYBOX, "command": ["sh", "-c", script]}
    container.pop("envFrom", None)
    container["volumeMounts"] = [m for m in container["volumeMounts"] if m["mountPath"] == "/tmp"]
    pod["volumes"] = [v for v in pod["volumes"] if "emptyDir" in v]
    return replaced


def wait_until_available(api_client: ApiClient, name: str) -> None:
    apps = AppsV1Api(api_client)
    deadline = time.monotonic() + TIMEOUT
    while time.monotonic() < deadline:
        status = apps.read_namespaced_deployment(name, SYSTEM)
        if (status.status.ready_replicas or 0) == status.spec.replicas:
            return
        time.sleep(1)
    state = pod_state(CoreV1Api(api_client), SYSTEM)
    pytest.fail(f"{name} not available after {TIMEOUT}s\n{state}")


@pytest.fixture(scope="module")
def cluster(k3s_api_client: ApiClient) -> Iterator[Cluster]:
    core = CoreV1Api(k3s_api_client)
    namespaces = (SYSTEM, JOBS, POSTGRES_NAMESPACE, CANARY_NAMESPACE)
    # test_k8s_manifests.py deletes the same namespaces on its way out.
    wait_for_namespaces_gone(core, namespaces)
    endpoint = api_server_endpoint(k3s_api_client)
    objects = replace_placeholders(render(BASE), endpoint[0])
    for obj in objects:
        if obj["kind"] != "Deployment":
            apply(k3s_api_client, obj)
        elif obj["metadata"]["name"] in STAND_INS:
            apply(k3s_api_client, stand_in(obj))
    core.create_namespace({"metadata": {"name": POSTGRES_NAMESPACE}})
    core.create_namespaced_pod(
        POSTGRES_NAMESPACE, http_server("postgres", {APP_LABEL: "postgres"}, {"postgres": 5432})
    )
    core.create_namespaced_service(
        POSTGRES_NAMESPACE,
        {
            "metadata": {"name": "postgres"},
            "spec": {"selector": {APP_LABEL: "postgres"}, "ports": [{"port": 5432}]},
        },
    )
    for name in STAND_INS:
        wait_until_available(k3s_api_client, name)
    wait_until(core, POSTGRES_NAMESPACE, "postgres", is_ready)
    yield Cluster(objects, endpoint)
    for namespace in namespaces:
        core.delete_namespace(namespace)
    wait_for_namespaces_gone(core, namespaces)


# --- Kubelet probes -----------------------------------------------------------------------------


@pytest.mark.parametrize("name", STAND_INS)
def test_kubelet_probes_reach_pods_under_the_default_deny(
    cluster: Cluster, k3s_api_client: ApiClient, name: str
) -> None:
    """The probes come from the node, which no policy admits; the pods must still be Ready.

    ``tasks`` has HTTP probes, the others TCP. Ready proves the readiness probe got through;
    staying Ready without a restart past the liveness probe's first run proves that one did.
    """
    core = CoreV1Api(k3s_api_client)
    deployment = find(cluster.objects, "Deployment", name, SYSTEM)
    [container] = deployment["spec"]["template"]["spec"]["containers"]
    liveness = container["livenessProbe"]
    settle = datetime.timedelta(
        seconds=liveness.get("initialDelaySeconds", 0) + liveness["periodSeconds"] + 5
    )
    labels = deployment["spec"]["selector"]["matchLabels"]
    selector = ",".join(f"{k}={v}" for k, v in labels.items())
    pods = core.list_namespaced_pod(SYSTEM, label_selector=selector).items
    names = [p.metadata.name for p in pods]
    assert len(names) == deployment["spec"]["replicas"], names

    for pod_name in names:
        while True:
            pod = core.read_namespaced_pod(pod_name, SYSTEM)
            [status] = pod.status.container_statuses
            assert is_ready(pod), pod_state(core, SYSTEM)
            assert status.restart_count == 0, pod_state(core, SYSTEM)
            if datetime.datetime.now(datetime.UTC) - pod.status.start_time > settle:
                break
            time.sleep(1)
        events = core.list_namespaced_event(
            SYSTEM, field_selector=f"involvedObject.name={pod_name}"
        ).items
        failed = [e.message for e in events if e.reason == "Unhealthy" and "Liveness" in e.message]
        assert not failed, failed


# --- Egress to the Kubernetes API ---------------------------------------------------------------


def api_client_pod(name: str, labels: dict[str, str], canary: str) -> dict:
    """A pod under the task service's account that creates and deletes a Job in golem-jobs,
    through the in-cluster address (the ``kubernetes`` Service), as the client library does.

    kube-router lets a new pod's traffic through unfiltered until it has programmed the pod's
    rules (about a second in k3s), so the pod first waits until it can no longer reach
    ``canary``, a listener that no rule admits and nothing but its own egress rules refuses.
    """
    job = {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {"name": name},
        # Suspended: the Job object is the point, not a pod.
        "spec": {
            "suspend": True,
            "template": {
                "spec": {
                    "restartPolicy": "Never",
                    "containers": [{"name": "main", "image": BUSYBOX}],
                }
            },
        },
    }
    script = f"""
for i in $(seq 30); do
  curl -s -o /dev/null --connect-timeout 1 --max-time 2 http://{canary}:8000/ || break
  sleep 0.5
done
sa=/var/run/secrets/kubernetes.io/serviceaccount
jobs=https://$KUBERNETES_SERVICE_HOST:$KUBERNETES_SERVICE_PORT/apis/batch/v1/namespaces/{JOBS}/jobs
call() {{
  curl -s -o /dev/null -w '%{{http_code}}' --connect-timeout 2 --max-time 5 \\
    --cacert $sa/ca.crt -H "Authorization: Bearer $(cat $sa/token)" "$@"
}}
for i in 1 2 3 4 5 6 7 8 9 10; do
  code=$(call -X POST -H 'Content-Type: application/json' -d '{json.dumps(job)}' "$jobs")
  [ "$code" = 201 ] && break
  sleep 1
done
echo create=$code
echo delete=$(call -X DELETE "$jobs/{name}")
"""
    pod = hardened_pod(name, labels, script)
    [container] = pod["spec"]["containers"]
    container["image"] = CURL
    container["resources"] = {
        "requests": {"cpu": "100m", "memory": "64Mi"},
        "limits": {"cpu": "100m", "memory": "64Mi"},
    }
    # Never Ready: wearing the task service's labels, it must not join the tasks Service.
    container["readinessProbe"] = {"exec": {"command": ["true"]}, "initialDelaySeconds": 3600}
    pod["spec"]["serviceAccountName"] = "golem-tasks"
    pod["spec"]["automountServiceAccountToken"] = True
    return pod


# Each pod has the task service's token, so only the network decides whether its call arrives.
API_CALLERS = {
    # The tasks policy, with the placeholder replaced by the API server's endpoint.
    "api-tasks": ({APP_LABEL: "golem-tasks"}, "create=201 delete=200"),
    # The MCP servers' policy has no rule for the API server.
    "api-mcp": ({APP_LABEL: "golem-mcp"}, "create=000 delete=000"),
    # A policy naming the kubernetes Service's ClusterIP, which the pod connects to.
    "api-cluster-ip": ({APP_LABEL: "api-cluster-ip"}, "create=000 delete=000"),
}


@pytest.fixture(scope="module")
def api_calls(cluster: Cluster, k3s_api_client: ApiClient) -> dict[str, str]:
    core = CoreV1Api(k3s_api_client)
    cluster_ip = core.read_namespaced_service("kubernetes", "default").spec.cluster_ip
    apply(
        k3s_api_client,
        {
            "apiVersion": "networking.k8s.io/v1",
            "kind": "NetworkPolicy",
            "metadata": {"name": "test-api-cluster-ip", "namespace": SYSTEM},
            "spec": {
                "podSelector": {"matchLabels": {APP_LABEL: "api-cluster-ip"}},
                "policyTypes": ["Egress"],
                "egress": [
                    {
                        "to": [{"ipBlock": {"cidr": f"{cluster_ip}/32"}}],
                        "ports": [{"protocol": "TCP", "port": 443}],
                    }
                ],
            },
        },
    )
    canary = http_server("canary", {APP_LABEL: "canary"}, {"http": 8000})
    core.create_namespaced_pod(POSTGRES_NAMESPACE, canary)
    canary_ip = wait_until(core, POSTGRES_NAMESPACE, "canary", is_ready).status.pod_ip
    for name, (labels, _) in API_CALLERS.items():
        core.create_namespaced_pod(SYSTEM, api_client_pod(name, labels, canary_ip))
    logs = {}
    for name in API_CALLERS:
        wait_until(core, SYSTEM, name, has_finished)
        logs[name] = " ".join(pod_log(core, SYSTEM, name).split())
        core.delete_namespaced_pod(name, SYSTEM)
    return logs


def test_the_api_placeholder_is_replaced_by_the_endpoint_the_policies_open(
    cluster: Cluster,
) -> None:
    address, port = cluster.api_endpoint
    [rule] = [
        rule
        for rule in find(cluster.objects, "NetworkPolicy", "tasks", SYSTEM)["spec"]["egress"]
        if {"ipBlock": {"cidr": f"{address}/32"}} in rule["to"]
    ]
    assert port in [p["port"] for p in rule["ports"]]


@pytest.mark.parametrize("name", API_CALLERS)
def test_only_a_policy_naming_the_api_servers_endpoint_lets_a_call_arrive(
    api_calls: dict[str, str], name: str
) -> None:
    assert api_calls[name] == API_CALLERS[name][1], api_calls


# --- The operator's network check ---------------------------------------------------------------


def netcheck_objects() -> list[dict]:
    objects = copy.deepcopy(render(NETCHECK))
    for config in of_kind(objects, "ConfigMap"):
        config["data"]["NETCHECK_POSTGRES"] = POSTGRES_ADDRESS
    return objects


def wait_for_job(api_client: ApiClient, namespace: str, name: str) -> tuple[bool, str]:
    batch, core = BatchV1Api(api_client), CoreV1Api(api_client)
    deadline = time.monotonic() + 360
    while time.monotonic() < deadline:
        status = batch.read_namespaced_job(name, namespace).status
        if status.succeeded or status.failed:
            [pod] = core.list_namespaced_pod(namespace, label_selector=f"job-name={name}").items
            return bool(status.succeeded), pod_log(core, namespace, pod.metadata.name)
        time.sleep(1)
    pytest.fail(f"{namespace}/{name} not finished\n{pod_state(core, namespace)}")


def delete_jobs(api_client: ApiClient, objects: list[dict]) -> None:
    batch = BatchV1Api(api_client)
    jobs = [(j["metadata"]["namespace"], j["metadata"]["name"]) for j in of_kind(objects, "Job")]
    for namespace, name in jobs:
        batch.delete_namespaced_job(name, namespace, propagation_policy="Background")
    deadline = time.monotonic() + TIMEOUT
    for namespace, name in jobs:
        while time.monotonic() < deadline:
            try:
                batch.read_namespaced_job(name, namespace)
            except ApiException as gone:
                assert gone.status == 404
                break
            time.sleep(1)


def run_netcheck(api_client: ApiClient) -> dict[str, tuple[bool, str]]:
    """Each client Job's success and log, as the operator would read them."""
    objects = netcheck_objects()
    for obj in objects:
        apply(api_client, obj)
    try:
        return {
            name: wait_for_job(api_client, namespace, name)
            for name, (namespace, _) in CLIENTS.items()
        }
    finally:
        delete_jobs(api_client, objects)


def failures(log: str) -> set[str]:
    return {line.removeprefix("FAIL ") for line in log.splitlines() if line.startswith("FAIL ")}


def test_the_network_check_passes_against_the_manifests(
    cluster: Cluster, k3s_api_client: ApiClient
) -> None:
    results = run_netcheck(k3s_api_client)

    for name, (succeeded, log) in results.items():
        assert succeeded, f"{name}\n{log}"
        passed = [line for line in log.splitlines() if line.startswith("PASS ")]
        assert len(passed) == len(MATRIX[name]), f"{name}\n{log}"


def remove(name: str, namespace: str) -> Callable[[ApiClient, Cluster], None]:
    def breaks(api_client: ApiClient, cluster: Cluster) -> None:
        NetworkingV1Api(api_client).delete_namespaced_network_policy(name, namespace)

    return breaks


def admit_runs_to_postgres(api_client: ApiClient, cluster: Cluster) -> None:
    """The in-cluster Postgres rule of the other policies, pasted into the runs' by mistake."""
    [postgres] = [
        rule
        for rule in find(cluster.objects, "NetworkPolicy", "tasks", SYSTEM)["spec"]["egress"]
        if {"protocol": "TCP", "port": 5432} in rule["ports"]
    ]
    policy = copy.deepcopy(find(cluster.objects, "NetworkPolicy", "golem-run-egress", JOBS))
    policy["spec"]["egress"].append(postgres)
    apply(api_client, policy)


BREAKAGES = {
    # Runs lose every allow: the run client's names and opens fail (the settled check too, since
    # the canary's name no longer resolves), its closed checks pass.
    "without-run-egress": (
        remove("golem-run-egress", JOBS),
        ("golem-run-egress", JOBS),
        {
            "netcheck-run": {
                SETTLED,
                "dns:kubernetes.default.svc.cluster.local",
                f"dns:netcheck-run-listener.{JOBS}.svc.cluster.local",
                "open:edge.golem-system.svc:8000",
                "open:mcp-tracker-read.golem-system.svc:8000",
                "open:mcp-wiki-read.golem-system.svc:8000",
            }
        },
    ),
    # A policy that allows too much: only a closed check can see it. Postgres is outside the
    # default-deny namespaces, so the runs' egress rules are all that keep runs from it.
    "run-egress-admits-postgres": (
        admit_runs_to_postgres,
        ("golem-run-egress", JOBS),
        {"netcheck-run": {f"closed:{POSTGRES_ADDRESS}"}},
    ),
}


@pytest.mark.parametrize("breakage", BREAKAGES)
def test_the_network_check_fails_when_a_policy_is_broken(
    cluster: Cluster, k3s_api_client: ApiClient, breakage: str
) -> None:
    breaks, (policy, namespace), expected = BREAKAGES[breakage]
    breaks(k3s_api_client, cluster)
    try:
        results = run_netcheck(k3s_api_client)
    finally:
        apply(k3s_api_client, find(cluster.objects, "NetworkPolicy", policy, namespace))

    logs = "\n".join(f"{name}\n{log}" for name, (_, log) in results.items())
    assert {name for name, (succeeded, _) in results.items() if not succeeded} == set(expected)
    assert {name: failures(log) for name, (_, log) in results.items() if failures(log)} == (
        expected
    ), logs
