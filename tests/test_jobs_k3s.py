"""The launcher against a real Kubernetes API (k3s in a container, started once per session)."""

import time
import uuid
from collections.abc import Iterator
from dataclasses import replace

import pytest
from kubernetes.client import ApiClient, BatchV1Api, CoreV1Api, V1Secret
from kubernetes.client.exceptions import ApiException

from golem.orchestrator.jobs import (
    CatalogRef,
    JobNameTaken,
    JobSpec,
    JobStatus,
    KubernetesJobLauncher,
    build_job_manifest,
    job_name,
    token_secret_name,
)
from golem.run_token import RunClaims, SigningKey, issue

NAMESPACE = "golem-jobs-test"
SECRET = "golem-run-secrets"
MCP_CONFIGMAP = "golem-mcp-registry"
SIGNING_KEY = SigningKey.generate(kid="k3s-test")


@pytest.fixture(scope="module")
def namespace(k3s_api_client: ApiClient) -> Iterator[str]:
    core = CoreV1Api(k3s_api_client)
    core.create_namespace({"metadata": {"name": NAMESPACE}})
    # The Job refuses to start without its secret, so the test plays the secrets operator.
    core.create_namespaced_secret(
        NAMESPACE,
        {"metadata": {"name": SECRET}, "stringData": {"MODEL_GATEWAY_KEY": "test-only"}},
    )
    # The platform's MCP registry, which a deployment keeps in a ConfigMap.
    core.create_namespaced_config_map(
        NAMESPACE,
        {
            "metadata": {"name": MCP_CONFIGMAP},
            "data": {"registry.yaml": "wiki.read:\n  url: http://mcp:8080/mcp\n"},
        },
    )
    yield NAMESPACE
    core.delete_namespace(NAMESPACE)


@pytest.fixture
def launcher(k3s_api_client: ApiClient, namespace: str) -> KubernetesJobLauncher:
    return KubernetesJobLauncher(k3s_api_client, namespace)


def token_for(run_id: str) -> str:
    claims = RunClaims(run_id, "reviewer", "user:alice", run_id, ("wiki.read",), 2_000_000_000)
    return issue(claims, SIGNING_KEY, now=1_800_000_000)


def busybox_spec(namespace: str, script: str) -> JobSpec:
    run_id = str(uuid.uuid4())
    return JobSpec(
        run_id=run_id,
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
        run_token=token_for(run_id),
    )


def cluster_state(launcher: KubernetesJobLauncher) -> str:
    core = CoreV1Api(launcher.api_client)
    lines = []
    for pod in core.list_namespaced_pod(launcher.namespace).items:
        waiting = [
            f"{c.name}: {c.state.waiting.reason} {c.state.waiting.message or ''}"
            for c in (pod.status.container_statuses or [])
            if c.state and c.state.waiting
        ]
        lines.append(f"pod {pod.metadata.name} {pod.status.phase} {waiting}")
    for event in core.list_namespaced_event(launcher.namespace).items[-15:]:
        lines.append(f"event {event.involved_object.name}: {event.reason} {event.message}")
    return "\n".join(lines)


def wait_for_job(
    launcher: KubernetesJobLauncher, run_id: str, expected: JobStatus, timeout: float = 300
) -> None:
    # CI runners pull images into a fresh cluster slowly; the state explains a timeout there.
    deadline = time.monotonic() + timeout
    seen = launcher.status(run_id)
    while seen is not expected and time.monotonic() < deadline:
        time.sleep(1)
        seen = launcher.status(run_id)
    assert seen is expected, f"{seen} after {timeout}s\n{cluster_state(launcher)}"


def test_successful_command_is_observed_as_succeeded(launcher: KubernetesJobLauncher) -> None:
    # Writes to the workspace and /tmp prove the emptyDirs are writable under a read-only root
    # and a non-root user; the secret shows up only through envFrom.
    job = busybox_spec(
        launcher.namespace,
        'touch /workspace/f /tmp/f && ! touch /f 2>/dev/null && [ "$(id -u)" != 0 ]'
        ' && [ "$MODEL_GATEWAY_KEY" = test-only ] && [ -n "$GOLEM_RUN_ID" ]',
    )

    launcher.launch(job)

    wait_for_job(launcher, job.run_id, JobStatus.SUCCEEDED)


def test_failing_command_is_observed_as_failed(launcher: KubernetesJobLauncher) -> None:
    job = busybox_spec(launcher.namespace, "exit 3")

    launcher.launch(job)

    wait_for_job(launcher, job.run_id, JobStatus.FAILED)


def test_long_command_is_observed_as_running(launcher: KubernetesJobLauncher) -> None:
    job = busybox_spec(launcher.namespace, "sleep 300")

    launcher.launch(job)

    wait_for_job(launcher, job.run_id, JobStatus.RUNNING)
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

    wait_for_job(launcher, job.run_id, JobStatus.MISSING, timeout=30)


def test_unknown_run_is_missing_and_deleting_it_is_quiet(launcher: KubernetesJobLauncher) -> None:
    run_id = str(uuid.uuid4())

    assert launcher.status(run_id) is JobStatus.MISSING
    launcher.delete(run_id)


def test_launch_applies_the_spec_namespace_not_another(
    launcher: KubernetesJobLauncher,
) -> None:
    with pytest.raises(ValueError):
        launcher.launch(replace(busybox_spec(launcher.namespace, "exit 0"), namespace="default"))


def test_the_termination_message_of_a_finished_run_is_read_back(
    launcher: KubernetesJobLauncher,
) -> None:
    job = busybox_spec(
        launcher.namespace,
        'echo \'{"outcome": "invalid", "reasons": ["wrote outside solutions/"]}\''
        " > /dev/termination-log; exit 2",
    )

    launcher.launch(job)
    wait_for_job(launcher, job.run_id, JobStatus.FAILED)

    message = launcher.termination_message(job.run_id)
    assert message is not None
    assert '"outcome": "invalid"' in message


def test_an_unknown_run_has_no_termination_message(launcher: KubernetesJobLauncher) -> None:
    assert launcher.termination_message("no-such-run") is None


def read_token_secret(launcher: KubernetesJobLauncher, run_id: str) -> V1Secret:
    return CoreV1Api(launcher.api_client).read_namespaced_secret(
        token_secret_name(run_id), launcher.namespace
    )


def test_the_job_sees_its_run_token_and_the_mounted_mcp_registry(
    launcher: KubernetesJobLauncher,
) -> None:
    job = busybox_spec(launcher.namespace, "exit 1")
    job = replace(
        job,
        mcp_registry_configmap=MCP_CONFIGMAP,
        command=(
            "sh",
            "-c",
            f'[ "$GOLEM_RUN_TOKEN" = "{job.run_token}" ]'
            ' && [ "$GOLEM_MCP_REGISTRY" = /etc/golem/mcp/registry.yaml ]'
            " && grep -q wiki.read /etc/golem/mcp/registry.yaml"
            " && ! touch /etc/golem/mcp/x 2>/dev/null",
        ),
    )

    launcher.launch(job)

    wait_for_job(launcher, job.run_id, JobStatus.SUCCEEDED)


def test_the_token_secret_is_owned_by_its_job(
    launcher: KubernetesJobLauncher, k3s_api_client: ApiClient
) -> None:
    job = busybox_spec(launcher.namespace, "exit 0")

    launcher.launch(job)

    created = BatchV1Api(k3s_api_client).read_namespaced_job(job_name(job.run_id), NAMESPACE)
    [owner] = read_token_secret(launcher, job.run_id).metadata.owner_references
    assert (owner.kind, owner.name, owner.uid) == (
        "Job",
        job_name(job.run_id),
        created.metadata.uid,
    )


def test_launch_twice_creates_one_token_secret(
    launcher: KubernetesJobLauncher, k3s_api_client: ApiClient
) -> None:
    job = busybox_spec(launcher.namespace, "exit 0")

    launcher.launch(job)
    launcher.launch(replace(job, run_token=token_for(job.run_id) + "x"))

    secrets = CoreV1Api(k3s_api_client).list_namespaced_secret(
        launcher.namespace, label_selector=f"golem.dev/run-id={job.run_id}"
    )
    assert [s.metadata.name for s in secrets.items] == [token_secret_name(job.run_id)]


def test_a_job_left_without_its_token_secret_starts_once_a_relaunch_creates_it(
    launcher: KubernetesJobLauncher, k3s_api_client: ApiClient
) -> None:
    # A crash between creating the Job and its Secret: the pod waits (optional: false) and the
    # orchestrator's relaunch of the running run heals it.
    job = busybox_spec(launcher.namespace, '[ -n "$GOLEM_RUN_TOKEN" ]')
    BatchV1Api(k3s_api_client).create_namespaced_job(NAMESPACE, build_job_manifest(job))
    time.sleep(5)
    assert launcher.status(job.run_id) is not JobStatus.SUCCEEDED

    launcher.launch(job)

    wait_for_job(launcher, job.run_id, JobStatus.SUCCEEDED)


def test_deleting_the_job_deletes_its_token_secret(launcher: KubernetesJobLauncher) -> None:
    job = busybox_spec(launcher.namespace, "sleep 300")
    launcher.launch(job)

    launcher.delete(job.run_id)

    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        try:
            read_token_secret(launcher, job.run_id)
        except ApiException as error:
            assert error.status == 404
            return
        time.sleep(1)
    pytest.fail("the token secret outlived its Job")
