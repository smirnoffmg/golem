from dataclasses import replace
from datetime import UTC, datetime

import pytest
from kubernetes.client import V1Job, V1JobCondition, V1JobStatus, V1ObjectMeta

from golem.orchestrator.jobs import (
    CatalogRef,
    Cidr,
    Destination,
    EgressAllowList,
    InCluster,
    JobSpec,
    JobStatus,
    build_job_manifest,
    build_network_policy,
    build_token_secret,
    job_name,
    job_status_of,
    token_secret_name,
)

RUN_ID = "3f2b8c1e-8d4a-4c3e-9a57-0b7f2d6e1a90"
RUN_TOKEN = "eyJhbGciOiJFUzI1NiJ9.eyJzdWIiOiJydW4ifQ.c2lnbmF0dXJl"


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
    assert all(volumes[name] == {"name": name, "emptyDir": {}} for name in mounts.values())
    assert container(manifest)["workingDir"] == "/workspace"


def test_requests_equal_limits() -> None:
    resources = container(build_job_manifest(spec()))["resources"]

    assert resources["requests"] == {"cpu": "500m", "memory": "1Gi"}
    assert resources["limits"] == resources["requests"]


def test_run_parameters_are_passed_as_environment() -> None:
    assert env_of(build_job_manifest(spec())) == {
        "GOLEM_RUN_ID": RUN_ID,
        "GOLEM_AGENT": "reviewer",
        "GOLEM_CATALOG_REF": "https://git.example/team-a/catalog.git#a1b2c3d",
        "GOLEM_GOAL": "Fix the flaky test in payments.",
    }


def test_secrets_come_only_by_reference_to_the_shared_and_the_per_run_secret() -> None:
    manifest = build_job_manifest(spec())

    assert container(manifest)["envFrom"] == [
        {"secretRef": {"name": "golem-run-secrets", "optional": False}},
        {"secretRef": {"name": f"golem-run-{RUN_ID}-token", "optional": False}},
    ]
    assert all("valueFrom" not in item for item in container(manifest)["env"])
    assert "secret" not in str(pod_spec(manifest)["volumes"]).lower()


def test_the_run_token_never_appears_in_the_job_manifest() -> None:
    assert RUN_TOKEN not in str(build_job_manifest(spec(mcp_registry_configmap="golem-mcp")))


def test_the_run_token_is_hidden_from_the_spec_repr() -> None:
    assert RUN_TOKEN not in repr(spec())


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
    assert secret["stringData"] == {"GOLEM_RUN_TOKEN": RUN_TOKEN}
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


# --- Network policy -----------------------------------------------------------------------------


def allow_list() -> EgressAllowList:
    return EgressAllowList(
        a2a_edge=Destination(
            InCluster(
                namespace_labels={"kubernetes.io/metadata.name": "golem"},
                pod_labels={"app.kubernetes.io/name": "golem-edge"},
            ),
            ports=(8443,),
        ),
        model_gateway=Destination(
            InCluster(namespace_labels={"kubernetes.io/metadata.name": "llm-gateway"}),
            ports=(4000,),
        ),
        trace_store=Destination(Cidr("10.20.0.0/24"), ports=(443,)),
        mcp_servers=Destination(
            InCluster(
                namespace_labels={"kubernetes.io/metadata.name": "golem"},
                pod_labels={"golem.dev/component": "mcp"},
            ),
            ports=(8080,),
        ),
        git_host=Destination(Cidr("192.0.2.10/32"), ports=(443, 22)),
    )


def test_policy_selects_run_pods_and_restricts_both_directions() -> None:
    policy = build_network_policy("team-a-jobs", allow_list())

    assert policy["apiVersion"] == "networking.k8s.io/v1"
    assert policy["kind"] == "NetworkPolicy"
    assert policy["metadata"]["namespace"] == "team-a-jobs"
    assert policy["spec"]["podSelector"] == {"matchLabels": {"app.kubernetes.io/name": "golem-run"}}
    assert policy["spec"]["policyTypes"] == ["Ingress", "Egress"]


def test_policy_denies_all_ingress() -> None:
    assert build_network_policy("team-a-jobs", allow_list())["spec"]["ingress"] == []


def test_policy_allows_exactly_the_five_destinations_plus_dns() -> None:
    egress = build_network_policy("team-a-jobs", allow_list())["spec"]["egress"]

    assert len(egress) == 6
    assert egress[:5] == [
        {
            "to": [
                {
                    "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "golem"}},
                    "podSelector": {"matchLabels": {"app.kubernetes.io/name": "golem-edge"}},
                }
            ],
            "ports": [{"protocol": "TCP", "port": 8443}],
        },
        {
            "to": [
                {
                    "namespaceSelector": {
                        "matchLabels": {"kubernetes.io/metadata.name": "llm-gateway"}
                    },
                }
            ],
            "ports": [{"protocol": "TCP", "port": 4000}],
        },
        {
            "to": [{"ipBlock": {"cidr": "10.20.0.0/24"}}],
            "ports": [{"protocol": "TCP", "port": 443}],
        },
        {
            "to": [
                {
                    "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "golem"}},
                    "podSelector": {"matchLabels": {"golem.dev/component": "mcp"}},
                }
            ],
            "ports": [{"protocol": "TCP", "port": 8080}],
        },
        {
            "to": [{"ipBlock": {"cidr": "192.0.2.10/32"}}],
            "ports": [{"protocol": "TCP", "port": 443}, {"protocol": "TCP", "port": 22}],
        },
    ]


def test_policy_allows_dns_to_kube_dns_only() -> None:
    """Every destination above is reached by name (edge, gateway, Git host...), and once a
    NetworkPolicy selects a pod for Egress, DNS lookups to kube-dns are denied like any other
    traffic. Without this rule the Job could resolve nothing and every allowed destination
    would be unreachable. DNS answers over UDP and falls back to TCP for large responses."""
    dns = build_network_policy("team-a-jobs", allow_list())["spec"]["egress"][-1]

    assert dns == {
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


def test_destination_needs_ports_and_selectors() -> None:
    with pytest.raises(ValueError):
        Destination(Cidr("10.0.0.0/8"), ports=())
    with pytest.raises(ValueError):
        Destination(Cidr("10.0.0.0/8"), ports=(0,))
    with pytest.raises(ValueError):
        InCluster(namespace_labels={})
    with pytest.raises(ValueError):
        Cidr("not-a-cidr")


def test_catch_all_cidr_is_rejected() -> None:
    """0.0.0.0/0 would turn the allow-list back into allow-all."""
    with pytest.raises(ValueError):
        Cidr("0.0.0.0/0")
    with pytest.raises(ValueError):
        Cidr("::/0")
