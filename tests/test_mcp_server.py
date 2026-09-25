"""A platform MCP server end to end: the runtime's own MCP client with a real run token, the real
task service on golem_runs for keys and run status, the real audit log, and Jira faked at its
HTTP boundary (shapes as in test_mcp_atlassian)."""

import hashlib
import socket
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from decimal import Decimal
from functools import partial
from typing import Any

import httpx
import psycopg
import pytest
import uvicorn
from starlette.types import ASGIApp
from test_tasks_service import read_listener
from test_tasks_to_runs import CATALOG, TEMPLATE, FakeLauncher

from golem.adapters.jira import jira_authorization
from golem.catalog import Role
from golem.jwks import SigningKeys, fetch_jwks
from golem.mcp.atlassian import JiraDeployment
from golem.mcp.auth import RunStatuses, run_token_verifier
from golem.mcp.gate import Gate
from golem.mcp.groups import GROUPS
from golem.mcp.server import create_mcp_app
from golem.metrics import Metrics
from golem.orchestrator.admission import Limits
from golem.orchestrator.runs import RunCreated, StartRequest, cancel_run_of_task, start_run
from golem.orchestrator.service import PostgresOrchestrator
from golem.ratelimit import Limiter, Rate
from golem.run_token import RunClaims, SigningKey, issue
from golem.runtime.tools import (
    McpToolbox,
    Registry,
    ToolGroup,
    ToolLimits,
    ToolLoadError,
    describe,
    load_group,
)

KEY = SigningKey.generate(kid="run-key-1")
JIRA = "https://jira.example.test"
SERVICE_PAT = "jira-service-pat"
TRACKER = GROUPS["tracker.read"]
LIMITS = Limits(max_runs_per_caller=10, max_runs_per_root=10, budget_per_root=Decimal("100"))


def free_socket() -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    return sock


@contextmanager
def serve(app: ASGIApp) -> Iterator[str]:
    sock = free_socket()
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning", lifespan="on"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("test server did not start")
        time.sleep(0.02)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)


@dataclass
class Jira:
    requests: list[httpx.Request] = field(default_factory=list)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path == "/rest/api/2/issue/DISC-404":
            return httpx.Response(
                404, json={"errorMessages": ["Issue does not exist"], "errors": {}}
            )
        if request.url.path.startswith("/rest/api/2/issue/"):
            key = request.url.path.rsplit("/", 1)[-1]
            return httpx.Response(200, json=jira_issue(key))
        if request.url.path == "/rest/api/2/search/jql":
            return httpx.Response(200, json={"issues": [jira_issue("DISC-1")], "isLast": True})
        return httpx.Response(404, json={"errorMessages": ["no such resource"]})


def jira_issue(key: str) -> dict[str, Any]:
    return {
        "id": "10001",
        "key": key,
        "fields": {
            "summary": "Onboarding takes two weeks",
            "status": {"name": "In Progress"},
            "issuetype": {"name": "Story"},
            "assignee": {"displayName": "Ann Lee"},
            "description": "Interviews say so.",
        },
    }


@dataclass(frozen=True)
class Stack:
    tasks_url: str
    mcp_url: str
    runs_dsn: str
    jira: Jira


@pytest.fixture
def jira() -> Jira:
    return Jira()


@pytest.fixture
def tasks_url(runs_db: str) -> Iterator[str]:
    orchestrator = PostgresOrchestrator(
        dsn=runs_db,
        limits=LIMITS,
        estimated_cost=Decimal("1"),
        launcher=FakeLauncher(),
        template=TEMPLATE,
        catalogs={"discovery": CATALOG},
        signing_key=KEY,
        grants={"discovery": ("tracker.read",)},
    )
    with serve(read_listener(orchestrator, (KEY,))) as url:
        yield url


def mcp_app(tasks_url: str, audit_dsn: str, jira: Jira, **gate_options: Any) -> ASGIApp:
    keys = SigningKeys(
        partial(fetch_jwks, httpx.Client(timeout=2), f"{tasks_url}/internal/run-keys")
    )
    keys.refresh()
    gate = Gate(
        group=TRACKER,
        target_system="jira:jira.example.test",
        verify=run_token_verifier(keys),
        # No caching here, so a cancel is seen on the next call; the TTL is tested on its own.
        statuses=RunStatuses(httpx.AsyncClient(base_url=tasks_url, timeout=2), ttl_seconds=0),
        audit_dsn=audit_dsn,
        **gate_options,
    )
    upstream = httpx.AsyncClient(
        transport=httpx.MockTransport(jira),
        base_url=JIRA,
        headers={"Authorization": jira_authorization(user=None, token=SERVICE_PAT)},
    )
    return create_mcp_app(gate=gate, upstream=upstream, jira_deployment=JiraDeployment.CLOUD)


@pytest.fixture
def stack(
    tasks_url: str, runs_db: str, mcp_audit_dsn: str, audit_admin_dsn: str, jira: Jira
) -> Iterator[Stack]:
    with serve(mcp_app(tasks_url, mcp_audit_dsn, jira)) as url:
        yield Stack(tasks_url=tasks_url, mcp_url=f"{url}/mcp", runs_dsn=runs_db, jira=jira)


async def started_run(dsn: str, message_id: str = "m-1") -> RunCreated:
    request = StartRequest(
        caller="user:alice",
        message_id=message_id,
        task_id=f"task-{message_id}",
        agent="discovery",
        estimated_cost=Decimal("1"),
    )
    async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as conn:
        created = await start_run(conn, request, LIMITS)
    assert isinstance(created, RunCreated)
    return created


async def cancel(dsn: str, message_id: str = "m-1") -> None:
    async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as conn:
        await cancel_run_of_task(conn, f"task-{message_id}")


def token_for(run: RunCreated, **overrides: Any) -> str:
    now = int(time.time())
    claims = RunClaims(
        run_id=run.run_id,
        agent="discovery",
        caller="user:alice",
        root_run_id=run.root_run_id,
        tools=("tracker.read",),
        expires_at=now + 600,
    )
    return issue(replace(claims, **overrides), KEY, now)


def registry(stack: Stack) -> Registry:
    return Registry(groups=(ToolGroup(name=TRACKER.name, url=stack.mcp_url, tools=TRACKER.tools),))


def researcher() -> Role:
    return Role(name="researcher", writes="hypotheses/", tools=("tracker.read",))


async def tools_of(stack: Stack, token: str) -> dict[str, Any]:
    return await tools_of_url(stack.mcp_url, token)


async def tools_of_url(mcp_url: str, token: str) -> dict[str, Any]:
    group = ToolGroup(name=TRACKER.name, url=mcp_url, tools=TRACKER.tools)
    toolbox = McpToolbox(registry=Registry(groups=(group,)), run_token=token)
    return {tool.name: tool for tool in await toolbox.tools_for(researcher())}


async def audit_rows(dsn: str) -> list[dict[str, Any]]:
    async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as conn:
        cursor = await conn.execute(
            "SELECT account, request, target_system, operation, result, source, chain"
            " FROM audit_log ORDER BY id"
        )
        columns = [c.name for c in cursor.description or []]
        return [dict(zip(columns, row, strict=True)) for row in await cursor.fetchall()]


def text_of(reply: list[dict[str, Any]]) -> str:
    return "".join(block["text"] for block in reply if block.get("type") == "text")


def digest(token: str) -> str:
    return "sha256:" + hashlib.sha256(token.encode()).hexdigest()[:16]


# Allowed calls


async def test_a_role_loads_exactly_the_groups_tools_and_a_call_reaches_jira(
    stack: Stack, audit_admin_dsn: str
) -> None:
    run = await started_run(stack.runs_dsn)
    token = token_for(run)

    tools = await tools_of(stack, token)
    reply = await tools["get_issue"].ainvoke({"key": "DISC-1"})

    assert sorted(tools) == ["get_issue", "search_issues"]
    assert "DISC-1: Onboarding takes two weeks" in text_of(reply)
    [request] = stack.jira.requests
    assert request.url.path == "/rest/api/2/issue/DISC-1"
    rows = await audit_rows(audit_admin_dsn)
    [call] = [row for row in rows if row["operation"] == "get_issue"]
    assert call == {
        "account": "user:alice",
        "request": f"tools/call get_issue key='DISC-1' token={digest(token)}",
        "target_system": "jira:jira.example.test",
        "operation": "get_issue",
        "result": "allow",
        "source": "mcp:tracker.read",
        "chain": [run.root_run_id, run.run_id],
    }
    assert {row["operation"] for row in rows} >= {"initialize", "tools/list", "get_issue"}
    assert all(row["result"] == "allow" for row in rows)


async def test_the_run_token_never_reaches_jira(stack: Stack, audit_admin_dsn: str) -> None:
    token = token_for(await started_run(stack.runs_dsn))
    tools = await tools_of(stack, token)

    await tools["search_issues"].ainvoke({"jql": "project = DISC", "limit": 5})
    await tools["get_issue"].ainvoke({"key": "DISC-1"})

    assert len(stack.jira.requests) == 2
    for request in stack.jira.requests:
        assert request.headers["authorization"] == f"Bearer {SERVICE_PAT}"
        assert token not in str(request.url)
        assert all(token not in value for value in request.headers.values())
    for row in await audit_rows(audit_admin_dsn):
        assert token not in row["request"]


async def test_an_upstream_error_is_a_tool_error_not_a_crash(stack: Stack) -> None:
    tools = await tools_of(stack, token_for(await started_run(stack.runs_dsn)))

    reply = await tools["get_issue"].ainvoke(
        {"type": "tool_call", "name": "get_issue", "args": {"key": "DISC-404"}, "id": "c1"}
    )

    assert reply.status == "error"
    assert "404" in reply.text
    assert "Issue does not exist" in reply.text


async def test_argument_values_in_the_audit_are_bounded(stack: Stack, audit_admin_dsn: str) -> None:
    tools = await tools_of(stack, token_for(await started_run(stack.runs_dsn)))

    await tools["search_issues"].ainvoke({"jql": "text ~ " + "a" * 5_000, "limit": 3})

    [row] = [r for r in await audit_rows(audit_admin_dsn) if r["operation"] == "search_issues"]
    assert row["request"].startswith("tools/call search_issues jql='text ~ aaa")
    assert "limit=3" in row["request"]
    assert len(row["request"]) <= 400


# Refused calls


async def refused(stack: Stack, token: str) -> str:
    with pytest.raises(ToolLoadError) as caught:
        await load_group(registry(stack).groups[0], token, ToolLimits(call_timeout=10))
    return str(caught.value)


async def test_an_expired_token_is_refused_and_audited(stack: Stack, audit_admin_dsn: str) -> None:
    token = token_for(await started_run(stack.runs_dsn), expires_at=int(time.time()) - 1)

    message = await refused(stack, token)

    assert "401" in message
    assert stack.jira.requests == []
    rows = await audit_rows(audit_admin_dsn)
    assert rows
    assert all(row["account"] == "unauthenticated" for row in rows)
    assert all(row["result"] == "deny: token expired" for row in rows)
    assert all(row["chain"] == [] for row in rows)
    assert all(digest(token) in row["request"] for row in rows)


async def test_a_token_for_an_agent_without_the_group_is_refused_and_audited(
    stack: Stack, audit_admin_dsn: str
) -> None:
    run = await started_run(stack.runs_dsn)
    token = token_for(run, agent="reviewer", tools=("wiki.read",))

    message = await refused(stack, token)

    assert "403" in message
    rows = await audit_rows(audit_admin_dsn)
    assert rows
    for row in rows:
        assert row["account"] == "user:alice"
        assert row["result"] == "deny: the run token does not grant tool group 'tracker.read'"
        assert row["chain"] == [run.root_run_id, run.run_id]


async def test_a_canceled_run_is_refused_and_audited(stack: Stack, audit_admin_dsn: str) -> None:
    run = await started_run(stack.runs_dsn)
    token = token_for(run)
    tools = await tools_of(stack, token)
    await cancel(stack.runs_dsn)

    with pytest.raises(Exception) as caught:
        await tools["get_issue"].ainvoke({"key": "DISC-1"})

    assert "401" in describe(caught.value)

    assert stack.jira.requests == []
    denied = [r for r in await audit_rows(audit_admin_dsn) if r["result"] != "allow"]
    assert denied
    assert {r["result"] for r in denied} == {"deny: the run is canceled"}
    assert await refused(stack, token)


async def test_a_token_for_a_run_golem_does_not_know_is_refused(stack: Stack) -> None:
    unknown = RunCreated(
        run_id="00000000-0000-0000-0000-000000000001",
        root_run_id="00000000-0000-0000-0000-000000000001",
    )

    assert "401" in await refused(stack, token_for(unknown))


async def mcp_post(url: str, message: dict[str, Any], token: str | None) -> httpx.Response:
    headers = {"Accept": "application/json, text/event-stream"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    async with httpx.AsyncClient(timeout=10) as client:
        return await client.post(url, json=message, headers=headers)


LIST_TOOLS = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}


async def test_a_request_without_a_token_gets_a_bearer_challenge(
    stack: Stack, audit_admin_dsn: str
) -> None:
    response = await mcp_post(stack.mcp_url, LIST_TOOLS, token=None)

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == 'Bearer realm="golem-mcp"'
    [row] = await audit_rows(audit_admin_dsn)
    assert row["result"] == "deny: bearer token required"
    assert row["request"] == "tools/list token=none"


async def test_a_forged_token_is_refused_as_invalid(stack: Stack) -> None:
    forged = issue(
        RunClaims("run-x", "discovery", "user:mallory", "run-x", ("tracker.read",), 2**31),
        SigningKey.generate(kid="run-key-1"),
        int(time.time()),
    )

    response = await mcp_post(stack.mcp_url, LIST_TOOLS, token=forged)

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == 'Bearer error="invalid_token"'


async def test_a_tool_outside_the_group_is_refused_before_the_server_sees_it(
    stack: Stack, audit_admin_dsn: str
) -> None:
    token = token_for(await started_run(stack.runs_dsn))
    call = {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/call",
        "params": {"name": "delete_issue", "arguments": {"key": "DISC-1"}},
    }

    response = await mcp_post(stack.mcp_url, call, token=token)

    assert response.status_code == 403
    [row] = await audit_rows(audit_admin_dsn)
    assert row["operation"] == "delete_issue"
    assert row["result"] == "deny: tool 'delete_issue' is not in tool group 'tracker.read'"


async def test_a_message_that_is_not_json_rpc_is_refused(stack: Stack) -> None:
    token = token_for(await started_run(stack.runs_dsn))

    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.post(
            stack.mcp_url,
            content=b"[1, 2",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        )

    assert response.status_code == 400


# Failing closed on our own dependencies


async def test_without_the_audit_log_every_call_is_refused(
    tasks_url: str, runs_db: str, jira: Jira
) -> None:
    run = await started_run(runs_db)
    broken_dsn = "host=127.0.0.1 port=1 dbname=golem_audit user=golem_mcp connect_timeout=1"

    with serve(mcp_app(tasks_url, broken_dsn, jira)) as url:
        response = await mcp_post(f"{url}/mcp", LIST_TOOLS, token=token_for(run))

    assert response.status_code == 503
    assert "audit log unavailable" in response.text
    assert jira.requests == []


async def test_without_run_status_every_call_is_refused(
    runs_db: str, mcp_audit_dsn: str, audit_admin_dsn: str, jira: Jira
) -> None:
    run = await started_run(runs_db)
    orchestrator = PostgresOrchestrator(
        dsn="host=127.0.0.1 port=1 dbname=golem_runs connect_timeout=1",
        limits=LIMITS,
        estimated_cost=Decimal("1"),
        launcher=FakeLauncher(),
        template=TEMPLATE,
        catalogs={},
        signing_key=KEY,
        grants={},
    )

    with (
        serve(read_listener(orchestrator, (KEY,))) as broken_tasks,
        serve(mcp_app(broken_tasks, mcp_audit_dsn, jira)) as url,
    ):
        response = await mcp_post(f"{url}/mcp", LIST_TOOLS, token=token_for(run))

    assert response.status_code == 503
    assert "run status unavailable" in response.text
    [row] = await audit_rows(audit_admin_dsn)
    assert row["result"].startswith("deny: run status unavailable")


# Rate limits (ADR 0012)


class FrozenClock:
    def __call__(self) -> float:
        return 0.0


async def test_failed_authentications_are_limited_per_address_and_audited_once(
    tasks_url: str, runs_db: str, mcp_audit_dsn: str, audit_admin_dsn: str, jira: Jira
) -> None:
    run = await started_run(runs_db)
    failures = Limiter(Rate(per_minute=60, burst=2), clock=FrozenClock())
    app = mcp_app(tasks_url, mcp_audit_dsn, jira, auth_failures=failures)

    with serve(app) as url:
        allowed = [await mcp_post(f"{url}/mcp", LIST_TOOLS, token_for(run)) for _ in range(3)]
        refused = [await mcp_post(f"{url}/mcp", LIST_TOOLS, token=None) for _ in range(2)]
        flooded = [await mcp_post(f"{url}/mcp", LIST_TOOLS, token="forged") for _ in range(3)]

    assert [r.status_code for r in allowed] == [200] * 3
    assert [r.status_code for r in refused] == [401, 401]
    assert [r.status_code for r in flooded] == [429] * 3
    assert all(int(r.headers["retry-after"]) >= 1 for r in flooded)
    assert flooded[0].json()["error"] == "rate_limited"
    results = [row["result"] for row in await audit_rows(audit_admin_dsn)]
    assert results == ["allow"] * 3 + ["deny: bearer token required"] * 2 + ["deny: rate_limited"]


# Metrics (ADR 0013)


def tool_calls(metrics: Metrics) -> dict[tuple[str, str, str], float]:
    return {
        (s.labels["group"], s.labels["tool"], s.labels["decision"]): s.value
        for family in metrics.registry.collect()
        for s in family.samples
        if s.name == "golem_mcp_tool_calls_total"
    }


def tool_call(name: str) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/call",
        "params": {"name": name, "arguments": {"key": "DISC-1"}},
    }


async def test_tool_calls_are_counted_by_decision_with_unknown_tools_as_other(
    tasks_url: str, runs_db: str, mcp_audit_dsn: str, audit_admin_dsn: str, jira: Jira
) -> None:
    metrics = Metrics("mcp")
    token = token_for(await started_run(runs_db))
    app = mcp_app(tasks_url, mcp_audit_dsn, jira, metrics=metrics)

    with serve(app) as url:
        tools = await tools_of_url(f"{url}/mcp", token)
        await tools["get_issue"].ainvoke({"key": "DISC-1"})
        for name in ("delete_issue", "drop_everything", "x" * 200):
            await mcp_post(f"{url}/mcp", tool_call(name), token=token)
        await mcp_post(f"{url}/mcp", tool_call("get_issue"), token="forged")

    assert tool_calls(metrics) == {
        ("tracker.read", "get_issue", "allow"): 1,
        ("tracker.read", "other", "deny"): 3,
        ("tracker.read", "get_issue", "deny"): 1,
    }
    requests = {
        s.labels["route"]
        for family in metrics.registry.collect()
        for s in family.samples
        if s.name == "golem_http_requests_total"
    }
    assert requests == {"/mcp"}
    assert (
        metrics.registry.get_sample_value("golem_authentication_failures_total", {"process": "mcp"})
        == 1
    )


async def test_refusals_and_audit_failures_are_counted(
    tasks_url: str, runs_db: str, mcp_audit_dsn: str, audit_admin_dsn: str, jira: Jira
) -> None:
    broken_dsn = "host=127.0.0.1 port=1 dbname=golem_audit user=golem_mcp connect_timeout=1"
    metrics = Metrics("mcp")
    failures = Limiter(Rate(per_minute=60, burst=1), clock=FrozenClock())
    app = mcp_app(tasks_url, broken_dsn, jira, auth_failures=failures, metrics=metrics)

    with serve(app) as url:
        for _ in range(2):
            await mcp_post(f"{url}/mcp", LIST_TOOLS, token=None)

    value = metrics.registry.get_sample_value
    assert (
        value("golem_rate_limit_refusals_total", {"process": "mcp", "limit": "auth_failures"}) == 1
    )
    assert value("golem_audit_write_failures_total", {"process": "mcp"}) == 2
