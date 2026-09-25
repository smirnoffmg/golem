"""The task service's three listeners, and what a caller holding only one port can do.

NetworkPolicy admits a caller to a port, not to a path (ADR 0009), so each port serves only the
routes of the callers admitted to it, and the public port also wants the edge's shared secret.
"""

import asyncio
import os
import signal
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
import uvicorn
from prometheus_client import CollectorRegistry
from starlette.testclient import TestClient
from test_settings import TASKS_ENV
from test_tasks_service import FakeOrchestrator, make_card

from golem.metrics import Metrics
from golem.run_token import SigningKey
from golem.settings import task_service_settings
from golem.tasks.__main__ import Listener, listener_servers, serve_all
from golem.tasks.app import Listeners, create_listeners

EDGE_TOKEN = "edge-shared-secret"
KEY = SigningKey.generate(kid="run-2026-09")


@pytest.fixture
def orchestrator() -> FakeOrchestrator:
    return FakeOrchestrator()


@pytest.fixture
def listeners(orchestrator: FakeOrchestrator) -> Listeners:
    return create_listeners(make_card(), orchestrator, edge_token=EDGE_TOKEN, run_keys=(KEY,))


@pytest.fixture
def public(listeners: Listeners) -> Iterator[TestClient]:
    with TestClient(listeners.public) as client:
        yield client


@pytest.fixture
def internal_read(listeners: Listeners) -> Iterator[TestClient]:
    with TestClient(listeners.internal_read) as client:
        yield client


@pytest.fixture
def internal_write(listeners: Listeners) -> Iterator[TestClient]:
    with TestClient(listeners.internal_write) as client:
        yield client


def send_message(principal: str | None = "user:alice", **headers: str) -> dict[str, Any]:
    if principal is not None:
        headers["X-Golem-Principal"] = principal
    return {
        "headers": {"A2A-Version": "1.0", **headers},
        "json": {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "SendMessage",
            "params": {
                "tenant": "reviewer",
                "message": {"role": "ROLE_USER", "messageId": "m1", "parts": [{"text": "go"}]},
            },
        },
    }


FORGED_OUTCOME = {
    "task_id": "t-1",
    "tenant": "reviewer",
    "caller": "user:alice",
    "run_id": "run-1",
    "status": "succeeded",
}


# --- Each port serves only its own routes -------------------------------------------------------


def test_the_public_port_starts_runs_for_the_edge(
    public: TestClient, orchestrator: FakeOrchestrator
) -> None:
    response = public.post("/a2a", **send_message(**{"X-Golem-Edge-Token": EDGE_TOKEN}))

    assert response.status_code == 200
    assert response.json()["result"]["task"]["status"]["state"] == "TASK_STATE_WORKING"
    assert [run.caller for run in orchestrator.started] == ["user:alice"]


def test_the_public_port_serves_no_internal_route(public: TestClient) -> None:
    token = {"X-Golem-Edge-Token": EDGE_TOKEN}

    assert public.get("/internal/run-keys", headers=token).status_code == 404
    assert public.get("/internal/runs/run-1", headers=token).status_code == 404
    assert public.post("/internal/run-outcome", json=FORGED_OUTCOME, headers=token).status_code == (
        404
    )


def test_the_read_port_serves_run_keys_and_run_status(internal_read: TestClient) -> None:
    keys = internal_read.get("/internal/run-keys")

    assert keys.status_code == 200
    assert [k["kid"] for k in keys.json()["keys"]] == ["run-2026-09"]
    assert internal_read.get("/internal/runs/run-1").json() == {"error": "run not found"}


def test_an_mcp_like_caller_cannot_start_a_run_through_the_read_port(
    internal_read: TestClient, orchestrator: FakeOrchestrator
) -> None:
    response = internal_read.post("/a2a", **send_message(**{"X-Golem-Edge-Token": EDGE_TOKEN}))

    assert response.status_code == 404
    assert internal_read.get("/.well-known/agent-card.json").status_code == 404
    assert orchestrator.started == []


def test_a_run_outcome_cannot_be_forged_through_the_read_port(internal_read: TestClient) -> None:
    assert internal_read.post("/internal/run-outcome", json=FORGED_OUTCOME).status_code == 404


def test_the_write_port_serves_run_outcomes_only(
    internal_write: TestClient, orchestrator: FakeOrchestrator
) -> None:
    unknown_task = internal_write.post("/internal/run-outcome", json=FORGED_OUTCOME)

    assert unknown_task.json() == {"error": "task not found"}
    assert internal_write.post("/a2a", **send_message()).status_code == 404
    assert internal_write.get("/internal/run-keys").status_code == 404
    assert internal_write.get("/internal/runs/run-1").status_code == 404
    assert orchestrator.started == []


# --- The public port authenticates the edge -----------------------------------------------------


@pytest.mark.parametrize(
    "headers",
    [{}, {"X-Golem-Edge-Token": ""}, {"X-Golem-Edge-Token": "guessed"}],
    ids=["no token", "empty token", "wrong token"],
)
def test_a_forged_principal_without_the_edge_token_is_refused_and_starts_no_run(
    public: TestClient, orchestrator: FakeOrchestrator, headers: dict[str, str]
) -> None:
    response = public.post("/a2a", **send_message(principal="user:ceo", **headers))

    assert response.status_code == 401
    body = response.json()
    assert body["jsonrpc"] == "2.0"
    assert body["id"] is None
    assert body["error"]["code"] == -32040
    assert orchestrator.started == []


def test_without_the_edge_token_not_even_an_anonymous_request_is_served(
    public: TestClient, orchestrator: FakeOrchestrator
) -> None:
    assert public.post("/a2a", **send_message(principal=None)).status_code == 401
    assert public.get("/.well-known/agent-card.json").status_code == 401
    assert orchestrator.started == []


def test_the_listeners_refuse_to_be_built_without_an_edge_token() -> None:
    with pytest.raises(ValueError, match="edge token"):
        create_listeners(make_card(), FakeOrchestrator(), edge_token="")


# --- Three servers, one process -----------------------------------------------------------------


def servers_for(listeners: Listeners) -> list[Listener]:
    return [
        Listener(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning"))
        for app in (listeners.public, listeners.internal_read, listeners.internal_write)
    ]


async def started(servers: list[Listener], serving: asyncio.Task) -> list[str]:
    while not all(s.started for s in servers):
        assert not serving.done(), serving
        await asyncio.sleep(0.05)
    return [f"http://127.0.0.1:{s.servers[0].sockets[0].getsockname()[1]}" for s in servers]


async def test_the_three_listeners_serve_their_routes_on_their_own_ports(
    listeners: Listeners,
) -> None:
    servers = servers_for(listeners)
    serving = asyncio.create_task(serve_all(servers))
    public_url, read_url, write_url = await started(servers, serving)
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            refused = await client.post(f"{public_url}/a2a", **send_message())
            keys = await client.get(f"{read_url}/internal/run-keys")
            no_a2a = await client.post(f"{read_url}/a2a", **send_message())
            outcome = await client.post(f"{write_url}/internal/run-outcome", json=FORGED_OUTCOME)
    finally:
        os.kill(os.getpid(), signal.SIGTERM)
        await asyncio.wait_for(serving, 10)

    assert (refused.status_code, keys.status_code, no_a2a.status_code) == (401, 200, 404)
    assert outcome.json() == {"error": "task not found"}


async def test_sigterm_stops_all_three_servers(listeners: Listeners) -> None:
    servers = servers_for(listeners)
    serving = asyncio.create_task(serve_all(servers))
    await started(servers, serving)

    os.kill(os.getpid(), signal.SIGTERM)
    await asyncio.wait_for(serving, 10)

    assert all(s.should_exit for s in servers)


async def test_when_one_server_stops_the_others_stop_too(listeners: Listeners) -> None:
    servers = servers_for(listeners)
    serving = asyncio.create_task(serve_all(servers))
    await started(servers, serving)

    servers[2].should_exit = True
    await asyncio.wait_for(serving, 10)

    assert all(s.should_exit for s in servers)


# --- Metrics (ADR 0013) --------------------------------------------------------------------------


def test_the_three_listeners_count_requests_by_route_template_under_one_process(
    orchestrator: FakeOrchestrator,
) -> None:
    metrics = Metrics("tasks")
    listeners = create_listeners(
        make_card(), orchestrator, edge_token=EDGE_TOKEN, run_keys=(KEY,), metrics=metrics
    )
    with (
        TestClient(listeners.public) as public,
        TestClient(listeners.internal_read) as read,
        TestClient(listeners.internal_write) as write,
    ):
        public.post("/a2a", **send_message(principal=None))
        for run_id in ("run-1", "run-2", "6f1c0e4e-0000-4000-8000-000000000000"):
            read.get(f"/internal/runs/{run_id}")
        write.post("/internal/run-outcome", json=FORGED_OUTCOME)

    routes = {
        s.labels["route"]
        for family in metrics.registry.collect()
        for s in family.samples
        if s.name == "golem_http_requests_total"
    }
    assert routes == {"/a2a", "/internal/runs/{run_id}", "/internal/run-outcome"}
    assert (
        metrics.registry.get_sample_value(
            "golem_authentication_failures_total", {"process": "tasks"}
        )
        == 1
    )


def test_the_process_serves_metrics_on_a_fourth_port_of_its_own(listeners: Listeners) -> None:
    settings = task_service_settings(TASKS_ENV)

    servers = listener_servers(listeners, settings, CollectorRegistry())

    assert [s.config.port for s in servers] == [8000, 8001, 8002, 9090]
    with TestClient(servers[3].config.app) as metrics, TestClient(listeners.internal_read) as read:
        assert metrics.get("/metrics").status_code == 200
        assert read.get("/metrics").status_code == 404
