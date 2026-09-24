from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import psycopg
import pytest
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
