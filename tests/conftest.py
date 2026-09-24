from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import psycopg
import pytest
from kubernetes.client import ApiClient
from testcontainers.community.postgres import PostgresContainer

INIT_SQL = Path(__file__).parent.parent / "deploy" / "postgres" / "init.sql"


@pytest.fixture(scope="session")
def postgres() -> Iterator[PostgresContainer]:
    # The same init.sql as deploy/, so tests exercise the real roles and grants.
    container = PostgresContainer("postgres:17", driver=None).with_volume_mapping(
        str(INIT_SQL), "/docker-entrypoint-initdb.d/init.sql", "ro"
    )
    with container:
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
        await conn.execute("TRUNCATE runs, run_tasks")
    yield runs_dsn


def _audit_dsn(postgres: PostgresContainer, user: str, password: str) -> str:
    host = postgres.get_container_host_ip()
    port = postgres.get_exposed_port(5432)
    return f"host={host} port={port} dbname=golem_audit user={user} password={password}"


@pytest.fixture(scope="session")
def audit_dsn(postgres: PostgresContainer) -> str:
    return _audit_dsn(postgres, "golem_edge", "dev-only-golem-edge")


@pytest.fixture
async def audit_admin_dsn(postgres: PostgresContainer) -> AsyncIterator[str]:
    """Superuser DSN: the only way to read audit rows back, since service roles only INSERT."""
    dsn = _audit_dsn(postgres, postgres.username, postgres.password)
    async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as conn:
        await conn.execute("TRUNCATE audit_log")
    yield dsn


@pytest.fixture(scope="session")
def k3s_api_client() -> Iterator[ApiClient]:
    import yaml
    from kubernetes.config import new_client_from_config_dict
    from testcontainers.community.k3s import K3SContainer

    # Mounting the host's /sys/fs/cgroup (the module's default) breaks pod sandboxes on cgroup v2
    # hosts such as CI runners: "cgroup.procs: no such file or directory" (testcontainers-python
    # issue 591).
    with K3SContainer("rancher/k3s:v1.33.4-k3s1", enable_cgroup_mount=False) as k3s:
        api_client = new_client_from_config_dict(
            yaml.safe_load(k3s.config_yaml()), persist_config=False
        )
        with api_client:
            yield api_client
