"""`docker compose up` smoke test: the edge serves a public card and refuses anonymous calls.

Builds the image and starts the whole compose project, so it is opt-in: GOLEM_SMOKE=1.
The project is torn down with its volumes afterwards.
"""

import os
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("GOLEM_SMOKE") != "1", reason="set GOLEM_SMOKE=1 to run the compose smoke test"
)

COMPOSE = ["docker", "compose", "-f", str(Path(__file__).parent.parent / "deploy/compose.yaml")]
EDGE = f"http://127.0.0.1:{os.environ.get('GOLEM_EDGE_PORT', '8480')}"
CARD_PATH = "/agents/discovery/.well-known/agent-card.json"
STARTUP_SECONDS = 120


def edge_ready(client: httpx.Client) -> bool:
    try:
        return client.get(CARD_PATH).status_code == 200
    except httpx.HTTPError:
        return False


@pytest.fixture(scope="module")
def edge() -> Iterator[httpx.Client]:
    try:
        subprocess.run([*COMPOSE, "up", "-d", "--build"], check=True)
        with httpx.Client(base_url=EDGE, timeout=5) as client:
            deadline = time.monotonic() + STARTUP_SECONDS
            while not edge_ready(client):
                if time.monotonic() > deadline:
                    subprocess.run([*COMPOSE, "logs"], check=False)
                    pytest.fail("the edge did not serve a card in time")
                time.sleep(1)
            yield client
    finally:
        subprocess.run([*COMPOSE, "down", "-v"], check=False)


def test_the_edge_serves_the_public_agent_card(edge: httpx.Client) -> None:
    card = edge.get(CARD_PATH).json()

    assert card["name"] == "discovery"
    assert card["supportedInterfaces"][0]["url"] == f"{EDGE}/a2a"


def test_the_edge_refuses_a_call_without_a_token(edge: httpx.Client) -> None:
    response = edge.post(
        "/a2a",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "SendMessage",
            "params": {
                "tenant": "discovery",
                "message": {"role": "ROLE_USER", "messageId": "m-1", "parts": [{"text": "go"}]},
            },
        },
    )

    assert response.status_code == 401
    assert response.headers["www-authenticate"].startswith("Bearer")


def test_the_edge_refuses_a_token_it_cannot_verify(edge: httpx.Client) -> None:
    response = edge.post("/a2a", json={}, headers={"Authorization": "Bearer not-a-jwt"})

    assert response.status_code == 401


def test_every_service_is_running(edge: httpx.Client) -> None:
    result = subprocess.run(
        [*COMPOSE, "ps", "--status", "running", "--services"],
        check=True,
        capture_output=True,
        text=True,
    )

    assert set(result.stdout.split()) == {"postgres", "edge", "tasks", "reconciler"}
