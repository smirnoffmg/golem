"""Tasks live in golem_tasks, so a restarted task service still knows them."""

import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from a2a.server.context import ServerCallContext
from a2a.server.tasks import DatabaseTaskStore, TaskStore
from a2a.types.a2a_pb2 import ListTasksRequest, Task, TaskState, TaskStatus
from google.protobuf.timestamp_pb2 import Timestamp
from sqlalchemy.ext.asyncio import AsyncEngine
from test_tasks_service import FakeOrchestrator, create_app, make_card
from testcontainers.community.postgres import PostgresContainer

from golem.tasks.app import EdgePrincipal
from golem.tasks.store import backfill_agents, tasks_engine, tasks_store


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
    # golem_runs outlives the task service too: one orchestrator's records across the restart.
    orchestrator = FakeOrchestrator()
    async with service(engine, orchestrator) as before:
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

    orchestrator.finish(task["id"], succeeded=True, detail="MR !7 opened")
    async with service(engine, orchestrator) as after:
        seen = await rpc(after, "GetTask", {"id": task["id"], "tenant": "reviewer"})
        finished = await after.post("/internal/run-outcome", json={"task_id": task["id"]})
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


async def test_list_tasks_in_golem_tasks_shows_only_the_callers_tasks(engine: AsyncEngine) -> None:
    async def list_as(client: httpx.AsyncClient, principal: str) -> list[str]:
        body = (
            await client.post(
                "/a2a",
                headers={"A2A-Version": "1.0", "X-Golem-Principal": principal},
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "ListTasks",
                    "params": {"tenant": "reviewer"},
                },
            )
        ).json()
        return [task["id"] for task in body["result"]["tasks"]]

    async with service(engine, FakeOrchestrator()) as client:
        task = (
            await rpc(
                client,
                "SendMessage",
                {
                    "tenant": "reviewer",
                    "message": {
                        "role": "ROLE_USER",
                        "messageId": "m-list",
                        "parts": [{"text": "go"}],
                    },
                },
            )
        )["task"]

        assert task["id"] in await list_as(client, "user:alice")
        assert task["id"] not in await list_as(client, "user:mallory")


# ListTasks names an agent (ADR 0018): the tenant the edge checked filters the caller's tasks.


async def list_ids(
    client: httpx.AsyncClient, tenant: str, principal: str = "user:alice"
) -> list[str]:
    body = (
        await client.post(
            "/a2a",
            headers={"A2A-Version": "1.0", "X-Golem-Principal": principal},
            json={"jsonrpc": "2.0", "id": 1, "method": "ListTasks", "params": {"tenant": tenant}},
        )
    ).json()
    return [task["id"] for task in body["result"]["tasks"]]


async def send_to(client: httpx.AsyncClient, tenant: str, message_id: str) -> str:
    params = {
        "tenant": tenant,
        "message": {"role": "ROLE_USER", "messageId": message_id, "parts": [{"text": "go"}]},
    }
    return (await rpc(client, "SendMessage", params))["task"]["id"]


async def test_list_tasks_naming_an_agent_lists_only_that_agents_tasks(
    engine: AsyncEngine,
) -> None:
    async with service(engine, FakeOrchestrator()) as client:
        reviewers = await send_to(client, "reviewer", "m-agent-1")
        discovery = await send_to(client, "discovery", "m-agent-2")

        listed = await list_ids(client, "reviewer")

    assert reviewers in listed
    assert discovery not in listed


def stored_task(task_id: str, second: int, agent: str | None) -> Task:
    task = Task(
        id=task_id,
        context_id=str(uuid.uuid4()),
        status=TaskStatus(
            state=TaskState.TASK_STATE_COMPLETED,
            timestamp=Timestamp(seconds=1_800_000_000 + second),
        ),
    )
    if agent is not None:
        task.metadata.update({"golemAgent": agent})
    return task


async def pages(store: TaskStore, context: ServerCallContext, tenant: str = "") -> list[Any]:
    seen: list[Any] = []
    token = ""
    while True:
        page = await store.list(
            ListTasksRequest(tenant=tenant, page_size=3, page_token=token), context
        )
        seen.append(([t.id for t in page.tasks], page.total_size, page.next_page_token))
        if not page.next_page_token:
            return seen
        token = page.next_page_token


async def saved(engine: AsyncEngine, tasks: list[Task]) -> ServerCallContext:
    context = ServerCallContext(user=EdgePrincipal(f"user:{uuid.uuid4()}"))
    store = tasks_store(engine)
    for task in tasks:
        await store.save(task, context)
    return context


async def test_without_an_agent_the_list_pages_exactly_as_the_sdks_list(
    engine: AsyncEngine,
) -> None:
    # The override repeats the SDK's query; an upgrade that changes it must fail here.
    ids = [str(uuid.uuid4()) for _ in range(8)]
    # Equal timestamps exercise the id tie-break of the page token.
    context = await saved(
        engine,
        [stored_task(i, n // 2, "reviewer" if n % 2 else "discovery") for n, i in enumerate(ids)],
    )

    ours = await pages(tasks_store(engine), context)
    sdks = await pages(DatabaseTaskStore(engine), context)

    assert ours == sdks
    assert sorted(i for page, _, _ in ours for i in page) == sorted(ids)


async def test_naming_an_agent_pages_through_that_agents_tasks_only(engine: AsyncEngine) -> None:
    reviewers = [str(uuid.uuid4()) for _ in range(5)]
    others = [str(uuid.uuid4()) for _ in range(4)]
    context = await saved(
        engine,
        [stored_task(i, n, "reviewer") for n, i in enumerate(reviewers)]
        + [stored_task(i, n, "discovery") for n, i in enumerate(others)],
    )

    walked = await pages(tasks_store(engine), context, tenant="reviewer")

    assert [i for page, _, _ in walked for i in page] == list(reversed(reviewers))
    assert {total for _, total, _ in walked} == {5}


async def test_the_backfill_records_the_agent_of_tasks_that_have_a_run(
    engine: AsyncEngine,
) -> None:
    with_run, without_run = str(uuid.uuid4()), str(uuid.uuid4())
    context = await saved(
        engine, [stored_task(with_run, 1, None), stored_task(without_run, 2, None)]
    )
    store = tasks_store(engine)

    async def agents_of_tasks(task_ids: tuple[str, ...]) -> dict[str, str]:
        return {with_run: "reviewer"} if with_run in task_ids else {}

    await backfill_agents(engine, agents_of_tasks)

    listed = await store.list(ListTasksRequest(tenant="reviewer"), context)
    untouched = await store.get(without_run, context)
    assert [t.id for t in listed.tasks] == [with_run]
    assert untouched is not None
    assert "golemAgent" not in untouched.metadata
