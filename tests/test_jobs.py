import time
from dataclasses import replace
from datetime import UTC, datetime

import pytest
from kubernetes.client import (
    ApiClient,
    Configuration,
    V1Job,
    V1JobCondition,
    V1JobStatus,
    V1ObjectMeta,
)

from golem.orchestrator.jobs import (
    CatalogRef,
    JobSpec,
    JobStatus,
    KubernetesJobLauncher,
    build_job_manifest,
    build_token_secret,
    job_name,
    job_status_of,
    token_secret_name,
)
from golem.orchestrator.service import JobTemplate, job_spec_for
from golem.tasks.ports import RunStart

RUN_ID = "3f2b8c1e-8d4a-4c3e-9a57-0b7f2d6e1a90"
RUN_TOKEN = "eyJhbGciOiJFUzI1NiJ9.eyJzdWIiOiJydW4ifQ.c2lnbmF0dXJl"
CALL_TOKEN = "eyJhbGciOiJFUzI1NiJ9.eyJzdWIiOiJ1c2VyOmFsaWNlIn0.Y2FsbA"


def spec(**changes: object) -> JobSpec:
    base = JobSpec(
        run_id=RUN_ID,
        agent="reviewer",
        image="registry.example/golem:1.0",
        namespace="team-a-jobs",
        catalog_ref=CatalogRef(url="https://git.example/team-a/catalog.git", revision="a1b2c3d"),
        goal="Fix the flaky test in payments.",
        secret_name="golem-run-secrets",
        active_deadline_seconds=1800,
        ttl_seconds_after_finished=600,
        cpu="500m",
        memory="1Gi",
        run_token=RUN_TOKEN,
        call_token=CALL_TOKEN,
    )
    return replace(base, **changes)


def pod_spec(manifest: dict) -> dict:
    return manifest["spec"]["template"]["spec"]


def container(manifest: dict) -> dict:
    [only] = pod_spec(manifest)["containers"]
    return only


def env_of(manifest: dict) -> dict[str, str]:
    return {item["name"]: item["value"] for item in container(manifest)["env"]}


# --- Job manifest -------------------------------------------------------------------------------


def test_manifest_is_a_batch_v1_job_in_the_team_namespace() -> None:
    manifest = build_job_manifest(spec())

    assert manifest["apiVersion"] == "batch/v1"
    assert manifest["kind"] == "Job"
    assert manifest["metadata"]["namespace"] == "team-a-jobs"


def test_name_is_derived_from_run_id_and_is_a_dns_label() -> None:
    name = build_job_manifest(spec())["metadata"]["name"]

    assert name == job_name(RUN_ID) == f"golem-run-{RUN_ID}"
    assert len(name) <= 63


def test_job_and_pod_carry_the_same_labels() -> None:
    manifest = build_job_manifest(spec())
    expected = {
        "app.kubernetes.io/name": "golem-run",
        "golem.dev/run-id": RUN_ID,
        "golem.dev/agent": "reviewer",
    }

    assert manifest["metadata"]["labels"] == expected
    assert manifest["spec"]["template"]["metadata"]["labels"] == expected


def test_a_failed_run_is_not_retried_by_kubernetes_and_is_bounded_in_time() -> None:
    job = build_job_manifest(spec())["spec"]

    assert job["backoffLimit"] == 0
    assert job["activeDeadlineSeconds"] == 1800
    assert job["ttlSecondsAfterFinished"] == 600
    assert job["template"]["spec"]["restartPolicy"] == "Never"


def test_pod_gets_no_service_account_token() -> None:
    assert pod_spec(build_job_manifest(spec()))["automountServiceAccountToken"] is False


def test_pod_runs_as_a_numeric_non_root_user_with_default_seccomp() -> None:
    security = pod_spec(build_job_manifest(spec()))["securityContext"]

    assert security["runAsNonRoot"] is True
    assert isinstance(security["runAsUser"], int)
    assert security["runAsUser"] > 0
    assert security["seccompProfile"] == {"type": "RuntimeDefault"}


def test_container_cannot_escalate_and_has_no_capabilities() -> None:
    security = container(build_job_manifest(spec()))["securityContext"]

    assert security["allowPrivilegeEscalation"] is False
    assert security["privileged"] is False
    assert security["capabilities"] == {"drop": ["ALL"]}
    assert security["readOnlyRootFilesystem"] is True


def test_only_the_workspace_and_tmp_are_writable_and_both_are_empty_dirs() -> None:
    manifest = build_job_manifest(spec())
    volumes = {v["name"]: v for v in pod_spec(manifest)["volumes"]}
    mounts = {m["mountPath"]: m["name"] for m in container(manifest)["volumeMounts"]}

    assert set(mounts) == {"/workspace", "/tmp"}
    assert all(set(volumes[name]) == {"name", "emptyDir"} for name in mounts.values())
    assert container(manifest)["workingDir"] == "/workspace"


def test_the_run_cannot_fill_the_nodes_disk() -> None:
    # An untrusted run writing without bound would push the node into DiskPressure and get
    # other pods evicted; with a limit, the kubelet evicts only this one.
    manifest = build_job_manifest(spec(ephemeral_storage="3Gi"))
    resources = container(manifest)["resources"]
    volumes = {v["name"]: v for v in pod_spec(manifest)["volumes"]}

    assert resources["requests"]["ephemeral-storage"] == "3Gi"
    assert resources["limits"]["ephemeral-storage"] == "3Gi"
    assert volumes["workspace"]["emptyDir"] == {"sizeLimit": "3Gi"}
    assert volumes["tmp"]["emptyDir"] == {"sizeLimit": "3Gi"}


def test_the_pod_gets_no_environment_about_the_services_in_its_namespace() -> None:
    assert pod_spec(build_job_manifest(spec()))["enableServiceLinks"] is False


def test_requests_equal_limits() -> None:
    resources = container(build_job_manifest(spec()))["resources"]

    assert resources["requests"] == {"cpu": "500m", "memory": "1Gi", "ephemeral-storage": "2Gi"}
    assert resources["limits"] == resources["requests"]


def test_run_parameters_are_passed_as_environment() -> None:
    assert env_of(build_job_manifest(spec())) == {
        "GOLEM_RUN_ID": RUN_ID,
        "GOLEM_AGENT": "reviewer",
        "GOLEM_CATALOG_REF": "https://git.example/team-a/catalog.git#a1b2c3d",
        "GOLEM_GOAL": "Fix the flaky test in payments.",
    }


def test_a_goal_runs_target_is_passed_as_environment() -> None:
    env = env_of(build_job_manifest(spec(target="alert-0a1b2c3d4e5f")))

    assert env["GOLEM_TARGET"] == "alert-0a1b2c3d4e5f"


def test_the_target_the_starter_named_reaches_the_job_spec() -> None:
    run = RunStart(
        task_id="t-1",
        context_id="c-1",
        agent="reviewer",
        goal="disk usage alert",
        caller="service:alertmanager",
        message_id="m-1",
        target="alert-0a1b2c3d4e5f",
    )
    template = JobTemplate(
        image="registry.example/golem:1.0",
        namespace="team-a-jobs",
        secret_name="golem-run-secrets",
        active_deadline_seconds=1800,
        ttl_seconds_after_finished=600,
        cpu="500m",
        memory="1Gi",
    )
    catalog = CatalogRef(url="https://git.example/catalog.git", revision="a1b2c3d")

    assert job_spec_for(RUN_ID, run, catalog, template, "a.b.c", "d.e.f").target == (
        "alert-0a1b2c3d4e5f"
    )


def test_secrets_come_only_by_reference_to_the_shared_and_the_per_run_secret() -> None:
    manifest = build_job_manifest(spec())

    assert container(manifest)["envFrom"] == [
        {"secretRef": {"name": "golem-run-secrets", "optional": False}},
        {"secretRef": {"name": f"golem-run-{RUN_ID}-token", "optional": False}},
    ]
    assert all("valueFrom" not in item for item in container(manifest)["env"])
    assert "secret" not in str(pod_spec(manifest)["volumes"]).lower()


def test_the_run_and_call_tokens_never_appear_in_the_job_manifest() -> None:
    manifest = str(build_job_manifest(spec(mcp_registry_configmap="golem-mcp")))

    assert RUN_TOKEN not in manifest
    assert CALL_TOKEN not in manifest


def test_the_run_and_call_tokens_are_hidden_from_the_spec_repr() -> None:
    assert RUN_TOKEN not in repr(spec())
    assert CALL_TOKEN not in repr(spec())


def test_without_a_registry_config_map_nothing_is_mounted_for_mcp() -> None:
    manifest = build_job_manifest(spec())

    assert "GOLEM_MCP_REGISTRY" not in env_of(manifest)
    assert "configMap" not in str(pod_spec(manifest)["volumes"])


def test_the_mcp_registry_is_mounted_read_only_and_named_in_the_environment() -> None:
    manifest = build_job_manifest(spec(mcp_registry_configmap="golem-mcp"))
    volumes = {v["name"]: v for v in pod_spec(manifest)["volumes"]}
    mounts = {m["mountPath"]: m for m in container(manifest)["volumeMounts"]}

    assert mounts["/etc/golem/mcp"]["readOnly"] is True
    assert volumes[mounts["/etc/golem/mcp"]["name"]]["configMap"] == {"name": "golem-mcp"}
    assert env_of(manifest)["GOLEM_MCP_REGISTRY"] == "/etc/golem/mcp/registry.yaml"


def test_the_token_secret_holds_the_token_and_is_owned_by_the_job() -> None:
    secret = build_token_secret(spec(), job_uid="0b7f2d6e-1a90-4c3e-9a57-3f2b8c1e8d4a")

    assert secret["metadata"]["name"] == token_secret_name(RUN_ID) == f"golem-run-{RUN_ID}-token"
    assert secret["metadata"]["namespace"] == "team-a-jobs"
    assert secret["metadata"]["labels"]["golem.dev/run-id"] == RUN_ID
    assert secret["stringData"] == {"GOLEM_RUN_TOKEN": RUN_TOKEN, "GOLEM_CALL_TOKEN": CALL_TOKEN}
    assert secret["metadata"]["ownerReferences"] == [
        {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "name": job_name(RUN_ID),
            "uid": "0b7f2d6e-1a90-4c3e-9a57-3f2b8c1e8d4a",
        }
    ]


def test_default_command_starts_the_runtime() -> None:
    manifest = build_job_manifest(spec())

    assert container(manifest)["command"] == ["python", "-m", "golem.runtime"]
    assert container(manifest)["image"] == "registry.example/golem:1.0"


def test_command_can_be_overridden() -> None:
    manifest = build_job_manifest(spec(command=("sh", "-c", "exit 0")))

    assert container(manifest)["command"] == ["sh", "-c", "exit 0"]


@pytest.mark.parametrize(
    "changes",
    [
        {"image": ""},
        {"image": "  "},
        {"active_deadline_seconds": 0},
        {"active_deadline_seconds": -5},
        {"ttl_seconds_after_finished": -1},
        {"agent": ""},
        {"agent": "Reviewer"},
        {"agent": "code_reviewer"},
        {"agent": "reviewer-"},
        {"agent": "1reviewer"},
        {"agent": "a" * 64},
        {"run_id": ""},
        {"run_id": "Run-1"},
        {"run_id": "run_1"},
        {"run_id": "-run"},
        {"run_id": "r" * 54},
        {"namespace": ""},
        {"namespace": "Team"},
        {"secret_name": ""},
        {"goal": ""},
        {"cpu": ""},
        {"memory": ""},
        {"command": ()},
        {"run_token": ""},
        {"call_token": ""},
        {"mcp_registry_configmap": ""},
        {"mcp_registry_configmap": "Golem_MCP"},
    ],
)
def test_invalid_spec_is_rejected(changes: dict) -> None:
    with pytest.raises(ValueError):
        spec(**changes)


def test_invalid_catalog_ref_is_rejected() -> None:
    with pytest.raises(ValueError):
        CatalogRef(url="", revision="a1b2c3d")
    with pytest.raises(ValueError):
        CatalogRef(url="https://git.example/c.git", revision="")


def test_longest_valid_run_id_still_gives_a_dns_label() -> None:
    run_id = "r" * 53

    assert len(build_job_manifest(spec(run_id=run_id))["metadata"]["name"]) == 63


def test_ttl_zero_is_allowed() -> None:
    assert (
        build_job_manifest(spec(ttl_seconds_after_finished=0))["spec"]["ttlSecondsAfterFinished"]
        == 0
    )


# --- Job status ---------------------------------------------------------------------------------


def job_with(status: V1JobStatus | None) -> V1Job:
    return V1Job(metadata=V1ObjectMeta(name=job_name(RUN_ID)), status=status)


def condition(kind: str, value: str = "True") -> V1JobCondition:
    return V1JobCondition(type=kind, status=value, last_transition_time=datetime.now(UTC))


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (None, JobStatus.PENDING),
        (V1JobStatus(), JobStatus.PENDING),
        (V1JobStatus(active=0), JobStatus.PENDING),
        (V1JobStatus(active=1), JobStatus.RUNNING),
        (V1JobStatus(succeeded=1, conditions=[condition("Complete")]), JobStatus.SUCCEEDED),
        (V1JobStatus(failed=1, conditions=[condition("Failed")]), JobStatus.FAILED),
        (V1JobStatus(active=1, conditions=[condition("Failed", "False")]), JobStatus.RUNNING),
        # Newer Kubernetes sets FailureTarget first and Failed only once the pods are gone.
        (V1JobStatus(active=1, conditions=[condition("FailureTarget")]), JobStatus.RUNNING),
    ],
)
def test_job_status_is_read_from_conditions_and_active_pods(
    status: V1JobStatus | None, expected: JobStatus
) -> None:
    assert job_status_of(job_with(status)) is expected


# --- Launcher against an API server that stops answering ---------------------------------------


@pytest.mark.parametrize(
    "call",
    [
        lambda launcher: launcher.status(RUN_ID),
        lambda launcher: launcher.termination_message(RUN_ID),
        lambda launcher: launcher.delete(RUN_ID),
        lambda launcher: launcher.launch(spec(namespace="team-a-jobs")),
    ],
    ids=["status", "termination_message", "delete", "launch"],
)
def test_every_api_call_gives_up_on_a_silent_api_server(silent_server, call) -> None:
    # Without a timeout the client waits forever, and so does the reconciler's pass.
    configuration = Configuration(host=f"http://{silent_server}")
    configuration.retries = 0
    launcher = KubernetesJobLauncher(
        ApiClient(configuration), "team-a-jobs", request_timeout=(0.5, 0.5)
    )

    started = time.monotonic()
    with pytest.raises(Exception, match=r"(?i)timed? ?out"):
        call(launcher)
    assert time.monotonic() - started < 5
