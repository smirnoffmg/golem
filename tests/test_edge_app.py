from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import httpx
import psycopg
import pytest
from a2a.types.a2a_pb2 import AgentCapabilities, AgentCard, AgentInterface
from starlette.types import ASGIApp, Receive, Scope, Send
from test_tasks_service import FakeOrchestrator, make_card

from golem.edge.app import AUDIT_UNAVAILABLE, CALL_DENIED, UNAUTHENTICATED, create_edge_app
from golem.edge.auth import AuthFailure, Principal
from golem.edge.policy import ChainLimits, Registry
from golem.tasks.app import create_app

UNREACHABLE_DSN = "host=127.0.0.1 port=1 dbname=golem_audit user=golem_edge connect_timeout=1"

TOKENS = {
    "alice-token": Principal(name="user:alice", chain=()),
    "ci-token": Principal(name="service:ci", chain=()),
}

REGISTRY = Registry(
    allowed_callers={
        "reviewer": frozenset({"user:*"}),
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
class TaskService:
    """The real in-process task service, with the headers of every request it received."""

    orchestrator: FakeOrchestrator = field(default_factory=FakeOrchestrator)
    received: list[dict[str, str]] = field(default_factory=list)

    def app(self) -> ASGIApp:
        inner = create_app(make_card(), self.orchestrator)

        async def recording(scope: Scope, receive: Receive, send: Send) -> None:
            if scope["type"] == "http":
                self.received.append({k.decode().lower(): v.decode() for k, v in scope["headers"]})
            await inner(scope, receive, send)

        return recording


@pytest.fixture
def tasks() -> TaskService:
    return TaskService()


def edge_client(tasks: TaskService, audit_dsn: str) -> httpx.AsyncClient:
    forward = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=tasks.app()), base_url="http://tasks"
    )
    edge = create_edge_app(
        authenticate=authenticate,
        registry=REGISTRY,
        limits=ChainLimits(max_depth=3),
        audit_dsn=audit_dsn,
        forward=forward,
        cards={"discovery": discovery_card()},
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
        (send_message(method="ListTasks"), -32601),
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
