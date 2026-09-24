"""The launcher against a real Kubernetes API (k3s in a container, started once per session)."""

import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import replace

import pytest
from kubernetes.client import ApiClient, BatchV1Api, CoreV1Api, NetworkingV1Api

from golem.orchestrator.jobs import (
    CatalogRef,
    Cidr,
    Destination,
    EgressAllowList,
    InCluster,
    JobNameTaken,
    JobSpec,
    JobStatus,
    KubernetesJobLauncher,
    build_job_manifest,
    build_network_policy,
    job_name,
)

NAMESPACE = "golem-jobs-test"
SECRET = "golem-run-secrets"


@pytest.fixture(scope="module")
def namespace(k3s_api_client: ApiClient) -> Iterator[str]:
    core = CoreV1Api(k3s_api_client)
    core.create_namespace({"metadata": {"name": NAMESPACE}})
    # The Job refuses to start without its secret, so the test plays the secrets operator.
    core.create_namespaced_secret(
        NAMESPACE,
        {"metadata": {"name": SECRET}, "stringData": {"MODEL_GATEWAY_KEY": "test-only"}},
    )
    yield NAMESPACE
    core.delete_namespace(NAMESPACE)


@pytest.fixture
def launcher(k3s_api_client: ApiClient, namespace: str) -> KubernetesJobLauncher:
    return KubernetesJobLauncher(k3s_api_client, namespace)


def busybox_spec(namespace: str, script: str) -> JobSpec:
    return JobSpec(
        run_id=str(uuid.uuid4()),
        agent="reviewer",
        image="busybox:1.37",
        namespace=namespace,
        catalog_ref=CatalogRef(url="https://git.example/team/catalog.git", revision="a1b2c3d"),
        goal="test",
        secret_name=SECRET,
        active_deadline_seconds=120,
        ttl_seconds_after_finished=600,
        cpu="100m",
        memory="64Mi",
        command=("sh", "-c", script),
    )


def wait_for(read: Callable[[], JobStatus], expected: JobStatus, timeout: float = 120) -> None:
    deadline = time.monotonic() + timeout
    seen = read()
    while seen is not expected and time.monotonic() < deadline:
        time.sleep(1)
        seen = read()
    assert seen is expected


def test_successful_command_is_observed_as_succeeded(launcher: KubernetesJobLauncher) -> None:
    # Writes to the workspace and /tmp prove the emptyDirs are writable under a read-only root
    # and a non-root user; the secret shows up only through envFrom.
    job = busybox_spec(
        launcher.namespace,
        'touch /workspace/f /tmp/f && ! touch /f 2>/dev/null && [ "$(id -u)" != 0 ]'
        ' && [ "$MODEL_GATEWAY_KEY" = test-only ] && [ -n "$GOLEM_RUN_ID" ]',
    )

    launcher.launch(job)

    wait_for(lambda: launcher.status(job.run_id), JobStatus.SUCCEEDED)


def test_failing_command_is_observed_as_failed(launcher: KubernetesJobLauncher) -> None:
    job = busybox_spec(launcher.namespace, "exit 3")

    launcher.launch(job)

    wait_for(lambda: launcher.status(job.run_id), JobStatus.FAILED)


def test_long_command_is_observed_as_running(launcher: KubernetesJobLauncher) -> None:
    job = busybox_spec(launcher.namespace, "sleep 300")

    launcher.launch(job)

    wait_for(lambda: launcher.status(job.run_id), JobStatus.RUNNING)
    launcher.delete(job.run_id)


def test_launch_twice_creates_one_job(
    launcher: KubernetesJobLauncher, k3s_api_client: ApiClient
) -> None:
    job = busybox_spec(launcher.namespace, "exit 0")

    launcher.launch(job)
    launcher.launch(job)

    jobs = BatchV1Api(k3s_api_client).list_namespaced_job(
        launcher.namespace, label_selector=f"golem.dev/run-id={job.run_id}"
    )
    assert [j.metadata.name for j in jobs.items] == [job_name(job.run_id)]


def test_launch_refuses_a_foreign_job_with_the_same_name(
    launcher: KubernetesJobLauncher, k3s_api_client: ApiClient
) -> None:
    job = busybox_spec(launcher.namespace, "exit 0")
    foreign = build_job_manifest(job)
    foreign["metadata"]["labels"]["golem.dev/run-id"] = "someone-else"
    BatchV1Api(k3s_api_client).create_namespaced_job(launcher.namespace, foreign)

    with pytest.raises(JobNameTaken):
        launcher.launch(job)


def test_delete_makes_the_job_missing(launcher: KubernetesJobLauncher) -> None:
    job = busybox_spec(launcher.namespace, "sleep 300")
    launcher.launch(job)

    launcher.delete(job.run_id)

    wait_for(lambda: launcher.status(job.run_id), JobStatus.MISSING, timeout=30)


def test_unknown_run_is_missing_and_deleting_it_is_quiet(launcher: KubernetesJobLauncher) -> None:
    run_id = str(uuid.uuid4())

    assert launcher.status(run_id) is JobStatus.MISSING
    launcher.delete(run_id)


def test_launch_applies_the_spec_namespace_not_another(
    launcher: KubernetesJobLauncher,
) -> None:
    with pytest.raises(ValueError):
        launcher.launch(replace(busybox_spec(launcher.namespace, "exit 0"), namespace="default"))


def test_api_server_accepts_the_network_policy(k3s_api_client: ApiClient, namespace: str) -> None:
    allow = EgressAllowList(
        a2a_edge=Destination(
            InCluster(
                namespace_labels={"kubernetes.io/metadata.name": "golem"},
                pod_labels={"app.kubernetes.io/name": "golem-edge"},
            ),
            ports=(8443,),
        ),
        model_gateway=Destination(Cidr("10.20.1.0/24"), ports=(4000,)),
        trace_store=Destination(Cidr("10.20.2.0/24"), ports=(443,)),
        mcp_servers=Destination(
            InCluster(namespace_labels={"kubernetes.io/metadata.name": "golem"}), ports=(8080,)
        ),
        git_host=Destination(Cidr("192.0.2.10/32"), ports=(443, 22)),
    )

    created = NetworkingV1Api(k3s_api_client).create_namespaced_network_policy(
        namespace, build_network_policy(namespace, allow)
    )

    assert len(created.spec.egress) == 6
