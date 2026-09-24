from dataclasses import dataclass

from kubernetes import config
from kubernetes.client import ApiClient

from golem.orchestrator.jobs import JobLauncher, JobSpec, JobStatus, KubernetesJobLauncher
from golem.settings import Kubernetes

NO_CLUSTER = "no Kubernetes cluster is configured (GOLEM_KUBERNETES=none), so no run can start"


@dataclass(frozen=True)
class NoCluster:
    def launch(self, spec: JobSpec) -> None:
        raise RuntimeError(NO_CLUSTER)

    def status(self, run_id: str) -> JobStatus:
        raise RuntimeError(NO_CLUSTER)

    def delete(self, run_id: str) -> None:
        raise RuntimeError(NO_CLUSTER)


def launcher_for(kubernetes: Kubernetes, namespace: str) -> JobLauncher:
    match kubernetes:
        case Kubernetes.NONE:
            return NoCluster()
        case Kubernetes.IN_CLUSTER:
            config.load_incluster_config()
            return KubernetesJobLauncher(ApiClient(), namespace)
        case Kubernetes.KUBECONFIG:
            return KubernetesJobLauncher(config.new_client_from_config(), namespace)
