"""The rendered deploy/k8s/base against a real API server (k3s in a container).

k3s enforces NetworkPolicy with its embedded controller (kube-router's netpol library), so the
traffic tests below are real packets being dropped or answered, not a reading of the YAML.
"""

import time
from collections.abc import Iterator

import pytest
from kubernetes.client import ApiClient, AuthorizationV1Api, CoreV1Api
from kubernetes.client.exceptions import ApiException
from kubernetes.dynamic import DynamicClient
from test_jobs_k3s import busybox_spec
from test_k8s_render import BASE, JOBS, SYSTEM, of_kind, render

from golem.orchestrator.jobs import APP_LABEL, APP_NAME, build_job_manifest

FIELD_MANAGER = "golem-tests"
BUSYBOX = "busybox:1.37"
RUN_AS = 10001
PART_OF = {"app.kubernetes.io/part-of": "golem"}
MCP_TRACKER_LABELS = {APP_LABEL: "golem-mcp", "app.kubernetes.io/instance": "tracker-read"}


def apply(api_client: ApiClient, obj: dict) -> None:
    dynamic = DynamicClient(api_client)
    resource = dynamic.resources.get(api_version=obj["apiVersion"], kind=obj["kind"])
    dynamic.server_side_apply(
        resource,
        body=obj,
        namespace=obj["metadata"].get("namespace"),
        field_manager=FIELD_MANAGER,
        force_conflicts=True,
    )


@pytest.fixture(scope="module")
def deployed(k3s_api_client: ApiClient) -> Iterator[list[dict]]:
    objects = render(BASE)
    for obj in objects:
        apply(k3s_api_client, obj)
    yield objects
    core = CoreV1Api(k3s_api_client)
    for namespace in (SYSTEM, JOBS):
        core.delete_namespace(namespace)


def test_every_rendered_object_is_accepted_by_the_api_server(
    deployed: list[dict], k3s_api_client: ApiClient
) -> None:
    dynamic = DynamicClient(k3s_api_client)
    for obj in deployed:
        resource = dynamic.resources.get(api_version=obj["apiVersion"], kind=obj["kind"])
        found = resource.get(
            name=obj["metadata"]["name"], namespace=obj["metadata"].get("namespace")
        )
        assert found.metadata.name == obj["metadata"]["name"]


def test_every_pod_passes_the_restricted_pod_security_standard(
    deployed: list[dict], k3s_api_client: ApiClient
) -> None:
    """Both namespaces enforce ``restricted``: a dry-run pod from each template must be admitted."""
    core = CoreV1Api(k3s_api_client)
    templates = [
        (d["metadata"]["namespace"], d["spec"]["template"]) for d in of_kind(deployed, "Deployment")
    ]
    job = build_job_manifest(busybox_spec(JOBS, "exit 0"))
    templates.append((JOBS, job["spec"]["template"]))

    for namespace, template in templates:
        pod = {"apiVersion": "v1", "kind": "Pod", "metadata": {"generateName": "psa-"}}
        pod["spec"] = template["spec"]
        core.create_namespaced_pod(namespace, pod, dry_run="All")


@pytest.mark.parametrize("namespace", [SYSTEM, JOBS])
def test_a_pod_without_hardening_is_refused(
    deployed: list[dict], k3s_api_client: ApiClient, namespace: str
) -> None:
    """The negative control of the test above: the namespaces really enforce ``restricted``."""
    pod = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"generateName": "psa-"},
        "spec": {"containers": [{"name": "main", "image": BUSYBOX}]},
    }

    with pytest.raises(ApiException) as refused:
        CoreV1Api(k3s_api_client).create_namespaced_pod(namespace, pod, dry_run="All")

    assert refused.value.status == 403
    assert "restricted" in refused.value.body


# --- RBAC ---------------------------------------------------------------------------------------


def allowed(api_client: ApiClient, account: str, verb: str, resource: str, namespace: str) -> bool:
    """``account`` is ``<namespace>/<service account>``; ``namespace`` "" asks cluster-wide."""
    user_namespace, name = account.split("/")
    review = AuthorizationV1Api(api_client).create_subject_access_review(
        {
            "apiVersion": "authorization.k8s.io/v1",
            "kind": "SubjectAccessReview",
            "spec": {
                "user": f"system:serviceaccount:{user_namespace}:{name}",
                "groups": [
                    "system:serviceaccounts",
                    f"system:serviceaccounts:{user_namespace}",
                    "system:authenticated",
                ],
                "resourceAttributes": {
                    "verb": verb,
                    "group": "batch" if resource == "jobs" else "",
                    "resource": resource,
                    "namespace": namespace,
                },
            },
        }
    )
    return review.status.allowed


TASKS = f"{SYSTEM}/golem-tasks"
RECONCILER = f"{SYSTEM}/golem-reconciler"
RBAC = [
    (TASKS, "create", "jobs", JOBS, True),
    (TASKS, "get", "jobs", JOBS, True),
    (TASKS, "delete", "jobs", JOBS, True),
    (TASKS, "create", "secrets", JOBS, True),
    (TASKS, "get", "secrets", JOBS, False),
    (TASKS, "list", "secrets", JOBS, False),
    (TASKS, "watch", "secrets", JOBS, False),
    (TASKS, "list", "jobs", JOBS, False),
    (TASKS, "create", "jobs", SYSTEM, False),
    (TASKS, "create", "jobs", "", False),
    (TASKS, "get", "secrets", SYSTEM, False),
    (RECONCILER, "get", "jobs", JOBS, True),
    (RECONCILER, "list", "jobs", JOBS, True),
    (RECONCILER, "get", "pods", JOBS, True),
    (RECONCILER, "list", "pods", JOBS, True),
    (RECONCILER, "get", "secrets", JOBS, False),
    (RECONCILER, "list", "secrets", JOBS, False),
    (RECONCILER, "create", "jobs", JOBS, False),
    (RECONCILER, "delete", "jobs", JOBS, False),
    (RECONCILER, "list", "pods", SYSTEM, False),
    *(
        (account, verb, resource, namespace, False)
        for account in (
            f"{SYSTEM}/golem-edge",
            f"{SYSTEM}/golem-jira-adapter",
            f"{SYSTEM}/golem-mattermost-adapter",
            f"{SYSTEM}/golem-mcp",
            f"{SYSTEM}/golem-ui",
            f"{SYSTEM}/default",
            f"{JOBS}/default",
        )
        for verb, resource, namespace in (
            ("create", "jobs", JOBS),
            ("get", "secrets", JOBS),
            ("list", "secrets", SYSTEM),
            ("list", "pods", JOBS),
        )
    ),
]


@pytest.mark.parametrize(("account", "verb", "resource", "namespace", "expected"), RBAC)
def test_service_accounts_can_do_exactly_what_their_process_needs(
    deployed: list[dict],
    k3s_api_client: ApiClient,
    account: str,
    verb: str,
    resource: str,
    namespace: str,
    expected: bool,
) -> None:
    assert allowed(k3s_api_client, account, verb, resource, namespace) is expected


# --- NetworkPolicy, with real traffic -----------------------------------------------------------


def hardened_pod(
    name: str, labels: dict[str, str], script: str, *, listen: dict[str, int] | None = None
) -> dict:
    """A busybox pod that the ``restricted`` Pod Security Standard admits."""
    spec: dict = {
        "name": "main",
        "image": BUSYBOX,
        "command": ["sh", "-c", script],
        "resources": {
            "requests": {"cpu": "50m", "memory": "32Mi"},
            "limits": {"cpu": "50m", "memory": "32Mi"},
        },
        "securityContext": {
            "allowPrivilegeEscalation": False,
            "capabilities": {"drop": ["ALL"]},
            "readOnlyRootFilesystem": True,
        },
        "volumeMounts": [{"name": "tmp", "mountPath": "/tmp"}],
    }
    if listen:
        # Named like the real containers' ports: the Services target them by name.
        spec["ports"] = [{"name": n, "containerPort": port} for n, port in listen.items()]
        # Probed from inside the pod: Ready means every listener works, so a blocked connection
        # is the policy's doing, not a server that never came up.
        probe = " && ".join(f"wget -q -O- http://127.0.0.1:{port}/" for port in listen.values())
        spec["readinessProbe"] = {"exec": {"command": ["sh", "-c", probe]}, "periodSeconds": 1}
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"name": name, "labels": {**PART_OF, **labels}},
        "spec": {
            "restartPolicy": "Never",
            "automountServiceAccountToken": False,
            "securityContext": {
                "runAsNonRoot": True,
                "runAsUser": RUN_AS,
                "runAsGroup": RUN_AS,
                "seccompProfile": {"type": "RuntimeDefault"},
            },
            "containers": [spec],
            "volumes": [{"name": "tmp", "emptyDir": {}}],
        },
    }


def http_server(name: str, labels: dict[str, str], ports: dict[str, int]) -> dict:
    *background, foreground = ports.values()
    script = "; ".join(
        [
            f"mkdir -p /tmp/www && echo {name} > /tmp/www/index.html",
            *(f"httpd -p {port} -h /tmp/www" for port in background),
            f"httpd -f -p {foreground} -h /tmp/www",
        ]
    )
    return hardened_pod(name, labels, script, listen=ports)


def pod_state(core: CoreV1Api, namespace: str) -> str:
    lines = []
    for pod in core.list_namespaced_pod(namespace).items:
        waiting = [
            f"{c.name}: {c.state.waiting.reason} {c.state.waiting.message or ''}"
            for c in (pod.status.container_statuses or [])
            if c.state and c.state.waiting
        ]
        lines.append(f"pod {pod.metadata.name} {pod.status.phase} {waiting}")
    for event in core.list_namespaced_event(namespace).items[-15:]:
        lines.append(f"event {event.involved_object.name}: {event.reason} {event.message}")
    return "\n".join(lines)


def wait_until(core: CoreV1Api, namespace: str, name: str, done, timeout: float = 180):
    # CI runners pull images into a fresh cluster slowly; the state explains a timeout there.
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        pod = core.read_namespaced_pod(name, namespace)
        if done(pod):
            return pod
        time.sleep(1)
    pytest.fail(f"{namespace}/{name} not done after {timeout}s\n{pod_state(core, namespace)}")


def pod_log(core: CoreV1Api, namespace: str, name: str) -> str:
    # Preloaded, the client returns the log as the repr of bytes.
    response = core.read_namespaced_pod_log(name, namespace, _preload_content=False)
    return response.data.decode()


def is_ready(pod) -> bool:
    return any(c.type == "Ready" and c.status == "True" for c in pod.status.conditions or [])


def has_finished(pod) -> bool:
    return pod.status.phase in ("Succeeded", "Failed")


def attempt(label: str, command: str, success: str, failure: str) -> str:
    # The policy controller programs a new pod's rules a moment after it starts, and until then
    # the default deny drops everything. So each check retries, and the scripts run the checks
    # that must pass first: a later "blocked" is the allow-list's answer, not an early drop.
    return (
        f"r={failure}; for i in 1 2 3 4 5 6 7 8 9 10; do"
        f" if {command} >/dev/null 2>&1; then r={success}; break; fi; sleep 1; done;"
        f" echo {label}=$r"
    )


def reach(label: str, url: str) -> str:
    return attempt(label, f"wget -q -T 2 -O- {url}", "reached", "blocked")


@pytest.fixture(scope="module")
def traffic(deployed: list[dict], k3s_api_client: ApiClient) -> dict[str, str]:
    """Stand-ins for the MCP server, Postgres and another run; a run pod tries all three."""
    core = CoreV1Api(k3s_api_client)
    servers = [
        (SYSTEM, http_server("stand-in-mcp", MCP_TRACKER_LABELS, {"http": 8000})),
        (SYSTEM, http_server("stand-in-postgres", {APP_LABEL: "postgres"}, {"http": 5432})),
        (
            JOBS,
            http_server(
                "other-run", {APP_LABEL: APP_NAME, "golem.dev/run-id": "other"}, {"http": 8000}
            ),
        ),
    ]
    for namespace, pod in servers:
        core.create_namespaced_pod(namespace, pod)
    ips = {
        pod["metadata"]["name"]: wait_until(
            core, namespace, pod["metadata"]["name"], is_ready
        ).status.pod_ip
        for namespace, pod in servers
    }
    script = "; ".join(
        [
            attempt("dns", "nslookup kubernetes.default.svc.cluster.local", "resolved", "failed"),
            reach("mcp_service", "http://mcp-tracker-read.golem-system.svc:8000/"),
            reach("mcp_pod", f"http://{ips['stand-in-mcp']}:8000/"),
            reach("postgres", f"http://{ips['stand-in-postgres']}:5432/"),
            reach("other_run", f"http://{ips['other-run']}:8000/"),
        ]
    )
    client = hardened_pod("run-client", {APP_LABEL: APP_NAME, "golem.dev/run-id": "client"}, script)
    core.create_namespaced_pod(JOBS, client)
    wait_until(core, JOBS, "run-client", has_finished)
    log = pod_log(core, JOBS, "run-client")
    results = dict(line.split("=", 1) for line in log.split() if "=" in line)
    return results | {"_log": log}


def test_a_run_resolves_names_through_kube_dns(traffic: dict[str, str]) -> None:
    assert traffic.get("dns") == "resolved", traffic["_log"]


def test_a_run_reaches_the_mcp_server_by_its_service_name(traffic: dict[str, str]) -> None:
    assert traffic.get("mcp_service") == "reached", traffic["_log"]
    assert traffic.get("mcp_pod") == "reached", traffic["_log"]


def test_a_run_cannot_reach_postgres_in_the_system_namespace(traffic: dict[str, str]) -> None:
    assert traffic.get("postgres") == "blocked", traffic["_log"]


def test_a_run_cannot_reach_another_run(traffic: dict[str, str]) -> None:
    assert traffic.get("other_run") == "blocked", traffic["_log"]


def test_a_pod_outside_golem_jobs_cannot_reach_the_mcp_server(
    traffic: dict[str, str], k3s_api_client: ApiClient
) -> None:
    """The MCP server's ingress admits run pods only, not every pod that wears the run label."""
    core = CoreV1Api(k3s_api_client)
    ip = core.read_namespaced_pod("stand-in-mcp", SYSTEM).status.pod_ip
    namespace = "golem-outsider"
    core.create_namespace({"metadata": {"name": namespace}})
    try:
        outsider = hardened_pod(
            "outsider",
            {APP_LABEL: APP_NAME},
            reach("mcp", f"http://{ip}:8000/"),
        )
        core.create_namespaced_pod(namespace, outsider)
        wait_until(core, namespace, "outsider", has_finished)
        log = pod_log(core, namespace, "outsider")
    finally:
        core.delete_namespace(namespace)
    assert "mcp=blocked" in log, log


# --- The task service's ports, with real traffic ------------------------------------------------

TASK_SERVICE_PORTS = {"a2a": 8000, "internal-read": 8001, "internal-write": 8002}
# Each caller of the task service, and the one port it must reach; the other two must be
# blocked. The reached port is tried first (see attempt()).
CALLERS = {
    "edge": ({APP_LABEL: "golem-edge"}, "a2a"),
    "mcp": (MCP_TRACKER_LABELS, "internal-read"),
    "reconciler": ({APP_LABEL: "golem-reconciler"}, "internal-write"),
}


@pytest.fixture(scope="module")
def task_service_traffic(deployed: list[dict], k3s_api_client: ApiClient) -> dict[str, str]:
    """A task service stand-in on its three ports; a stand-in of each caller tries all three."""
    core = CoreV1Api(k3s_api_client)
    tasks = http_server("stand-in-tasks", {APP_LABEL: "golem-tasks"}, TASK_SERVICE_PORTS)
    core.create_namespaced_pod(SYSTEM, tasks)
    wait_until(core, SYSTEM, "stand-in-tasks", is_ready)
    for caller, (labels, allowed_port) in CALLERS.items():
        ordered = [allowed_port, *(p for p in TASK_SERVICE_PORTS if p != allowed_port)]
        script = "; ".join(
            reach(
                f"{caller}_{port.replace('-', '_')}",
                f"http://tasks.{SYSTEM}.svc:{TASK_SERVICE_PORTS[port]}/",
            )
            for port in ordered
        )
        core.create_namespaced_pod(SYSTEM, hardened_pod(f"{caller}-client", labels, script))
    logs = []
    for caller in CALLERS:
        wait_until(core, SYSTEM, f"{caller}-client", has_finished)
        logs.append(pod_log(core, SYSTEM, f"{caller}-client"))
    log = "\n".join(logs)
    results = dict(line.split("=", 1) for line in log.split() if "=" in line)
    return results | {"_log": log}


@pytest.mark.parametrize(
    ("caller", "port", "expected"),
    [
        (caller, port, "reached" if port == allowed_port else "blocked")
        for caller, (_, allowed_port) in CALLERS.items()
        for port in TASK_SERVICE_PORTS
    ],
)
def test_each_caller_reaches_its_own_task_service_port_only(
    task_service_traffic: dict[str, str], caller: str, port: str, expected: str
) -> None:
    key = f"{caller}_{port.replace('-', '_')}"
    assert task_service_traffic.get(key) == expected, task_service_traffic["_log"]
