"""The edge's proposal and report routes (ADR 0015, ADR 0018): only a person's token, one token
from the caller's bucket, an audit row before anything is served, and the forwarded request
built from nothing but the verified principal and the agents it reviews."""

import json
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest
from starlette.requests import Request
from starlette.responses import JSONResponse
from test_edge_app import (
    CARD_KEY,
    CARDS,
    EDGE_TOKEN,
    PUBLIC_BASE_URL,
    REGISTRY,
    UNREACHABLE_DSN,
    audit_rows,
    authenticate,
)

from golem.edge.app import create_edge_app
from golem.edge.card_signing import card_keys
from golem.edge.policy import ChainLimits
from golem.ratelimit import Limiter, Rate

PROPOSAL = str(uuid.uuid4())
REVIEWERS = {"desk": frozenset({"user:bob"}), "wiki": frozenset({"user:bob", "user:carol"})}


@dataclass
class TaskPort:
    """Stands in for the task service's edge port: records what arrives, answers 200."""

    received: list[tuple[str, str, dict[str, str], Any]] = field(default_factory=list)

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        request = Request(scope, receive)
        body = await request.body()
        self.received.append(
            (
                request.method,
                str(request.url.path) + (f"?{request.url.query}" if request.url.query else ""),
                {k.lower(): v for k, v in request.headers.items()},
                json.loads(body) if body else None,
            )
        )
        await JSONResponse({"ok": True})(scope, receive, send)


@pytest.fixture
def port() -> TaskPort:
    return TaskPort()


def client_for(port: TaskPort, audit_dsn: str, **extra: Any) -> httpx.AsyncClient:
    edge = create_edge_app(
        authenticate=authenticate,
        registry=REGISTRY,
        limits=ChainLimits(max_depth=3),
        audit_dsn=audit_dsn,
        forward=httpx.AsyncClient(transport=httpx.ASGITransport(app=port), base_url="http://t"),
        edge_token=EDGE_TOKEN,
        cards=CARDS,
        card_keys=card_keys([CARD_KEY]),
        public_base_url=PUBLIC_BASE_URL,
        reviewers=REVIEWERS,
        **extra,
    )
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=edge, client=("10.0.0.7", 51234)),
        base_url="http://edge",
    )


@pytest.fixture
async def edge(port: TaskPort, audit_dsn: str, audit_admin_dsn: str) -> AsyncIterator[Any]:
    async with client_for(port, audit_dsn) as client:
        yield client


def bearer(token: str = "bob-token") -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "X-Golem-Reviews": "forged",
        "X-Golem-Principal": "x",
    }


async def test_a_listing_is_audited_then_forwarded_with_the_agents_the_person_reviews(
    edge: httpx.AsyncClient, port: TaskPort, audit_admin_dsn: str
) -> None:
    response = await edge.get("/proposals?agent=desk&state=pending,failed", headers=bearer())

    assert response.status_code == 200
    [(method, path, headers, _)] = port.received
    assert (method, path) == ("GET", "/proposals?agent=desk&state=pending%2Cfailed")
    assert headers["x-golem-principal"] == "user:bob"
    # Built from the catalogs, never from what the client sent.
    assert headers["x-golem-reviews"] == "desk,wiki"
    assert headers["x-golem-edge-token"] == EDGE_TOKEN
    assert await audit_rows(audit_admin_dsn) == [
        ("user:bob", "proposals", "ListProposals", "allow", "10.0.0.7")
    ]


async def test_a_person_who_reviews_nothing_is_forwarded_with_an_empty_list(
    edge: httpx.AsyncClient, port: TaskPort
) -> None:
    await edge.get("/proposals", headers=bearer("alice-token"))

    [(_, _, headers, _)] = port.received
    assert (headers["x-golem-principal"], headers["x-golem-reviews"]) == ("user:alice", "")


async def test_one_proposal_and_a_decision_are_forwarded_and_audited(
    edge: httpx.AsyncClient, port: TaskPort, audit_admin_dsn: str
) -> None:
    await edge.get(f"/proposals/{PROPOSAL}", headers=bearer())
    await edge.post(
        f"/proposals/{PROPOSAL}/decision",
        json={"decision": "reject", "reason": "Wrong customer.", "extra": 1},
        headers=bearer(),
    )

    (_, read, _, _), (method, decided, _, body) = port.received
    assert read == f"/proposals/{PROPOSAL}"
    assert (method, decided) == ("POST", f"/proposals/{PROPOSAL}/decision")
    assert body == {"decision": "reject", "reason": "Wrong customer."}
    rows = await audit_rows(audit_admin_dsn)
    assert [(row[2], row[3]) for row in rows] == [
        ("ReadProposal", "allow"),
        ("DecideProposal", "allow"),
    ]


async def test_reports_are_forwarded_and_audited(
    edge: httpx.AsyncClient, port: TaskPort, audit_admin_dsn: str
) -> None:
    await edge.get("/reports?agent=desk", headers=bearer())
    await edge.get("/reports/task-1", headers=bearer())

    assert [path for _, path, _, _ in port.received] == ["/reports?agent=desk", "/reports/task-1"]
    rows = await audit_rows(audit_admin_dsn)
    assert [(row[1], row[2]) for row in rows] == [
        ("reports", "ListReports"),
        ("reports", "ReadReport"),
    ]


async def test_an_agent_never_decides_nor_reads_proposals(
    edge: httpx.AsyncClient, port: TaskPort, audit_admin_dsn: str
) -> None:
    listing = await edge.get("/proposals", headers=bearer("discovery-for-alice"))
    decision = await edge.post(
        f"/proposals/{PROPOSAL}/decision",
        json={"decision": "accept"},
        headers=bearer("discovery-for-alice"),
    )

    assert (listing.status_code, listing.json()["error"]) == (403, "agents_do_not_decide")
    assert decision.status_code == 403
    assert port.received == []
    assert {row[3] for row in await audit_rows(audit_admin_dsn)} == {"deny: agents_do_not_decide"}


@pytest.mark.parametrize(
    ("method", "path", "body", "status"),
    [
        ("GET", "/proposals?state=nope", None, 400),
        ("GET", "/proposals?agent=Bad!", None, 400),
        ("GET", "/proposals?color=red", None, 400),
        ("GET", "/proposals/not-a-uuid", None, 404),
        ("POST", f"/proposals/{PROPOSAL}/decision", {"decision": "maybe"}, 400),
        ("POST", f"/proposals/{PROPOSAL}/decision", {"decision": "reject", "reason": 1}, 400),
        ("GET", "/reports/bad$id", None, 404),
    ],
)
async def test_a_malformed_request_is_refused_audited_and_not_forwarded(
    edge: httpx.AsyncClient,
    port: TaskPort,
    audit_admin_dsn: str,
    method: str,
    path: str,
    body: Any,
    status: int,
) -> None:
    response = await edge.request(method, path, json=body, headers=bearer())

    assert response.status_code == status
    assert port.received == []
    [row] = await audit_rows(audit_admin_dsn)
    assert row[3].startswith("deny: ")


async def test_without_the_audit_log_nothing_is_served(port: TaskPort) -> None:
    async with client_for(port, UNREACHABLE_DSN) as client:
        response = await client.get("/proposals", headers=bearer())

    assert response.status_code == 503
    assert port.received == []


async def test_without_a_token_it_is_401(edge: httpx.AsyncClient, port: TaskPort) -> None:
    response = await edge.get("/proposals")

    assert response.status_code == 401
    assert port.received == []


async def test_the_routes_share_the_callers_bucket(port: TaskPort, audit_dsn: str) -> None:
    callers = Limiter(Rate(per_minute=60, burst=1), clock=lambda: 0.0)
    async with client_for(port, audit_dsn, callers=callers) as client:
        first = await client.get("/proposals", headers=bearer())
        second = await client.get("/reports", headers=bearer())

    assert (first.status_code, second.status_code) == (200, 429)
