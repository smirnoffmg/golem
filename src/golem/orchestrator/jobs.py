"""Runs as Kubernetes Jobs: the manifest, the egress policy and the launcher.

Everything inside a Job is untrusted (ADR 0004), so the pod-level part of the security boundary
lives in the manifest itself and the network part in the namespace's NetworkPolicy.
"""

import ipaddress
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

from kubernetes.client import ApiClient, BatchV1Api, V1Job
from kubernetes.client.exceptions import ApiException

APP_LABEL = "app.kubernetes.io/name"
APP_NAME = "golem-run"
RUN_ID_LABEL = "golem.dev/run-id"
AGENT_LABEL = "golem.dev/agent"

JOB_NAME_PREFIX = "golem-run-"
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
    command: tuple[str, ...] | None = None

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
            len(self.secret_name) <= 253 and DNS_SUBDOMAIN.fullmatch(self.secret_name) is not None,
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


def job_name(run_id: str) -> str:
    return f"{JOB_NAME_PREFIX}{run_id}"


def run_labels(spec: JobSpec) -> dict[str, str]:
    return {APP_LABEL: APP_NAME, RUN_ID_LABEL: spec.run_id, AGENT_LABEL: spec.agent}


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
        ],
        # optional: False keeps the pod from starting without keys instead of failing mid-run.
        "envFrom": [{"secretRef": {"name": spec.secret_name, "optional": False}}],
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
                    ],
                },
            },
        },
    }


@dataclass(frozen=True)
class InCluster:
    namespace_labels: Mapping[str, str]
    pod_labels: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # An empty namespaceSelector matches every namespace.
        _require(bool(self.namespace_labels), "namespace_labels must not be empty")


@dataclass(frozen=True)
class Cidr:
    cidr: str

    def __post_init__(self) -> None:
        try:
            network = ipaddress.ip_network(self.cidr)
        except ValueError as error:
            raise ValueError(f"not a CIDR: {self.cidr!r}") from error
        _require(network.prefixlen > 0, f"catch-all CIDR would allow all egress: {self.cidr}")


@dataclass(frozen=True)
class Destination:
    peer: InCluster | Cidr
    ports: tuple[int, ...]

    def __post_init__(self) -> None:
        _require(bool(self.ports), "a destination needs at least one port")
        _require(all(0 < port < 65536 for port in self.ports), f"bad port in {self.ports}")


@dataclass(frozen=True)
class EgressAllowList:
    """The five destinations a Job may reach (ADR 0004); anything else is denied."""

    a2a_edge: Destination
    model_gateway: Destination
    trace_store: Destination
    mcp_servers: Destination
    git_host: Destination

    def destinations(self) -> tuple[Destination, ...]:
        return (
            self.a2a_edge,
            self.model_gateway,
            self.trace_store,
            self.mcp_servers,
            self.git_host,
        )


def _peer(peer: InCluster | Cidr) -> dict:
    if isinstance(peer, Cidr):
        return {"ipBlock": {"cidr": peer.cidr}}
    selector = {"namespaceSelector": {"matchLabels": dict(peer.namespace_labels)}}
    if peer.pod_labels:
        selector["podSelector"] = {"matchLabels": dict(peer.pod_labels)}
    return selector


def _egress_rule(destination: Destination) -> dict:
    return {
        "to": [_peer(destination.peer)],
        "ports": [{"protocol": "TCP", "port": port} for port in destination.ports],
    }


def _dns_rule() -> dict:
    return {
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


def build_network_policy(namespace: str, egress: EgressAllowList) -> dict:
    _require(_is_dns_label(namespace), f"namespace is not a DNS label: {namespace!r}")
    return {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "NetworkPolicy",
        "metadata": {"name": f"{APP_NAME}-egress", "namespace": namespace},
        "spec": {
            "podSelector": {"matchLabels": {APP_LABEL: APP_NAME}},
            "policyTypes": ["Ingress", "Egress"],
            "ingress": [],
            "egress": [
                *(_egress_rule(d) for d in egress.destinations()),
                _dns_rule(),
            ],
        },
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
        try:
            self._batch.create_namespaced_job(self.namespace, build_job_manifest(spec))
        except ApiException as error:
            if error.status != HTTP_CONFLICT:
                raise
            existing = self._batch.read_namespaced_job(job_name(spec.run_id), self.namespace)
            if (existing.metadata.labels or {}).get(RUN_ID_LABEL) != spec.run_id:
                raise JobNameTaken(job_name(spec.run_id)) from error

    def status(self, run_id: str) -> JobStatus:
        try:
            job = self._batch.read_namespaced_job(job_name(run_id), self.namespace)
        except ApiException as error:
            if error.status == HTTP_NOT_FOUND:
                return JobStatus.MISSING
            raise
        return job_status_of(job)

    def delete(self, run_id: str) -> None:
        try:
            # Job deletion through the API orphans its pods unless propagation is set.
            self._batch.delete_namespaced_job(
                job_name(run_id), self.namespace, propagation_policy="Background"
            )
        except ApiException as error:
            if error.status != HTTP_NOT_FOUND:
                raise
