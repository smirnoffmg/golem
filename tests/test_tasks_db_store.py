"""Tasks live in golem_tasks, so a restarted task service still knows them."""

from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine
from test_tasks_service import FakeOrchestrator, create_app, make_card
from testcontainers.community.postgres import PostgresContainer

from golem.tasks.store import tasks_engine, tasks_store


@pytest.fixture
async def engine(postgres: PostgresContainer) -> AsyncIterator[AsyncEngine]:
    host = postgres.get_container_host_ip()
    port = postgres.get_exposed_port(5432)
    engine = tasks_engine(
        f"postgresql+asyncpg://golem_tasks:dev-only-golem-tasks@{host}:{port}/golem_tasks"
    )
    yield engine
    await engine.dispose()


def service(engine: AsyncEngine, orchestrator: FakeOrchestrator) -> httpx.AsyncClient:
    app = create_app(make_card(), orchestrator, tasks_store(engine))
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://tasks")


async def rpc(client: httpx.AsyncClient, method: str, params: dict[str, Any]) -> dict[str, Any]:
    body = (
        await client.post(
            "/a2a",
            headers={"A2A-Version": "1.0", "X-Golem-Principal": "user:alice"},
            json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        )
    ).json()
    assert "error" not in body, body
    return body["result"]


async def test_a_task_survives_a_restart_of_the_task_service(engine: AsyncEngine) -> None:
    async with service(engine, FakeOrchestrator()) as before:
        task = (
            await rpc(
                before,
                "SendMessage",
                {
                    "tenant": "reviewer",
                    "message": {"role": "ROLE_USER", "messageId": "m-1", "parts": [{"text": "go"}]},
                },
            )
        )["task"]

    async with service(engine, FakeOrchestrator()) as after:
        seen = await rpc(after, "GetTask", {"id": task["id"], "tenant": "reviewer"})
        finished = await after.post(
            "/internal/run-outcome",
            json={
                "task_id": task["id"],
                "tenant": "reviewer",
                "caller": "user:alice",
                "run_id": "run-1",
                "status": "succeeded",
                "detail": "MR !7 opened",
            },
        )
        done = await rpc(after, "GetTask", {"id": task["id"], "tenant": "reviewer"})

    assert seen["status"]["state"] == "TASK_STATE_WORKING"
    assert finished.status_code == 200
    assert done["status"]["state"] == "TASK_STATE_COMPLETED"


async def test_another_caller_cannot_see_the_task(engine: AsyncEngine) -> None:
    async with service(engine, FakeOrchestrator()) as client:
        task = (
            await rpc(
                client,
                "SendMessage",
                {
                    "tenant": "reviewer",
                    "message": {"role": "ROLE_USER", "messageId": "m-2", "parts": [{"text": "go"}]},
                },
            )
        )["task"]
        response = await client.post(
            "/a2a",
            headers={"A2A-Version": "1.0", "X-Golem-Principal": "user:mallory"},
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "GetTask",
                "params": {"id": task["id"], "tenant": "reviewer"},
            },
        )

    assert "error" in response.json()


async def test_push_tokens_are_stored_encrypted(engine: AsyncEngine) -> None:
    from cryptography.fernet import Fernet
    from sqlalchemy import text

    from golem.tasks.app import PushDelivery
    from golem.tasks.store import push_config_store

    push = PushDelivery(
        config_store=push_config_store(engine, Fernet.generate_key().decode()),
        client=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(204))),
        allowed_prefixes=("http://adapter:8080/",),
    )
    app = create_app(make_card(), FakeOrchestrator(), tasks_store(engine), push)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://tasks"
    ) as client:
        await rpc(
            client,
            "SendMessage",
            {
                "tenant": "reviewer",
                "message": {"role": "ROLE_USER", "messageId": "m-3", "parts": [{"text": "go"}]},
                "configuration": {
                    "taskPushNotificationConfig": {
                        "url": "http://adapter:8080/a2a/push",
                        "token": "secret-push-token",
                    }
                },
            },
        )

    async with engine.connect() as conn:
        rows = (await conn.execute(text("SELECT * FROM push_notification_configs"))).all()
    assert rows
    assert all("secret-push-token" not in str(row) for row in rows)
