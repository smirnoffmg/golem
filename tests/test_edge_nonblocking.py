"""A slow token check (a key refetch) must not stall other requests on the edge."""

import asyncio
import time

import httpx
from test_edge_app import EDGE_TOKEN, discovery_card

from golem.edge.app import create_edge_app
from golem.edge.auth import AuthFailure, Principal
from golem.edge.policy import ChainLimits, Registry


def slow_authenticate(token: str) -> Principal | AuthFailure:
    time.sleep(0.5)
    return AuthFailure(reason="unknown key")


async def test_a_slow_token_check_does_not_block_the_event_loop() -> None:
    app = create_edge_app(
        authenticate=slow_authenticate,
        registry=Registry(allowed_callers={}),
        limits=ChainLimits(max_depth=3),
        audit_dsn="host=unused",
        forward=httpx.AsyncClient(),
        edge_token=EDGE_TOKEN,
        cards={"discovery": discovery_card()},
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://edge"
    ) as client:

        async def card_done_at() -> float:
            await asyncio.sleep(0.05)
            await client.get("/agents/discovery/.well-known/agent-card.json")
            return time.monotonic()

        # Measured from before both requests start: a blocked loop delays the whole card task.
        started = time.monotonic()
        _, done_at = await asyncio.gather(
            client.post("/a2a", headers={"Authorization": "Bearer x"}, json={}),
            card_done_at(),
        )

    latency = done_at - started
    assert latency < 0.3
