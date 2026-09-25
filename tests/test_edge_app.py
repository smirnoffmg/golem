from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import httpx
import psycopg
import pytest
from a2a.types.a2a_pb2 import AgentCapabilities, AgentCard, AgentInterface
from starlette.types import ASGIApp, Receive, Scope, Send
from test_tasks_service import FakeOrchestrator, make_card

from golem.edge.app import (
    AUDIT_UNAVAILABLE,
    CALL_DENIED,
    RATE_LIMITED,
    UNAUTHENTICATED,
    create_edge_app,
)
from golem.edge.auth import AuthFailure, Principal
from golem.edge.policy import ChainLimits, Registry
from golem.metrics import Metrics
from golem.ratelimit import Limiter, Rate, parse_networks
from golem.run_status import RunStatuses
from golem.tasks.app import create_listeners

EDGE_TOKEN = "edge-shared-secret"
UNREACHABLE_DSN = "host=127.0.0.1 port=1 dbname=golem_audit user=golem_edge connect_timeout=1"

TOKENS = {
    "alice-token": Principal(name="user:alice", chain=()),
    "bob-token": Principal(name="user:bob", chain=()),
    "ci-token": Principal(name="service:ci", chain=()),
    # What a verified call token becomes (golem.edge.auth.authenticate_call).
    "discovery-for-alice": Principal(
        name="agent:discovery",
        chain=("discovery",),
        subject="user:alice",
        root_run_id="root-1",
        run_id="run-a",
    ),
    "discovery-for-bob": Principal(
        name="agent:discovery",
        chain=("discovery",),
        subject="user:bob",
        root_run_id="root-2",
        run_id="run-b",
    ),
}

REGISTRY = Registry(
    allowed_callers={
        "reviewer": frozenset({"user:*", "agent:discovery"}),
        "discovery": frozenset({"user:alice"}),
        "evaluator": frozenset({"service:gitlab-ci"}),
    }
)


def discovery_card() -> AgentCard:
    return AgentCard(
        name="discovery",
        description="Turns an epic into product decisions.",
        version="0.1.0",
        supported_interfaces=[
            AgentInterface(
                url="https://golem.example.test/a2a",
                protocol_binding="JSONRPC",
                tenant="discovery",
                protocol_version="1.0",
            )
        ],
        capabilities=AgentCapabilities(streaming=False),
        default_input_modes=["text/plain"],
        default_output_modes=["text/plain"],
    )


def authenticate(token: str) -> Principal | AuthFailure:
    return TOKENS.get(token, AuthFailure("invalid token"))


@dataclass
class RunsOrchestrator(FakeOrchestrator):
    """Also answers run statuses, which the edge asks for a call token's issuing run."""

    statuses: dict[str, str] = field(
        default_factory=lambda: {"run-a": "running", "run-b": "running"}
    )
    asked: list[str] = field(default_factory=list)
    broken: bool = False

    async def status(self, run_id: str) -> str | None:
        self.asked.append(run_id)
        if self.broken:
            raise RuntimeError("golem_runs unavailable")
        return self.statuses.get(run_id)


@dataclass
class TaskService:
    """The real in-process task service, with the headers of every request it received."""

    orchestrator: RunsOrchestrator = field(default_factory=RunsOrchestrator)
    received: list[dict[str, str]] = field(default_factory=list)

    def statuses(self, clock: Any = None) -> RunStatuses:
        """The edge's view of run statuses: the task service's internal read port."""
        read = create_listeners(make_card(), self.orchestrator, edge_token=EDGE_TOKEN)
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=read.internal_read, raise_app_exceptions=False),
            base_url="http://tasks-read",
        )
        return RunStatuses(client, ttl_seconds=10, **({"clock": clock} if clock else {}))

    def app(self) -> ASGIApp:
        inner = create_listeners(make_card(), self.orchestrator, edge_token=EDGE_TOKEN).public

        async def recording(scope: Scope, receive: Receive, send: Send) -> None:
            if scope["type"] == "http":
                self.received.append({k.decode().lower(): v.decode() for k, v in scope["headers"]})
            await inner(scope, receive, send)

        return recording


@pytest.fixture
def tasks() -> TaskService:
    return TaskService()


def edge_client(tasks: TaskService, audit_dsn: str, **rate_limits: Any) -> httpx.AsyncClient:
    forward = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=tasks.app()), base_url="http://tasks"
    )
    edge = create_edge_app(
        authenticate=authenticate,
        registry=REGISTRY,
        limits=ChainLimits(max_depth=3),
        audit_dsn=audit_dsn,
        forward=forward,
        edge_token=EDGE_TOKEN,
        cards={"discovery": discovery_card()},
        **({"run_statuses": tasks.statuses()} | rate_limits),
    )
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=edge, client=("10.0.0.7", 51234)),
        base_url="http://edge",
    )


@pytest.fixture
async def edge(
    tasks: TaskService, audit_dsn: str, audit_admin_dsn: str
) -> AsyncIterator[httpx.AsyncClient]:
    async with edge_client(tasks, audit_dsn) as client:
        yield client


def send_message(tenant: str | None = "reviewer", method: str = "SendMessage") -> dict[str, Any]:
    params: dict[str, Any] = {
        "message": {"role": "ROLE_USER", "messageId": "m1", "parts": [{"text": "fix the test"}]}
    }
    if tenant is not None:
        params["tenant"] = tenant
    return {"jsonrpc": "2.0", "id": 7, "method": method, "params": params}


async def call(
    client: httpx.AsyncClient,
    body: dict[str, Any],
    token: str | None = "alice-token",
    extra_headers: dict[str, str] | None = None,
) -> httpx.Response:
    headers = {"A2A-Version": "1.0", **(extra_headers or {})}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    return await client.post("/a2a", json=body, headers=headers)


async def audit_rows(admin_dsn: str) -> list[tuple[str, str, str, str, str | None]]:
    async with await psycopg.AsyncConnection.connect(admin_dsn) as conn:
        cursor = await conn.execute(
            "SELECT account, target_system, operation, result, host(source_ip)"
            " FROM audit_log ORDER BY id"
        )
        return await cursor.fetchall()


def error_of(response: httpx.Response) -> dict[str, Any]:
    body = response.json()
    assert "result" not in body, body
    return body["error"]


async def test_allowed_call_is_audited_then_forwarded_and_creates_a_task(
    edge: httpx.AsyncClient, tasks: TaskService, audit_admin_dsn: str
) -> None:
    response = await call(edge, send_message())

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == 7
    assert body["result"]["task"]["status"]["state"] == "TASK_STATE_WORKING"
    [run] = tasks.orchestrator.started
    assert (run.agent, run.goal, run.message_id) == ("reviewer", "fix the test", "m1")
    assert await audit_rows(audit_admin_dsn) == [
        ("user:alice", "agent:reviewer", "SendMessage", "allow", "10.0.0.7")
    ]


async def test_forwarded_request_names_the_principal_and_passes_the_version(
    edge: httpx.AsyncClient, tasks: TaskService
) -> None:
    await call(
        edge,
        send_message(),
        extra_headers={"X-Golem-Principal": "user:mallory", "Cookie": "session=secret"},
    )

    [headers] = tasks.received
    assert headers["x-golem-principal"] == "user:alice"
    assert headers["a2a-version"] == "1.0"
    assert "authorization" not in headers
    assert "cookie" not in headers


async def test_forwarded_request_carries_the_edge_token_and_never_the_clients_one(
    edge: httpx.AsyncClient, tasks: TaskService
) -> None:
    response = await call(edge, send_message(), extra_headers={"X-Golem-Edge-Token": "forged"})

    [headers] = tasks.received
    assert headers["x-golem-edge-token"] == EDGE_TOKEN
    assert response.json()["result"]["task"]["status"]["state"] == "TASK_STATE_WORKING"


async def test_an_edge_with_the_wrong_token_starts_no_run(
    tasks: TaskService, audit_dsn: str, audit_admin_dsn: str
) -> None:
    edge = create_edge_app(
        authenticate=authenticate,
        registry=REGISTRY,
        limits=ChainLimits(max_depth=3),
        audit_dsn=audit_dsn,
        forward=httpx.AsyncClient(
            transport=httpx.ASGITransport(app=tasks.app()), base_url="http://tasks"
        ),
        edge_token="not-the-task-services",
        cards={},
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=edge), base_url="http://edge"
    ) as client:
        response = await call(client, send_message())

    assert response.status_code == 401
    assert tasks.orchestrator.started == []


async def test_trace_context_is_forwarded_so_the_run_joins_the_callers_trace(
    edge: httpx.AsyncClient, tasks: TaskService
) -> None:
    traceparent = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
    await call(
        edge,
        send_message(),
        extra_headers={"traceparent": traceparent, "tracestate": "vendor=1"},
    )

    [headers] = tasks.received
    assert headers["traceparent"] == traceparent
    assert headers["tracestate"] == "vendor=1"


async def test_a_malformed_traceparent_is_dropped(
    edge: httpx.AsyncClient, tasks: TaskService
) -> None:
    await call(edge, send_message(), extra_headers={"traceparent": "../../etc/passwd"})

    [headers] = tasks.received
    assert "traceparent" not in headers


async def test_task_service_runs_on_behalf_of_the_edge_principal(
    edge: httpx.AsyncClient, tasks: TaskService
) -> None:
    await call(edge, send_message())

    assert [run.caller for run in tasks.orchestrator.started] == ["user:alice"]


async def test_get_and_cancel_are_forwarded(
    edge: httpx.AsyncClient, tasks: TaskService, audit_admin_dsn: str
) -> None:
    task = (await call(edge, send_message())).json()["result"]["task"]

    got = await call(
        edge,
        {
            "jsonrpc": "2.0",
            "id": 8,
            "method": "GetTask",
            "params": {"id": task["id"], "tenant": "reviewer"},
        },
    )
    canceled = await call(
        edge,
        {
            "jsonrpc": "2.0",
            "id": 9,
            "method": "CancelTask",
            "params": {"id": task["id"], "tenant": "reviewer"},
        },
    )

    assert got.json()["result"]["id"] == task["id"]
    assert canceled.json()["result"]["status"]["state"] == "TASK_STATE_CANCELED"
    assert tasks.orchestrator.canceled == [task["id"]]
    assert [row[2] for row in await audit_rows(audit_admin_dsn)] == [
        "SendMessage",
        "GetTask",
        "CancelTask",
    ]


def list_tasks(tenant: str = "reviewer") -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": 10, "method": "ListTasks", "params": {"tenant": tenant}}


async def test_list_tasks_is_audited_and_shows_only_the_callers_tasks(
    edge: httpx.AsyncClient, audit_admin_dsn: str
) -> None:
    mine = (await call(edge, send_message())).json()["result"]["task"]

    alice = await call(edge, list_tasks())
    bob = await call(edge, list_tasks(), token="bob-token")

    assert [task["id"] for task in alice.json()["result"]["tasks"]] == [mine["id"]]
    assert bob.json()["result"]["tasks"] == []
    assert [(row[0], row[2], row[3]) for row in await audit_rows(audit_admin_dsn)] == [
        ("user:alice", "SendMessage", "allow"),
        ("user:alice", "ListTasks", "allow"),
        ("user:bob", "ListTasks", "allow"),
    ]


async def test_list_tasks_is_subject_to_the_call_registry(
    edge: httpx.AsyncClient, tasks: TaskService
) -> None:
    response = await call(edge, list_tasks(tenant="evaluator"))

    assert error_of(response)["code"] == CALL_DENIED
    assert tasks.received == []


async def test_unknown_agent_is_denied_audited_and_not_forwarded(
    edge: httpx.AsyncClient, tasks: TaskService, audit_admin_dsn: str
) -> None:
    response = await call(edge, send_message(tenant="ghost"))

    assert response.status_code == 200
    error = error_of(response)
    assert error["code"] == CALL_DENIED
    assert "unknown_agent" in error["message"]
    assert tasks.received == []
    assert await audit_rows(audit_admin_dsn) == [
        (
            "user:alice",
            "agent:ghost",
            "SendMessage",
            "deny: unknown_agent: agent 'ghost' is not registered",
            "10.0.0.7",
        )
    ]


async def test_caller_not_in_the_registry_entry_is_denied(
    edge: httpx.AsyncClient, tasks: TaskService, audit_admin_dsn: str
) -> None:
    response = await call(edge, send_message(tenant="evaluator"), token="ci-token")

    assert error_of(response)["code"] == CALL_DENIED
    assert "not_allowed" in error_of(response)["message"]
    assert tasks.received == []
    [row] = await audit_rows(audit_admin_dsn)
    assert row[0] == "service:ci"
    assert row[3].startswith("deny: not_allowed")


@pytest.mark.parametrize(
    ("body", "code"),
    [
        (send_message(method="ListTaskPushNotificationConfigs"), -32601),
        (send_message(method="SendStreamingMessage"), -32601),
        (send_message(tenant=None), -32602),
        (send_message(tenant=""), -32602),
        ({"jsonrpc": "2.0", "id": 1, "method": "GetTask", "params": "x"}, -32602),
        ({"jsonrpc": "1.0", "id": 1, "method": "GetTask", "params": {}}, -32600),
        ([send_message()], -32600),
    ],
    ids=[
        "list-tasks",
        "streaming",
        "no-tenant",
        "empty-tenant",
        "params-not-object",
        "wrong-jsonrpc-version",
        "batch",
    ],
)
async def test_malformed_or_unsupported_requests_are_refused_and_audited(
    edge: httpx.AsyncClient,
    tasks: TaskService,
    audit_admin_dsn: str,
    body: Any,
    code: int,
) -> None:
    response = await call(edge, body)

    assert response.status_code == 200
    assert error_of(response)["code"] == code
    assert tasks.received == []
    [row] = await audit_rows(audit_admin_dsn)
    assert row[3].startswith("deny: ")


async def test_body_that_is_not_json_is_a_parse_error(
    edge: httpx.AsyncClient, tasks: TaskService, audit_admin_dsn: str
) -> None:
    response = await edge.post(
        "/a2a", content=b"{not json", headers={"Authorization": "Bearer alice-token"}
    )

    assert error_of(response)["code"] == -32700
    assert response.json()["id"] is None
    assert tasks.received == []
    assert len(await audit_rows(audit_admin_dsn)) == 1


@pytest.mark.parametrize(
    "authorization",
    [None, "Bearer", "Bearer ", "Basic YWxpY2U6c2VjcmV0", "bearer-ish alice-token", "Bearer bad"],
)
async def test_unauthenticated_requests_get_401_and_are_not_audited(
    edge: httpx.AsyncClient,
    tasks: TaskService,
    audit_admin_dsn: str,
    authorization: str | None,
) -> None:
    # Only authenticated decisions are audited: an anonymous flood must not be able to
    # grow the insert-only log, and there is no account to attribute the row to.
    headers = {} if authorization is None else {"Authorization": authorization}

    response = await edge.post("/a2a", json=send_message(), headers=headers)

    assert response.status_code == 401
    assert response.headers["www-authenticate"].startswith("Bearer")
    assert error_of(response)["code"] == UNAUTHENTICATED
    assert tasks.received == []
    assert await audit_rows(audit_admin_dsn) == []


async def test_bearer_scheme_is_case_insensitive(edge: httpx.AsyncClient) -> None:
    response = await edge.post(
        "/a2a",
        json=send_message(),
        headers={"Authorization": "bearer alice-token", "A2A-Version": "1.0"},
    )

    assert "result" in response.json()


async def test_unreachable_audit_log_refuses_and_does_not_forward(tasks: TaskService) -> None:
    async with edge_client(tasks, UNREACHABLE_DSN) as client:
        response = await call(client, send_message())

    assert error_of(response)["code"] == AUDIT_UNAVAILABLE
    assert tasks.received == []
    assert tasks.orchestrator.started == []


async def test_unreachable_audit_log_still_refuses_a_denied_call(tasks: TaskService) -> None:
    async with edge_client(tasks, UNREACHABLE_DSN) as client:
        response = await call(client, send_message(tenant="ghost"))

    assert error_of(response)["code"] == AUDIT_UNAVAILABLE
    assert tasks.received == []


async def test_unreachable_task_service_is_an_internal_error(
    audit_dsn: str, audit_admin_dsn: str
) -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    edge = create_edge_app(
        authenticate=authenticate,
        registry=REGISTRY,
        limits=ChainLimits(max_depth=3),
        audit_dsn=audit_dsn,
        forward=httpx.AsyncClient(transport=httpx.MockTransport(refuse), base_url="http://tasks"),
        edge_token=EDGE_TOKEN,
        cards={},
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=edge), base_url="http://edge"
    ) as client:
        response = await call(client, send_message())

    assert error_of(response)["code"] == -32603
    assert response.json()["id"] == 7


async def test_public_card_is_served_without_authentication(edge: httpx.AsyncClient) -> None:
    response = await edge.get("/agents/discovery/.well-known/agent-card.json")

    assert response.status_code == 200
    card = response.json()
    assert card["name"] == "discovery"
    assert card["supportedInterfaces"][0]["tenant"] == "discovery"


async def test_unknown_card_is_404(edge: httpx.AsyncClient) -> None:
    response = await edge.get("/agents/ghost/.well-known/agent-card.json")

    assert response.status_code == 404


# --- Rate limits (ADR 0012) ----------------------------------------------------------------------


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def rate_limited(response: httpx.Response) -> bool:
    return (
        response.status_code == 429
        and int(response.headers["retry-after"]) >= 1
        and error_of(response)["code"] == RATE_LIMITED
    )


async def test_a_caller_over_its_rate_gets_429_and_one_audit_row_per_refusal_streak(
    tasks: TaskService, audit_dsn: str, audit_admin_dsn: str
) -> None:
    clock = Clock()
    callers = Limiter(Rate(per_minute=60, burst=2), clock=clock)

    async with edge_client(tasks, audit_dsn, callers=callers) as client:
        allowed = [await call(client, send_message()) for _ in range(2)]
        refused = [await call(client, send_message()) for _ in range(3)]
        clock.now += 1
        again = await call(client, send_message())
        refused_again = await call(client, send_message())

    assert all(r.status_code == 200 for r in allowed)
    assert all(rate_limited(r) for r in refused)
    assert refused[0].json()["id"] == 7
    assert refused[0].headers["retry-after"] == "1"
    assert again.status_code == 200
    assert rate_limited(refused_again)
    assert len(tasks.received) == 3
    results = [row[3] for row in await audit_rows(audit_admin_dsn)]
    assert results == ["allow", "allow", "deny: rate_limited", "allow", "deny: rate_limited"]


async def test_one_callers_rate_does_not_limit_another(
    tasks: TaskService, audit_dsn: str, audit_admin_dsn: str
) -> None:
    callers = Limiter(Rate(per_minute=60, burst=1), clock=Clock())

    async with edge_client(tasks, audit_dsn, callers=callers) as client:
        await call(client, send_message())
        alice = await call(client, send_message())
        bob = await call(client, send_message(), token="bob-token")

    assert rate_limited(alice)
    assert bob.status_code == 200


async def test_failed_authentications_are_limited_per_address_before_verifying(
    tasks: TaskService, audit_dsn: str, audit_admin_dsn: str
) -> None:
    verified: list[str] = []

    def counting(token: str) -> Principal | AuthFailure:
        verified.append(token)
        return authenticate(token)

    failures = Limiter(Rate(per_minute=60, burst=2), clock=Clock())
    edge = create_edge_app(
        authenticate=counting,
        registry=REGISTRY,
        limits=ChainLimits(max_depth=3),
        audit_dsn=audit_dsn,
        forward=httpx.AsyncClient(
            transport=httpx.ASGITransport(app=tasks.app()), base_url="http://tasks"
        ),
        edge_token=EDGE_TOKEN,
        cards={},
        auth_failures=failures,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=edge, client=("203.0.113.5", 4000)),
        base_url="http://edge",
    ) as client:
        for _ in range(3):
            assert (await call(client, send_message())).status_code == 200
        unauthenticated = [await call(client, send_message(), token="bad") for _ in range(2)]
        flooded = await call(client, send_message(), token="bad")
        even_valid = await call(client, send_message())
        no_token = await call(client, send_message(), token=None)

    assert [r.status_code for r in unauthenticated] == [401, 401]
    assert rate_limited(flooded) and rate_limited(even_valid) and rate_limited(no_token)
    assert flooded.json()["id"] is None
    # Three successful calls cost nothing; the refused ones never reached verification.
    assert verified == ["alice-token"] * 3 + ["bad"] * 2
    assert [row[3] for row in await audit_rows(audit_admin_dsn)] == ["allow"] * 3


def proxied_edge(tasks: TaskService, audit_dsn: str, **rate_limits: Any) -> httpx.AsyncClient:
    edge = create_edge_app(
        authenticate=authenticate,
        registry=REGISTRY,
        limits=ChainLimits(max_depth=3),
        audit_dsn=audit_dsn,
        forward=httpx.AsyncClient(
            transport=httpx.ASGITransport(app=tasks.app()), base_url="http://tasks"
        ),
        edge_token=EDGE_TOKEN,
        cards={},
        trusted_proxies=parse_networks("10.0.0.0/8"),
        **rate_limits,
    )
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=edge, client=("10.0.0.7", 51234)),
        base_url="http://edge",
    )


async def test_behind_a_trusted_proxy_the_forwarded_client_is_limited_and_audited(
    tasks: TaskService, audit_dsn: str, audit_admin_dsn: str
) -> None:
    failures = Limiter(Rate(per_minute=60, burst=1), clock=Clock())

    async with proxied_edge(tasks, audit_dsn, auth_failures=failures) as client:
        first = {"X-Forwarded-For": "203.0.113.5"}
        second = {"X-Forwarded-For": "203.0.113.6"}
        await call(client, send_message(), token="bad", extra_headers=first)
        limited = await call(client, send_message(), token="bad", extra_headers=first)
        other = await call(client, send_message(), token="bad", extra_headers=second)
        allowed = await call(
            client, send_message(), extra_headers={"X-Forwarded-For": "2001:db8::7"}
        )

    assert rate_limited(limited)
    assert other.status_code == 401
    assert allowed.status_code == 200
    [row] = await audit_rows(audit_admin_dsn)
    assert row[4] == "2001:db8::7"


async def test_a_forwarded_for_from_an_untrusted_peer_does_not_escape_the_limit(
    tasks: TaskService, audit_dsn: str, audit_admin_dsn: str
) -> None:
    failures = Limiter(Rate(per_minute=60, burst=1), clock=Clock())

    async with edge_client(tasks, audit_dsn, auth_failures=failures) as client:
        await call(
            client, send_message(), token="bad", extra_headers={"X-Forwarded-For": "1.1.1.1"}
        )
        spoofed = await call(
            client, send_message(), token="bad", extra_headers={"X-Forwarded-For": "2.2.2.2"}
        )
        allowed = await call(client, send_message(), extra_headers={"X-Forwarded-For": "2.2.2.2"})

    assert rate_limited(spoofed)
    assert rate_limited(allowed)


# --- Metrics (ADR 0013) --------------------------------------------------------------------------


def sample(metrics: Metrics, name: str, **labels: str) -> float:
    return metrics.registry.get_sample_value(name, labels) or 0.0


async def test_every_edge_request_is_counted_under_its_route_template(
    tasks: TaskService, audit_dsn: str, audit_admin_dsn: str
) -> None:
    metrics = Metrics("edge")

    async with edge_client(tasks, audit_dsn, metrics=metrics) as client:
        await call(client, send_message())
        for name in ("discovery", "no-such-agent", "another-made-up-name"):
            await client.get(f"/agents/{name}/.well-known/agent-card.json")
        await client.get("/wp-login.php")

    card = "/agents/{name}/.well-known/agent-card.json"
    requests = "golem_http_requests_total"
    assert (
        sample(metrics, requests, process="edge", route="/a2a", method="POST", status_class="2xx")
        == 1
    )
    assert (
        sample(metrics, requests, process="edge", route=card, method="GET", status_class="2xx") == 1
    )
    assert (
        sample(metrics, requests, process="edge", route=card, method="GET", status_class="4xx") == 2
    )
    assert (
        sample(
            metrics, requests, process="edge", route="unmatched", method="GET", status_class="4xx"
        )
        == 1
    )


async def test_edge_refusals_are_counted_by_kind(
    tasks: TaskService, audit_dsn: str, audit_admin_dsn: str
) -> None:
    metrics = Metrics("edge")
    callers = Limiter(Rate(per_minute=60, burst=1), clock=Clock())
    failures = Limiter(Rate(per_minute=60, burst=1), clock=Clock())

    async with edge_client(
        tasks, audit_dsn, metrics=metrics, callers=callers, auth_failures=failures
    ) as client:
        await call(client, send_message("evaluator"))
        await call(client, send_message())
        await call(client, send_message(), token="forged")
        await call(client, send_message(), token="forged")

    assert sample(metrics, "golem_policy_denials_total", reason="not_allowed") == 1
    assert sample(metrics, "golem_rate_limit_refusals_total", process="edge", limit="caller") == 1
    assert sample(metrics, "golem_authentication_failures_total", process="edge") == 1
    assert (
        sample(metrics, "golem_rate_limit_refusals_total", process="edge", limit="auth_failures")
        == 1
    )


async def test_an_audit_write_failure_is_counted(tasks: TaskService) -> None:
    metrics = Metrics("edge")

    async with edge_client(tasks, UNREACHABLE_DSN, metrics=metrics) as client:
        await call(client, send_message())

    assert sample(metrics, "golem_audit_write_failures_total", process="edge") == 1


# Delegation: an agent holding a call token (ADR 0014)


async def audit_chains(admin_dsn: str) -> list[tuple[str, str, list[str]]]:
    async with await psycopg.AsyncConnection.connect(admin_dsn) as conn:
        cursor = await conn.execute("SELECT account, result, chain FROM audit_log ORDER BY id")
        return await cursor.fetchall()


async def test_a_delegated_call_is_forwarded_for_the_subject_with_its_chain(
    edge: httpx.AsyncClient, tasks: TaskService, audit_admin_dsn: str
) -> None:
    response = await call(edge, send_message(), token="discovery-for-alice")

    assert response.json()["result"]["task"]["status"]["state"] == "TASK_STATE_WORKING"
    [headers] = tasks.received
    assert headers["x-golem-principal"] == "user:alice"
    assert headers["x-golem-chain"] == "discovery"
    assert headers["x-golem-root-run"] == "root-1"
    assert await audit_chains(audit_admin_dsn) == [("user:alice", "allow", ["discovery"])]


async def test_chain_headers_from_a_client_never_reach_the_task_service(
    edge: httpx.AsyncClient, tasks: TaskService
) -> None:
    await call(
        edge,
        send_message(),
        extra_headers={"X-Golem-Chain": "discovery", "X-Golem-Root-Run": "someone-elses-run"},
    )
    await call(
        edge,
        send_message(),
        token="discovery-for-alice",
        extra_headers={"X-Golem-Chain": "forged", "X-Golem-Root-Run": "forged"},
    )

    human, agent = tasks.received
    assert "x-golem-chain" not in human
    assert "x-golem-root-run" not in human
    assert (agent["x-golem-chain"], agent["x-golem-root-run"]) == ("discovery", "root-1")


async def test_the_chain_policy_bites_on_a_delegated_call(
    edge: httpx.AsyncClient, tasks: TaskService, audit_admin_dsn: str
) -> None:
    cycle = await call(edge, send_message(tenant="discovery"), token="discovery-for-alice")
    not_allowed = await call(edge, send_message(tenant="evaluator"), token="discovery-for-alice")

    assert error_of(cycle)["code"] == CALL_DENIED
    assert error_of(cycle)["message"].startswith("cycle:")
    assert error_of(not_allowed)["message"].startswith("not_allowed:")
    assert tasks.received == []
    assert [row[2] for row in await audit_chains(audit_admin_dsn)] == [["discovery"]] * 2


@pytest.mark.parametrize("method", ["GetTask", "ListTasks", "CancelTask"])
async def test_an_agent_may_only_start_a_task_never_read_or_cancel_the_subjects(
    edge: httpx.AsyncClient, tasks: TaskService, audit_admin_dsn: str, method: str
) -> None:
    params = {"tenant": "reviewer", "id": "t"}
    body = {"jsonrpc": "2.0", "id": 3, "method": method, "params": params}

    response = await call(edge, body, token="discovery-for-alice")

    assert error_of(response)["code"] == CALL_DENIED
    assert tasks.received == []
    [(account, result, _)] = await audit_chains(audit_admin_dsn)
    assert (account, result.startswith("deny: ")) == ("user:alice", True)


async def test_an_agent_may_not_send_into_an_existing_task(
    edge: httpx.AsyncClient, tasks: TaskService
) -> None:
    body = send_message()
    body["params"]["message"]["taskId"] = "a-task-of-alice"

    response = await call(edge, body, token="discovery-for-alice")

    assert error_of(response)["code"] == CALL_DENIED
    assert tasks.received == []


async def test_the_rate_limit_counts_the_acting_agent_across_subjects(
    tasks: TaskService, audit_dsn: str, audit_admin_dsn: str
) -> None:
    callers = Limiter(Rate(per_minute=60, burst=1), clock=Clock())

    async with edge_client(tasks, audit_dsn, callers=callers) as client:
        first = await call(client, send_message(), token="discovery-for-alice")
        second = await call(client, send_message(), token="discovery-for-bob")
        alice = await call(client, send_message())

    assert first.status_code == 200
    assert rate_limited(second)
    assert alice.status_code == 200


# Revocation: a call token is only good while its issuing run is running (ASVS 10.4.9)


def unauthorized_as_revoked(response: httpx.Response) -> bool:
    return (
        response.status_code == 401
        and response.headers["www-authenticate"] == 'Bearer error="invalid_token"'
        and error_of(response)["message"].startswith("run_not_active:")
    )


@pytest.mark.parametrize("status", ["canceled", "succeeded", "failed"])
async def test_a_call_token_of_a_run_that_stopped_is_refused_and_audited(
    edge: httpx.AsyncClient, tasks: TaskService, audit_admin_dsn: str, status: str
) -> None:
    tasks.orchestrator.statuses["run-a"] = status

    response = await call(edge, send_message(), token="discovery-for-alice")

    assert unauthorized_as_revoked(response)
    assert status in error_of(response)["message"]
    assert tasks.received == []
    [(account, result, chain)] = await audit_chains(audit_admin_dsn)
    assert (account, chain) == ("user:alice", ["discovery"])
    assert result.startswith("deny: run_not_active")


async def test_a_call_token_of_an_unknown_run_is_refused(
    edge: httpx.AsyncClient, tasks: TaskService
) -> None:
    del tasks.orchestrator.statuses["run-a"]

    assert unauthorized_as_revoked(await call(edge, send_message(), token="discovery-for-alice"))


async def test_without_a_run_status_the_edge_fails_closed(
    edge: httpx.AsyncClient, tasks: TaskService, audit_admin_dsn: str
) -> None:
    tasks.orchestrator.broken = True

    response = await call(edge, send_message(), token="discovery-for-alice")

    assert response.status_code == 503
    assert error_of(response)["message"].startswith("run status unavailable")
    assert tasks.received == []
    [(_, result, _)] = await audit_chains(audit_admin_dsn)
    assert result.startswith("deny: run status unavailable")


async def test_an_edge_that_cannot_ask_for_run_statuses_refuses_every_call_token(
    tasks: TaskService, audit_dsn: str, audit_admin_dsn: str
) -> None:
    async with edge_client(tasks, audit_dsn, run_statuses=None) as client:
        agent = await call(client, send_message(), token="discovery-for-alice")
        person = await call(client, send_message())

    assert agent.status_code == 503
    assert person.status_code == 200


async def test_a_running_run_is_asked_about_once_per_ttl(
    tasks: TaskService, audit_dsn: str, audit_admin_dsn: str
) -> None:
    clock = Clock()

    async with edge_client(tasks, audit_dsn, run_statuses=tasks.statuses(clock)) as client:
        first = await call(client, send_message(), token="discovery-for-alice")
        tasks.orchestrator.statuses["run-a"] = "canceled"
        clock.now += 9
        cached = await call(client, send_message(), token="discovery-for-alice")
        clock.now += 1
        revoked = await call(client, send_message(), token="discovery-for-alice")

    assert first.status_code == cached.status_code == 200
    assert unauthorized_as_revoked(revoked)
    assert tasks.orchestrator.asked == ["run-a", "run-a"]


async def test_people_and_services_are_never_asked_about(
    edge: httpx.AsyncClient, tasks: TaskService
) -> None:
    await call(edge, send_message())
    await call(edge, send_message(), token="ci-token")

    assert tasks.orchestrator.asked == []
