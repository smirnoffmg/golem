"""Fixtures of the e2e suite: the Golem image in k3s, the git server and the model server."""

import time
from collections.abc import Iterator

import pytest
from e2e_cluster import (
    GIT_SERVICE,
    IMAGE,
    MODEL_SERVER,
    MODEL_SERVICE,
    MODES,
    NAMESPACE,
    build_image,
    e2e_catalog,
    example_context,
    files_config_map,
    git_server,
    import_image,
    model_server,
    run_secret,
    service,
    wait_ready,
)
from kubernetes.client import ApiClient, AppsV1Api, CoreV1Api
from testcontainers.community.k3s import K3SContainer


@pytest.fixture(scope="session")
def golem_image(k3s: K3SContainer) -> str:
    started = time.monotonic()
    build_image()
    built = time.monotonic()
    import_image(k3s)
    print(f"image built in {built - started:.0f}s, imported in {time.monotonic() - built:.0f}s")
    return IMAGE


@pytest.fixture(scope="session")
def cluster(k3s_api_client: ApiClient, golem_image: str) -> Iterator[ApiClient]:
    core, apps = CoreV1Api(k3s_api_client), AppsV1Api(k3s_api_client)
    core.create_namespace({"metadata": {"name": NAMESPACE}})
    catalog_map, catalog_items = files_config_map("e2e-catalog", e2e_catalog())
    context_map, context_items = files_config_map("e2e-context", example_context())
    code_map = {
        "metadata": {"name": "e2e-model-server"},
        "data": {"model_server.py": MODEL_SERVER.read_text()},
    }
    for config_map in (catalog_map, context_map, code_map):
        core.create_namespaced_config_map(NAMESPACE, config_map)
    for mode in MODES:
        core.create_namespaced_secret(NAMESPACE, run_secret(mode))
    apps.create_namespaced_deployment(NAMESPACE, git_server(catalog_items, context_items))
    apps.create_namespaced_deployment(NAMESPACE, model_server())
    core.create_namespaced_service(NAMESPACE, service(GIT_SERVICE, 9418))
    core.create_namespaced_service(NAMESPACE, service(MODEL_SERVICE, 8080))
    # A fresh k3s answers the API before its DNS does; runs resolve the servers by name.
    wait_ready(core, apps, "coredns", namespace="kube-system")
    wait_ready(core, apps, GIT_SERVICE)
    wait_ready(core, apps, MODEL_SERVICE)
    yield k3s_api_client
    core.delete_namespace(NAMESPACE)
