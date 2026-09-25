"""The e2e cluster: the Golem image imported into k3s, a git server and a model server.

Both servers run the Golem image itself (git and Python are in it), so the suite pulls nothing
but k3s. The model server's code comes from a ConfigMap and never ships in `src/`.
"""

import subprocess
import time
from pathlib import Path

import pytest
import yaml
from kubernetes.client import ApiClient, AppsV1Api, CoreV1Api
from kubernetes.stream import stream
from testcontainers.community.k3s import K3SContainer

ROOT = Path(__file__).parent.parent.parent
EXAMPLES = ROOT / "examples"
MODEL_SERVER = Path(__file__).parent / "model_server.py"
IMAGE = "golem:e2e"
NAMESPACE = "golem-e2e"
GIT_SERVICE = "golem-git"
MODEL_SERVICE = "golem-model"
GIT_ROOT = "/srv/git"
CATALOG_URL = f"git://{GIT_SERVICE}/catalog.git"
CONTEXT_URL = f"git://{GIT_SERVICE}/context.git"
MODEL_URL = f"http://{MODEL_SERVICE}:8080/v1"
MODES = ("normal", "garbage", "silent", "oversized")
MODEL_TIMEOUT_SECONDS = 5
UID = 10001

SEED = f"""set -eu
for repo in catalog context; do
  git init --quiet --bare --initial-branch=main {GIT_ROOT}/$repo.git
  work=/tmp/seed-$repo
  mkdir -p $work
  # A ConfigMap volume also holds dot-named bookkeeping entries; the glob skips them.
  for entry in /seed/$repo/*; do cp -RL "$entry" $work/; done
  git -C $work init --quiet --initial-branch=main
  git -C $work add --all
  git -C $work -c user.name=Seed -c user.email=seed@localhost commit --quiet -m seed
  git -C $work push --quiet {GIT_ROOT}/$repo.git main
done
"""


def secret_name(mode: str) -> str:
    return f"golem-run-secrets-{mode}"


def build_image() -> None:
    # The Dockerfile's cache and bind mounts need BuildKit, which the docker CLI uses and the
    # Python SDK's build does not.
    subprocess.run(["docker", "build", "--quiet", "--tag", IMAGE, str(ROOT)], check=True)


def import_image(k3s: K3SContainer) -> None:
    # k3s embeds containerd, and the k3s image links `ctr` to its binary, which talks to that
    # containerd in the kubelet's `k8s.io` namespace (https://docs.k3s.io/installation/airgap:
    # "Manually import the archive with `ctr image import`"). No registry, so Jobs must not
    # pull: a tag other than `latest` defaults to imagePullPolicy IfNotPresent
    # (https://kubernetes.io/docs/concepts/containers/images/).
    container_id = k3s.get_wrapped_container().id
    save = subprocess.Popen(["docker", "save", IMAGE], stdout=subprocess.PIPE)
    try:
        imported = subprocess.run(
            ["docker", "exec", "-i", container_id, "ctr", "images", "import", "-"],
            stdin=save.stdout,
            capture_output=True,
            text=True,
            check=False,
        )
        assert imported.returncode == 0, f"ctr images import failed: {imported.stderr}"
    finally:
        assert save.stdout is not None
        save.stdout.close()
        assert save.wait() == 0, "docker save failed"


def e2e_catalog() -> dict[str, str]:
    """The discovery example, pointed at the in-cluster context and without MCP tools."""
    catalog = yaml.safe_load((EXAMPLES / "discovery" / "agent.yaml").read_text())
    catalog["context"]["url"] = CONTEXT_URL
    for role in catalog["roles"]:
        role.pop("tools", None)
    files = {"agent.yaml": yaml.safe_dump(catalog, sort_keys=False)}
    for path in sorted((EXAMPLES / "discovery" / "roles").glob("*.md")):
        files[f"roles/{path.name}"] = path.read_text()
    return files


def example_context() -> dict[str, str]:
    root = EXAMPLES / "context"
    return {
        path.relative_to(root).as_posix(): path.read_text()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def files_config_map(name: str, files: dict[str, str]) -> tuple[dict, list[dict]]:
    # ConfigMap keys cannot hold a slash; volume items map each key back to its path.
    keys = {path: path.replace("/", "__") for path in files}
    config_map = {
        "metadata": {"name": name},
        "data": {keys[path]: text for path, text in files.items()},
    }
    return config_map, [{"key": keys[path], "path": path} for path in files]


def restricted() -> dict:
    return {
        "allowPrivilegeEscalation": False,
        "readOnlyRootFilesystem": True,
        "runAsNonRoot": True,
        "capabilities": {"drop": ["ALL"]},
    }


def pod_security() -> dict:
    return {"runAsUser": UID, "runAsGroup": UID, "fsGroup": UID, "runAsNonRoot": True}


def deployment(name: str, pod_spec: dict) -> dict:
    labels = {"app": name}
    return {
        "metadata": {"name": name, "labels": labels},
        "spec": {
            "replicas": 1,
            "selector": {"matchLabels": labels},
            "template": {"metadata": {"labels": labels}, "spec": pod_spec},
        },
    }


def service(name: str, port: int) -> dict:
    return {
        "metadata": {"name": name},
        "spec": {"selector": {"app": name}, "ports": [{"port": port, "targetPort": port}]},
    }


def git_server(catalog_items: list[dict], context_items: list[dict]) -> dict:
    mounts = [{"name": "repos", "mountPath": GIT_ROOT}, {"name": "tmp", "mountPath": "/tmp"}]
    return deployment(
        GIT_SERVICE,
        {
            "securityContext": pod_security(),
            "initContainers": [
                {
                    "name": "seed",
                    "image": IMAGE,
                    "command": ["sh", "-c", SEED],
                    "securityContext": restricted(),
                    "volumeMounts": [
                        *mounts,
                        {"name": "catalog", "mountPath": "/seed/catalog"},
                        {"name": "context", "mountPath": "/seed/context"},
                    ],
                }
            ],
            "containers": [
                {
                    "name": "git-daemon",
                    "image": IMAGE,
                    "command": [
                        "git",
                        "daemon",
                        "--reuseaddr",
                        "--export-all",
                        "--enable=receive-pack",
                        f"--base-path={GIT_ROOT}",
                        "--port=9418",
                        GIT_ROOT,
                    ],
                    "ports": [{"containerPort": 9418}],
                    "readinessProbe": {"tcpSocket": {"port": 9418}, "periodSeconds": 1},
                    "securityContext": restricted(),
                    "volumeMounts": mounts,
                }
            ],
            "volumes": [
                {"name": "repos", "emptyDir": {}},
                {"name": "tmp", "emptyDir": {}},
                {"name": "catalog", "configMap": {"name": "e2e-catalog", "items": catalog_items}},
                {"name": "context", "configMap": {"name": "e2e-context", "items": context_items}},
            ],
        },
    )


def model_server() -> dict:
    return deployment(
        MODEL_SERVICE,
        {
            "securityContext": pod_security(),
            "containers": [
                {
                    "name": "model",
                    "image": IMAGE,
                    "command": [
                        "python",
                        "-m",
                        "uvicorn",
                        "model_server:app",
                        "--app-dir=/srv/model",
                        "--host=0.0.0.0",
                        "--port=8080",
                    ],
                    "env": [{"name": "PYTHONDONTWRITEBYTECODE", "value": "1"}],
                    "ports": [{"containerPort": 8080}],
                    "readinessProbe": {
                        "httpGet": {"path": "/healthz", "port": 8080},
                        "periodSeconds": 1,
                    },
                    "securityContext": restricted(),
                    "volumeMounts": [{"name": "code", "mountPath": "/srv/model"}],
                }
            ],
            "volumes": [{"name": "code", "configMap": {"name": "e2e-model-server"}}],
        },
    )


def run_secret(mode: str) -> dict:
    # What the platform's `golem-run-secrets` holds in production: the gateway and its key.
    return {
        "metadata": {"name": secret_name(mode)},
        "stringData": {
            "GOLEM_MODEL_GATEWAY_URL": MODEL_URL,
            "GOLEM_MODEL": mode,
            "GOLEM_MODEL_KEY": "test-only",
            "GOLEM_MODEL_TIMEOUT_SECONDS": str(MODEL_TIMEOUT_SECONDS),
        },
    }


def pod_state(core: CoreV1Api, namespace: str) -> str:
    lines = []
    for pod in core.list_namespaced_pod(namespace).items:
        status = pod.status
        statuses = [*(status.init_container_statuses or []), *(status.container_statuses or [])]
        states = [f"{c.name}: {c.state.waiting or c.state.terminated or 'up'}" for c in statuses]
        lines.append(f"pod {pod.metadata.name} {pod.status.phase} {states}")
    for event in core.list_namespaced_event(namespace).items[-20:]:
        lines.append(f"event {event.involved_object.name}: {event.reason} {event.message}")
    return "\n".join(lines)


def wait_ready(
    core: CoreV1Api, apps: AppsV1Api, name: str, namespace: str = NAMESPACE, timeout: float = 180
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = apps.read_namespaced_deployment(name, namespace).status
        if (status.ready_replicas or 0) >= 1:
            return
        time.sleep(1)
    pytest.fail(f"{name} not ready after {timeout}s\n{pod_state(core, namespace)}")


def git_pod(core: CoreV1Api) -> str:
    [pod] = core.list_namespaced_pod(NAMESPACE, label_selector=f"app={GIT_SERVICE}").items
    return pod.metadata.name


def git_in_server(api_client: ApiClient, *args: str) -> str:
    """Runs git in the git server's pod, where the bare repositories live."""
    core = CoreV1Api(api_client)
    return stream(
        core.connect_get_namespaced_pod_exec,
        git_pod(core),
        NAMESPACE,
        command=["git", *args],
        stderr=True,
        stdin=False,
        stdout=True,
        tty=False,
    )


def drop_proposals(api_client: ApiClient) -> None:
    """Deletes every proposal branch, so the lead picks the same target for the next run."""
    context = f"{GIT_ROOT}/context.git"
    script = (
        f"git -C {context} for-each-ref --format='delete %(refname)' refs/heads/golem/"
        f" | git -C {context} update-ref --stdin"
    )
    core = CoreV1Api(api_client)
    stream(
        core.connect_get_namespaced_pod_exec,
        git_pod(core),
        NAMESPACE,
        command=["sh", "-c", script],
        stderr=True,
        stdin=False,
        stdout=True,
        tty=False,
    )
