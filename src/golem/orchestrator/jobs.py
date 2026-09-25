"""Runs as Kubernetes Jobs: the manifest and the launcher.

Everything inside a Job is untrusted (ADR 0004), so the pod-level part of the security boundary
lives in the manifest itself and the network part in the Jobs namespace's NetworkPolicies,
deploy/k8s/base/network-policies-jobs.yaml (ADR 0009).
"""

import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

from kubernetes.client import ApiClient, BatchV1Api, CoreV1Api, V1Job, V1Pod
from kubernetes.client.exceptions import ApiException

APP_LABEL = "app.kubernetes.io/name"
APP_NAME = "golem-run"
RUN_ID_LABEL = "golem.dev/run-id"
AGENT_LABEL = "golem.dev/agent"

JOB_NAME_PREFIX = "golem-run-"
TOKEN_SECRET_SUFFIX = "-token"
RUN_TOKEN_ENV = "GOLEM_RUN_TOKEN"
CALL_TOKEN_ENV = "GOLEM_CALL_TOKEN"
MCP_REGISTRY_DIR = "/etc/golem/mcp"
MCP_REGISTRY_PATH = f"{MCP_REGISTRY_DIR}/registry.yaml"
MCP_REGISTRY_VOLUME = "mcp-registry"
RUNTIME_COMMAND = ("python", "-m", "golem.runtime")
WORKSPACE = "/workspace"
RUN_AS_USER = 10001

DNS_LABEL = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")
DNS_SUBDOMAIN = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?(\.[a-z0-9]([-a-z0-9]*[a-z0-9])?)*$")
AGENT_NAME = re.compile(r"^[a-z]([-a-z0-9]*[a-z0-9])?$")
MAX_LABEL = 63
MAX_RUN_ID = MAX_LABEL - len(JOB_NAME_PREFIX)


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _is_dns_label(value: str, max_length: int = MAX_LABEL) -> bool:
    return len(value) <= max_length and DNS_LABEL.fullmatch(value) is not None


def _is_dns_subdomain(value: str) -> bool:
    return len(value) <= 253 and DNS_SUBDOMAIN.fullmatch(value) is not None


@dataclass(frozen=True)
class CatalogRef:
    url: str
    revision: str

    def __post_init__(self) -> None:
        _require(bool(self.url.strip()), "catalog url must not be empty")
        _require(bool(self.revision.strip()), "catalog revision must not be empty")

    def __str__(self) -> str:
        return f"{self.url}#{self.revision}"


@dataclass(frozen=True)
class JobSpec:
    run_id: str
    agent: str
    image: str
    namespace: str
    catalog_ref: CatalogRef
    goal: str
    secret_name: str
    active_deadline_seconds: int
    ttl_seconds_after_finished: int
    cpu: str
    memory: str
    run_token: str = field(repr=False)
    # For the edge, when a role delegates to another agent (ADR 0014).
    call_token: str = field(repr=False)
    command: tuple[str, ...] | None = None
    traceparent: str = ""
    tracestate: str = ""
    mcp_registry_configmap: str | None = None

    def __post_init__(self) -> None:
        # The run id becomes both the Job name suffix and a label value, so it must fit both.
        _require(
            _is_dns_label(self.run_id, MAX_RUN_ID),
            f"run_id must be a DNS label of at most {MAX_RUN_ID} chars: {self.run_id!r}",
        )
        _require(
            len(self.agent) <= MAX_LABEL and AGENT_NAME.fullmatch(self.agent) is not None,
            f"agent must be a lowercase slug of at most {MAX_LABEL} chars: {self.agent!r}",
        )
        _require(bool(self.image.strip()), "image must not be empty")
        _require(_is_dns_label(self.namespace), f"namespace is not a DNS label: {self.namespace!r}")
        _require(
            _is_dns_subdomain(self.secret_name),
            f"secret_name is not a DNS subdomain: {self.secret_name!r}",
        )
        _require(bool(self.goal.strip()), "goal must not be empty")
        _require(
            self.active_deadline_seconds > 0,
            f"active_deadline_seconds must be positive: {self.active_deadline_seconds}",
        )
        _require(
            self.ttl_seconds_after_finished >= 0,
            f"ttl_seconds_after_finished must not be negative: {self.ttl_seconds_after_finished}",
        )
        _require(bool(self.cpu.strip()), "cpu must not be empty")
        _require(bool(self.memory.strip()), "memory must not be empty")
        _require(self.command is None or len(self.command) > 0, "command must not be empty")
        _require(bool(self.run_token.strip()), "run_token must not be empty")
        _require(bool(self.call_token.strip()), "call_token must not be empty")
        _require(
            self.mcp_registry_configmap is None or _is_dns_subdomain(self.mcp_registry_configmap),
            f"mcp_registry_configmap is not a DNS subdomain: {self.mcp_registry_configmap!r}",
        )


def job_name(run_id: str) -> str:
    return f"{JOB_NAME_PREFIX}{run_id}"


def token_secret_name(run_id: str) -> str:
    return f"{job_name(run_id)}{TOKEN_SECRET_SUFFIX}"


def run_labels(spec: JobSpec) -> dict[str, str]:
    return {APP_LABEL: APP_NAME, RUN_ID_LABEL: spec.run_id, AGENT_LABEL: spec.agent}


def trace_env(spec: JobSpec) -> list[dict[str, str]]:
    # Environment-variable carrier names from the OpenTelemetry spec; the runtime's root span
    # becomes a child of the request's span.
    pairs = (("TRACEPARENT", spec.traceparent), ("TRACESTATE", spec.tracestate))
    return [{"name": name, "value": value} for name, value in pairs if value]


def mcp_registry_env(spec: JobSpec) -> list[dict[str, str]]:
    if spec.mcp_registry_configmap is None:
        return []
    return [{"name": "GOLEM_MCP_REGISTRY", "value": MCP_REGISTRY_PATH}]


def mcp_registry_mounts(spec: JobSpec) -> list[dict]:
    if spec.mcp_registry_configmap is None:
        return []
    return [{"name": MCP_REGISTRY_VOLUME, "mountPath": MCP_REGISTRY_DIR, "readOnly": True}]


def mcp_registry_volumes(spec: JobSpec) -> list[dict]:
    if spec.mcp_registry_configmap is None:
        return []
    return [{"name": MCP_REGISTRY_VOLUME, "configMap": {"name": spec.mcp_registry_configmap}}]


def _container(spec: JobSpec) -> dict:
    resources = {"cpu": spec.cpu, "memory": spec.memory}
    return {
        "name": "runtime",
        "image": spec.image,
        "command": list(spec.command or RUNTIME_COMMAND),
        "workingDir": WORKSPACE,
        "env": [
            {"name": "GOLEM_RUN_ID", "value": spec.run_id},
            {"name": "GOLEM_AGENT", "value": spec.agent},
            {"name": "GOLEM_CATALOG_REF", "value": str(spec.catalog_ref)},
            {"name": "GOLEM_GOAL", "value": spec.goal},
            *trace_env(spec),
            *mcp_registry_env(spec),
        ],
        # optional: False keeps the pod from starting without keys instead of failing mid-run;
        # the token Secret is created right after the Job, and the pod waits for it.
        "envFrom": [
            {"secretRef": {"name": spec.secret_name, "optional": False}},
            {"secretRef": {"name": token_secret_name(spec.run_id), "optional": False}},
        ],
        "resources": {"requests": resources, "limits": dict(resources)},
        "securityContext": {
            "allowPrivilegeEscalation": False,
            "privileged": False,
            "readOnlyRootFilesystem": True,
            "runAsNonRoot": True,
            "capabilities": {"drop": ["ALL"]},
        },
        "volumeMounts": [
            {"name": "workspace", "mountPath": WORKSPACE},
            {"name": "tmp", "mountPath": "/tmp"},
            *mcp_registry_mounts(spec),
        ],
    }


def build_job_manifest(spec: JobSpec) -> dict:
    labels = run_labels(spec)
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {"name": job_name(spec.run_id), "namespace": spec.namespace, "labels": labels},
        "spec": {
            # Retrying a failed run is the orchestrator's decision, not the Job controller's.
            "backoffLimit": 0,
            "activeDeadlineSeconds": spec.active_deadline_seconds,
            "ttlSecondsAfterFinished": spec.ttl_seconds_after_finished,
            "template": {
                "metadata": {"labels": dict(labels)},
                "spec": {
                    "restartPolicy": "Never",
                    "automountServiceAccountToken": False,
                    "securityContext": {
                        "runAsNonRoot": True,
                        "runAsUser": RUN_AS_USER,
                        "runAsGroup": RUN_AS_USER,
                        "fsGroup": RUN_AS_USER,
                        "seccompProfile": {"type": "RuntimeDefault"},
                    },
                    "containers": [_container(spec)],
                    "volumes": [
                        {"name": "workspace", "emptyDir": {}},
                        {"name": "tmp", "emptyDir": {}},
                        *mcp_registry_volumes(spec),
                    ],
                },
            },
        },
    }


def build_token_secret(spec: JobSpec, job_uid: str) -> dict:
    # Owned by the Job, so deleting the Job (or its TTL) garbage-collects the token with it.
    return {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {
            "name": token_secret_name(spec.run_id),
            "namespace": spec.namespace,
            "labels": run_labels(spec),
            "ownerReferences": [
                {
                    "apiVersion": "batch/v1",
                    "kind": "Job",
                    "name": job_name(spec.run_id),
                    "uid": job_uid,
                }
            ],
        },
        "type": "Opaque",
        "stringData": {RUN_TOKEN_ENV: spec.run_token, CALL_TOKEN_ENV: spec.call_token},
    }


class JobStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    MISSING = "missing"


def _has_condition(job: V1Job, kind: str) -> bool:
    conditions = (job.status and job.status.conditions) or []
    return any(c.type == kind and c.status == "True" for c in conditions)


def job_status_of(job: V1Job) -> JobStatus:
    if _has_condition(job, "Complete"):
        return JobStatus.SUCCEEDED
    if _has_condition(job, "Failed"):
        return JobStatus.FAILED
    if job.status and (job.status.active or 0) > 0:
        return JobStatus.RUNNING
    return JobStatus.PENDING


class JobNameTaken(RuntimeError):
    """A Job with the run's name exists but belongs to another run id."""


class JobLauncher(Protocol):
    def launch(self, spec: JobSpec) -> None: ...

    def status(self, run_id: str) -> JobStatus: ...

    def delete(self, run_id: str) -> None: ...

    def termination_message(self, run_id: str) -> str | None: ...


def termination_message_of(pods: list[V1Pod]) -> str | None:
    """The run's report, which the runtime writes to /dev/termination-log before exiting."""
    for pod in pods:
        for container in (pod.status and pod.status.container_statuses) or []:
            terminated = container.state and container.state.terminated
            if terminated is not None and terminated.message:
                return terminated.message
    return None


HTTP_NOT_FOUND = 404
HTTP_CONFLICT = 409


@dataclass(frozen=True)
class KubernetesJobLauncher:
    api_client: ApiClient
    namespace: str

    @property
    def _batch(self) -> BatchV1Api:
        return BatchV1Api(self.api_client)

    def launch(self, spec: JobSpec) -> None:
        _require(
            spec.namespace == self.namespace,
            f"spec namespace {spec.namespace!r} is not the launcher's {self.namespace!r}",
        )
        job = self._create_job(spec)
        try:
            CoreV1Api(self.api_client).create_namespaced_secret(
                self.namespace, build_token_secret(spec, job.metadata.uid)
            )
        except ApiException as error:
            # A relaunch keeps the token the Job already has.
            if error.status != HTTP_CONFLICT:
                raise

    def _create_job(self, spec: JobSpec) -> V1Job:
        try:
            return self._batch.create_namespaced_job(self.namespace, build_job_manifest(spec))
        except ApiException as error:
            if error.status != HTTP_CONFLICT:
                raise
            existing = self._batch.read_namespaced_job(job_name(spec.run_id), self.namespace)
            if (existing.metadata.labels or {}).get(RUN_ID_LABEL) != spec.run_id:
                raise JobNameTaken(job_name(spec.run_id)) from error
            return existing

    def status(self, run_id: str) -> JobStatus:
        try:
            job = self._batch.read_namespaced_job(job_name(run_id), self.namespace)
        except ApiException as error:
            if error.status == HTTP_NOT_FOUND:
                return JobStatus.MISSING
            raise
        return job_status_of(job)

    def termination_message(self, run_id: str) -> str | None:
        pods = CoreV1Api(self.api_client).list_namespaced_pod(
            self.namespace, label_selector=f"{RUN_ID_LABEL}={run_id}"
        )
        return termination_message_of(pods.items)

    def delete(self, run_id: str) -> None:
        try:
            # Job deletion through the API orphans its dependents (pods, the token Secret)
            # unless propagation is set.
            self._batch.delete_namespaced_job(
                job_name(run_id), self.namespace, propagation_policy="Background"
            )
        except ApiException as error:
            if error.status != HTTP_NOT_FOUND:
                raise
