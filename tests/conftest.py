import socket
import threading
from collections.abc import AsyncIterator, Iterator
from typing import TYPE_CHECKING

import psycopg
import pytest
from kubernetes.client import ApiClient
from support.demo import postgres_container
from testcontainers.community.postgres import PostgresContainer

if TYPE_CHECKING:
    from testcontainers.community.k3s import K3SContainer


@pytest.fixture(scope="session")
def postgres() -> Iterator[PostgresContainer]:
    with postgres_container() as container:
        yield container


@pytest.fixture(scope="session")
def runs_dsn(postgres: PostgresContainer) -> str:
    host = postgres.get_container_host_ip()
    port = postgres.get_exposed_port(5432)
    return f"host={host} port={port} dbname=golem_runs user=golem_runs password=dev-only-golem-runs"


@pytest.fixture
async def runs_db(runs_dsn: str) -> AsyncIterator[str]:
    from golem.orchestrator.runs import apply_schema

    async with await psycopg.AsyncConnection.connect(runs_dsn, autocommit=True) as conn:
        await apply_schema(conn)
        await conn.execute("TRUNCATE runs, run_tasks, proposals, process_stages")
    yield runs_dsn


def _audit_dsn(postgres: PostgresContainer, user: str, password: str) -> str:
    host = postgres.get_container_host_ip()
    port = postgres.get_exposed_port(5432)
    return f"host={host} port={port} dbname=golem_audit user={user} password={password}"


@pytest.fixture(scope="session")
def audit_dsn(postgres: PostgresContainer) -> str:
    return _audit_dsn(postgres, "golem_edge", "dev-only-golem-edge")


@pytest.fixture(scope="session")
def mcp_audit_dsn(postgres: PostgresContainer) -> str:
    return _audit_dsn(postgres, "golem_mcp", "dev-only-golem-mcp")


@pytest.fixture
async def audit_admin_dsn(postgres: PostgresContainer) -> AsyncIterator[str]:
    """Superuser DSN: the only way to read audit rows back, since service roles only INSERT."""
    dsn = _audit_dsn(postgres, postgres.username, postgres.password)
    async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as conn:
        await conn.execute("TRUNCATE audit_log")
    yield dsn


@pytest.fixture(scope="session")
def k3s() -> Iterator["K3SContainer"]:
    from testcontainers.community.k3s import K3SContainer

    # Mounting the host's /sys/fs/cgroup (the module's default) breaks pod sandboxes on cgroup v2
    # hosts such as CI runners: "cgroup.procs: no such file or directory" (testcontainers-python
    # issue 591).
    with K3SContainer("rancher/k3s:v1.33.4-k3s1", enable_cgroup_mount=False) as container:
        yield container


@pytest.fixture(scope="session")
def k3s_api_client(k3s: "K3SContainer") -> Iterator[ApiClient]:
    import yaml
    from kubernetes.config import new_client_from_config_dict

    api_client = new_client_from_config_dict(
        yaml.safe_load(k3s.config_yaml()), persist_config=False
    )
    with api_client:
        yield api_client


@pytest.fixture
def silent_server() -> Iterator[str]:
    """``host:port`` of a server that accepts connections and never answers, like a peer
    behind a stalled link."""
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen()
    server.settimeout(0.1)
    accepted: list[socket.socket] = []
    stopping = threading.Event()

    def accept() -> None:
        while not stopping.is_set():
            try:
                accepted.append(server.accept()[0])
            except OSError:
                continue

    thread = threading.Thread(target=accept, daemon=True)
    thread.start()
    yield f"127.0.0.1:{server.getsockname()[1]}"
    stopping.set()
    thread.join()
    for conn in accepted:
        conn.close()
    server.close()
