"""Push notifications reach allowed receivers only (no request forgery into the cluster)."""

from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from a2a.server.tasks import InMemoryPushNotificationConfigStore
from test_tasks_service import FakeOrchestrator, make_card

from golem.tasks.app import PushDelivery, create_app

ADAPTER = "http://adapter.golem.svc:8080"


@pytest.fixture
def pushed() -> list[httpx.Request]:
    return []


@pytest.fixture
async def client(pushed: list[httpx.Request]) -> AsyncIterator[httpx.AsyncClient]:
    async def receive(request: httpx.Request) -> httpx.Response:
        pushed.append(request)
        return httpx.Response(204)

    push = PushDelivery(
        config_store=InMemoryPushNotificationConfigStore(),
        client=httpx.AsyncClient(transport=httpx.MockTransport(receive)),
        allowed_prefixes=(f"{ADAPTER}/",),
    )
    app = create_app(make_card(), FakeOrchestrator(), push=push)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://tasks"
    ) as test_client:
        yield test_client


def send_body(push_url: str) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "SendMessage",
        "params": {
            "tenant": "reviewer",
            "message": {"role": "ROLE_USER", "messageId": "m-1", "parts": [{"text": "go"}]},
            "configuration": {
                "taskPushNotificationConfig": {"url": push_url, "token": "per-run-token"}
            },
        },
    }


async def send(client: httpx.AsyncClient, push_url: str) -> dict[str, Any]:
    return (
        await client.post(
            "/a2a",
            headers={"A2A-Version": "1.0", "X-Golem-Principal": "service:jira-adapter"},
            json=send_body(push_url),
        )
    ).json()


async def test_a_terminal_state_is_pushed_to_an_allowed_receiver(
    client: httpx.AsyncClient, pushed: list[httpx.Request]
) -> None:
    task = (await send(client, f"{ADAPTER}/a2a/push"))["result"]["task"]

    await client.post(
        "/internal/run-outcome",
        json={
            "task_id": task["id"],
            "tenant": "reviewer",
            "caller": "service:jira-adapter",
            "run_id": "run-1",
            "status": "succeeded",
            "detail": "MR !42 opened",
        },
    )

    terminal = [r for r in pushed if b"TASK_STATE_COMPLETED" in r.content]
    assert len(terminal) == 1
    assert terminal[0].url == f"{ADAPTER}/a2a/push"
    assert terminal[0].headers["X-A2A-Notification-Token"] == "per-run-token"
    assert b"MR !42 opened" in terminal[0].content


@pytest.mark.parametrize(
    "url",
    [
        "http://169.254.169.254/latest/meta-data/",
        "http://postgres:5432/",
        "http://adapter.golem.svc:8080.evil.test/a2a/push",
    ],
)
async def test_a_push_url_outside_the_allowed_receivers_is_refused(
    client: httpx.AsyncClient, pushed: list[httpx.Request], url: str
) -> None:
    body = await send(client, url)

    assert "error" in body
    assert pushed == []
