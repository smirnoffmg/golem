"""One run end to end: the real image as a Job in k3s, cloning and pushing over git://, with a
scripted model server that also misbehaves the way a real gateway can (Release It!, p. 137).

Run explicitly: `uv run pytest -m e2e`. The first run builds the image and starts k3s.
"""

import json
import re
import time
import uuid
from collections.abc import Iterator

import pytest
from e2e_cluster import (
    CATALOG_URL,
    IMAGE,
    MODEL_TIMEOUT_SECONDS,
    NAMESPACE,
    drop_proposals,
    git_in_server,
    pod_state,
    secret_name,
)
from kubernetes.client import ApiClient, CoreV1Api

from golem import call_token
from golem.call_token import CallClaims
from golem.orchestrator.jobs import (
    RUN_AS_USER,
    CatalogRef,
    JobSpec,
    JobStatus,
    KubernetesJobLauncher,
)
from golem.run_token import RunClaims, SigningKey, issue

pytestmark = pytest.mark.e2e

SIGNING_KEY = SigningKey.generate(kid="e2e")
TARGET = "H-2"
TARGET_FILE = "hypotheses/H-2.md"
EVIDENCE = "- Supports: three of five interviewed teams re-ran a decided discussion (interviews)."
# High on purpose: a run that fails must fail by its own limits, not by the Job's deadline.
DEADLINE_SECONDS = 900
FINISH_TIMEOUT = 300


def run_spec(mode: str) -> JobSpec:
    run_id = f"e2e-{mode}-{uuid.uuid4().hex[:8]}"
    claims = RunClaims(run_id, "discovery", "user:e2e", run_id, (), 2_000_000_000)
    return JobSpec(
        run_id=run_id,
        agent="discovery",
        image=IMAGE,
        namespace=NAMESPACE,
        catalog_ref=CatalogRef(url=CATALOG_URL, revision="main"),
        goal="Work the discovery backlog",
        secret_name=secret_name(mode),
        active_deadline_seconds=DEADLINE_SECONDS,
        ttl_seconds_after_finished=3600,
        cpu="1",
        memory="1Gi",
        run_token=issue(claims, SIGNING_KEY, now=int(time.time())),
        call_token=call_token.issue(
            CallClaims("user:e2e", "discovery", ("discovery",), run_id, run_id, 2_000_000_000),
            SIGNING_KEY,
            now=int(time.time()),
        ),
    )


def finish(launcher: KubernetesJobLauncher, run_id: str) -> tuple[JobStatus, dict, float]:
    started = time.monotonic()
    deadline = started + FINISH_TIMEOUT
    status = launcher.status(run_id)
    while status not in (JobStatus.SUCCEEDED, JobStatus.FAILED) and time.monotonic() < deadline:
        time.sleep(1)
        status = launcher.status(run_id)
    elapsed = time.monotonic() - started
    core = CoreV1Api(launcher.api_client)
    assert status in (JobStatus.SUCCEEDED, JobStatus.FAILED), (
        f"{status} after {elapsed:.0f}s\n{pod_state(core, NAMESPACE)}"
    )
    message = launcher.termination_message(run_id)
    assert message is not None, f"no report from {run_id}\n{run_logs(core, run_id)}"
    return status, json.loads(message), elapsed


def run_logs(core: CoreV1Api, run_id: str) -> str:
    pods = core.list_namespaced_pod(NAMESPACE, label_selector=f"golem.dev/run-id={run_id}")
    return "\n".join(
        core.read_namespaced_pod_log(pod.metadata.name, NAMESPACE, tail_lines=50)
        for pod in pods.items
    )


def proposal_branches(api_client: ApiClient) -> list[str]:
    output = git_in_server(api_client, "ls-remote", "git://localhost/context.git", "golem/*")
    return [line.split("\t")[1].removeprefix("refs/heads/") for line in output.splitlines()]


def sections(text: str) -> dict[str, str]:
    """Front matter and title under "", then each `## ` section by its heading."""
    parts = re.split(r"^## (.+)$", text, flags=re.MULTILINE)
    return {"": parts[0], **{parts[i]: parts[i + 1] for i in range(1, len(parts), 2)}}


@pytest.fixture
def launcher(cluster: ApiClient) -> Iterator[KubernetesJobLauncher]:
    # Every test starts from the example context: an open proposal would move the lead on.
    drop_proposals(cluster)
    yield KubernetesJobLauncher(cluster, NAMESPACE)


def test_a_run_proposes_the_evidence_of_its_target(
    launcher: KubernetesJobLauncher, cluster: ApiClient
) -> None:
    spec = run_spec("normal")

    launcher.launch(spec)
    status, report, elapsed = finish(launcher, spec.run_id)

    assert status is JobStatus.SUCCEEDED, json.dumps(report)
    assert report["outcome"] == "proposed", json.dumps(report)
    assert (report["role"], report["target_id"]) == ("researcher", TARGET)
    branch = f"golem/{TARGET}/{spec.run_id}"
    assert report["branch"] == branch
    assert branch in proposal_branches(cluster)
    context = "/srv/git/context.git"
    changed = git_in_server(cluster, "-C", context, "diff", "--name-only", "main", branch)
    assert changed.split() == [TARGET_FILE]
    before = sections(git_in_server(cluster, "-C", context, "show", f"main:{TARGET_FILE}"))
    after = sections(git_in_server(cluster, "-C", context, "show", f"{branch}:{TARGET_FILE}"))
    assert {k: v for k, v in after.items() if k != "Evidence"} == {
        k: v for k, v in before.items() if k != "Evidence"
    }
    assert after["Evidence"].strip() == EVIDENCE
    print(f"normal run finished in {elapsed:.0f}s")


def test_the_run_ran_under_the_restricted_security_context(
    launcher: KubernetesJobLauncher, cluster: ApiClient
) -> None:
    spec = run_spec("normal")
    launcher.launch(spec)
    finish(launcher, spec.run_id)

    [pod] = (
        CoreV1Api(cluster)
        .list_namespaced_pod(NAMESPACE, label_selector=f"golem.dev/run-id={spec.run_id}")
        .items
    )
    [container] = pod.spec.containers
    assert (container.image, container.image_pull_policy) == (IMAGE, "IfNotPresent")
    assert pod.spec.security_context.run_as_non_root is True
    assert pod.spec.security_context.run_as_user == RUN_AS_USER
    assert container.security_context.read_only_root_filesystem is True
    assert container.security_context.run_as_non_root is True
    assert container.security_context.allow_privilege_escalation is False
    assert container.security_context.capabilities.drop == ["ALL"]
    assert pod.spec.automount_service_account_token is False


@pytest.mark.parametrize("mode", ["garbage", "oversized"])
def test_an_unusable_model_response_fails_the_run_and_pushes_nothing(
    launcher: KubernetesJobLauncher, cluster: ApiClient, mode: str
) -> None:
    spec = run_spec(mode)

    launcher.launch(spec)
    status, report, _ = finish(launcher, spec.run_id)

    assert status is JobStatus.FAILED, json.dumps(report)
    assert report["outcome"] == "failed", json.dumps(report)
    assert any("model response" in reason for reason in report["reasons"]), json.dumps(report)
    assert not [b for b in proposal_branches(cluster) if b.endswith(f"/{spec.run_id}")]


def test_a_silent_model_fails_the_run_by_its_own_timeout(
    launcher: KubernetesJobLauncher, cluster: ApiClient
) -> None:
    spec = run_spec("silent")

    launcher.launch(spec)
    status, report, elapsed = finish(launcher, spec.run_id)

    assert status is JobStatus.FAILED, json.dumps(report)
    assert report["outcome"] == "failed", json.dumps(report)
    assert any("timed out" in reason.lower() for reason in report["reasons"]), json.dumps(report)
    # Three attempts (the client's two retries) of MODEL_TIMEOUT_SECONDS, backoff and startup.
    assert elapsed < 30 * MODEL_TIMEOUT_SECONDS < DEADLINE_SECONDS
    assert not [b for b in proposal_branches(cluster) if b.endswith(f"/{spec.run_id}")]
