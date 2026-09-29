"""The write servers end to end (ADR 0015): the task service's own client with a real proposal
token, a real write server (gate, tools, audit log), the task service's real route for a
proposal's state, and Confluence, Jira Service Management and Jira faked at their HTTP
boundary (shapes as in test_mcp_writes).

Then the whole way once more on golem_runs: a person accepts, the task service applies through
the write server, and the proposal ends applied."""

import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, replace
from decimal import Decimal
from functools import partial
from typing import Any

import httpx
import psycopg
import pytest
from psycopg.types.json import Jsonb
from starlette.testclient import TestClient
from starlette.types import ASGIApp, Receive, Scope, Send
from test_mcp_server import serve
from test_mcp_writes import COMMENT, ISSUE, PAGE, REPLY, Confluence, Desk, Jira
from test_tasks_proposals import as_person, start
from test_tasks_service import TEST_EDGE_TOKEN, FakeOrchestrator, make_card, read_listener
from test_tasks_to_runs import CATALOG, GRANTS, TEMPLATE, FakeLauncher

from golem.jwks import SigningKeys, fetch_jwks
from golem.mcp.atlassian import Deployment
from golem.mcp.auth import proposal_token_verifier, run_token_verifier
from golem.mcp.gate import Gate
from golem.mcp.groups import GROUPS
from golem.mcp.server import create_mcp_app
from golem.orchestrator.admission import Limits
from golem.orchestrator.service import PostgresOrchestrator
from golem.proposal_payload import payload_digest
from golem.proposal_status import ProposalStates
from golem.proposal_token import APPLY, PREVIEW, ProposalClaims, issue
from golem.run_status import RunStatuses
from golem.run_token import RunClaims, SigningKey
from golem.run_token import issue as issue_run_token
from golem.tasks.app import EDGE_TOKEN_HEADER, create_listeners
from golem.tasks.apply import ApplyUnavailable, McpApplier, WriteServer
from golem.tasks.ports import Applied, LivePage, ProposalDetail, ProposalGate, ProposalSummary

KEY = SigningKey.generate(kid="run-key-1")
PROPOSAL = "0c6f0d4e-6c43-4a8e-9a55-0f6c7b0f4a11"
KINDS = {"wiki.write": "wiki_edit", "desk.write": "desk_reply", "tracker.write": "tracker_issue"}


def detail(kind: str, payload: dict[str, Any], proposal_id: str = PROPOSAL) -> ProposalDetail:
    return ProposalDetail(
        summary=ProposalSummary(
            id=proposal_id,
            task_id="task-1",
            agent="docs",
            kind=kind,
            state="accepted",
            summary="",
            url=None,
            owner="service:jira",
            created_at="2026-09-29T10:00:00+00:00",
            decided_by="user:bob",
            decided_at="2026-09-29T11:00:00+00:00",
        ),
        payload=payload,
        digest=payload_digest(payload),
        target="alert-3f2a",
        reason=None,
        detail=None,
        report=None,
        stage=False,
    )


@dataclass
class Upstreams:
    confluence: Confluence
    desk: Desk
    jira: Jira

    def of(self, group: str) -> Any:
        return {"wiki.write": self.confluence, "desk.write": self.desk}.get(group, self.jira)


def write_app(group: str, tasks_url: str, audit_dsn: str, upstream: Any, resource: str) -> ASGIApp:
    keys = SigningKeys(
        partial(fetch_jwks, httpx.Client(timeout=2), f"{tasks_url}/internal/run-keys")
    )
    keys.refresh()
    gate = Gate(
        group=GROUPS[group],
        target_system=f"{GROUPS[group].system}:example.test",
        verify=proposal_token_verifier(keys, resource),
        statuses=None,
        audit_dsn=audit_dsn,
        proposals=ProposalStates(httpx.AsyncClient(base_url=tasks_url, timeout=2), ttl_seconds=0),
    )
    base = "https://acme.atlassian.net/wiki" if group == "wiki.write" else "https://jira.test"
    client = httpx.AsyncClient(transport=httpx.MockTransport(upstream), base_url=base)
    return create_mcp_app(
        gate=gate,
        upstream=client,
        jira_deployment=Deployment.CLOUD,
        confluence_deployment=Deployment.CLOUD,
        allowed=frozenset({"OPS", "SD"}),
    )


@dataclass
class Stack:
    urls: dict[str, str]
    resources: dict[str, str]
    orchestrator: FakeOrchestrator
    upstreams: Upstreams

    def applier(self) -> McpApplier:
        return McpApplier(
            servers={
                group: WriteServer(url=url, resource=self.resources[group])
                for group, url in self.urls.items()
            },
            signing_key=KEY,
        )

    def allow(self, kind: str, payload: dict[str, Any], state: str = "accepted") -> None:
        self.orchestrator.gates[PROPOSAL] = ProposalGate(
            PROPOSAL, state, payload_digest(payload), kind
        )


@pytest.fixture
def stack(mcp_audit_dsn: str) -> Iterator[Stack]:
    orchestrator = FakeOrchestrator()
    upstreams = Upstreams(Confluence(Deployment.CLOUD), Desk(), Jira())
    with serve(read_listener(orchestrator, (KEY,))) as tasks_url:
        urls: dict[str, str] = {}
        resources: dict[str, str] = {}
        servers = []
        for group in KINDS:
            resource = f"http://mcp-{group.replace('.', '-')}.test:8000/mcp"
            app = write_app(group, tasks_url, mcp_audit_dsn, upstreams.of(group), resource)
            servers.append(serve(app))
            urls[group] = f"{servers[-1].__enter__()}/mcp"
            resources[group] = resource
        try:
            yield Stack(urls, resources, orchestrator, upstreams)
        finally:
            for server in servers:
                server.__exit__(None, None, None)


def proposal_token(stack: Stack, server: str, payload: dict[str, Any], **overrides: Any) -> str:
    claims = ProposalClaims(
        audience=stack.resources[server],
        decider="user:bob",
        proposal_id=PROPOSAL,
        scope=APPLY,
        group=server,
        digest=payload_digest(payload),
    )
    return issue(replace(claims, **overrides), KEY, int(time.time()))


def call(
    stack: Stack, group: str, token: str, tool: str, arguments: dict[str, Any]
) -> httpx.Response:
    return httpx.post(
        stack.urls[group],
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json, text/event-stream",
        },
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": tool, "arguments": arguments},
        },
        timeout=10,
    )


async def audit_rows(dsn: str) -> list[dict[str, Any]]:
    async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as conn:
        cursor = await conn.execute(
            "SELECT account, request, operation, result, source, chain FROM audit_log ORDER BY id"
        )
        columns = [c.name for c in cursor.description or []]
        return [dict(zip(columns, row, strict=True)) for row in await cursor.fetchall()]


# Applied through the write server


async def test_an_accepted_page_edit_is_applied_as_the_person_who_accepted_it(
    stack: Stack, audit_admin_dsn: str
) -> None:
    stack.allow("wiki_edit", PAGE)

    result = await stack.applier().apply(detail("wiki_edit", PAGE))

    assert result == Applied("applied", "Page 123 is at version 8.")
    assert stack.upstreams.confluence.message == f"golem:{PROPOSAL} accepted by user:bob"
    rows = [r for r in await audit_rows(audit_admin_dsn) if r["operation"] == "apply_page_edit"]
    [row] = rows
    assert row["account"] == "user:bob"
    assert row["result"] == "allow"
    assert row["source"] == "mcp:wiki.write"
    assert row["chain"] == []
    assert f"proposal={PROPOSAL}" in row["request"]
    assert "act=service:golem-tasks" in row["request"]
    assert f"sha256:{payload_digest(PAGE)[:16]}" in row["request"]
    # The page's body is not logged, only its digest.
    assert "<p>New</p>" not in row["request"]


@pytest.mark.parametrize(
    ("kind", "payload", "writes"),
    [
        ("wiki_edit", PAGE, lambda u: [r for r in u.confluence.requests if r.method == "PUT"]),
        ("desk_reply", REPLY, lambda u: u.desk.posts()),
        ("tracker_issue", ISSUE, lambda u: u.jira.posts()),
        ("tracker_issue", COMMENT, lambda u: u.jira.posts()),
    ],
)
async def test_each_kind_applied_twice_writes_once(
    stack: Stack, kind: str, payload: dict[str, Any], writes: Any
) -> None:
    stack.allow(kind, payload)
    applier = stack.applier()

    first = await applier.apply(detail(kind, payload))
    second = await applier.apply(detail(kind, payload))

    assert (first.state, second.state) == ("applied", "applied")
    assert len(writes(stack.upstreams)) == 1


async def test_a_page_changed_since_the_role_read_it_is_stale(stack: Stack) -> None:
    stack.upstreams.confluence.version = 9
    stack.allow("wiki_edit", PAGE)

    result = await stack.applier().apply(detail("wiki_edit", PAGE))

    assert result.state == "stale"
    assert [r for r in stack.upstreams.confluence.requests if r.method == "PUT"] == []


async def test_a_new_issue_carries_the_target_the_task_service_sent(stack: Stack) -> None:
    stack.allow("tracker_issue", ISSUE)

    await stack.applier().apply(detail("tracker_issue", ISSUE))

    [fields] = stack.upstreams.jira.issues.values()
    assert fields["labels"] == ["golem-0c6f0d4e6c43", "golem-alert-3f2a"]


async def test_a_preview_reads_the_live_page_for_a_pending_proposal(stack: Stack) -> None:
    stack.allow("wiki_edit", PAGE, state="pending")

    live = await stack.applier().preview(detail("wiki_edit", PAGE), "user:carol")

    assert live == LivePage(title="Runbook", version=7, body="<p>Old</p>")


async def test_outside_the_allowed_spaces_the_apply_fails_and_nothing_is_written(
    stack: Stack,
) -> None:
    stack.upstreams.confluence.space = "HR"
    stack.allow("wiki_edit", PAGE)

    result = await stack.applier().apply(detail("wiki_edit", PAGE))

    assert result.state == "failed"
    assert "space HR is not one this server writes to" in (result.detail or "")


# Refused at the gate


def refusal(response: httpx.Response) -> tuple[int, str]:
    return response.status_code, response.json()["error_description"]


async def test_a_proposal_that_is_not_accepted_is_not_applied(stack: Stack) -> None:
    stack.allow("wiki_edit", PAGE, state="rejected")
    token = proposal_token(stack, "wiki.write", PAGE)

    response = call(
        stack, "wiki.write", token, "apply_page_edit", {"proposal_id": PROPOSAL, "payload": PAGE}
    )

    assert refusal(response) == (401, "the proposal is rejected")
    with pytest.raises(ApplyUnavailable):
        await stack.applier().apply(detail("wiki_edit", PAGE))


def test_an_unknown_proposal_is_refused(stack: Stack) -> None:
    token = proposal_token(stack, "wiki.write", PAGE)

    response = call(
        stack, "wiki.write", token, "apply_page_edit", {"proposal_id": PROPOSAL, "payload": PAGE}
    )

    assert refusal(response) == (401, "the proposal is unknown")


def test_a_payload_other_than_the_one_decided_is_refused(stack: Stack) -> None:
    stack.allow("wiki_edit", PAGE)
    token = proposal_token(stack, "wiki.write", PAGE)
    swapped = {**PAGE, "body": "<p>Something else</p>"}

    response = call(
        stack,
        "wiki.write",
        token,
        "apply_page_edit",
        {"proposal_id": PROPOSAL, "payload": swapped},
    )

    assert refusal(response) == (403, "the payload is not the one the person decided")
    assert stack.upstreams.confluence.requests == []


def test_a_token_for_a_payload_the_row_does_not_hold_is_refused(stack: Stack) -> None:
    stack.allow("wiki_edit", PAGE)
    other = {**PAGE, "title": "Other"}
    token = proposal_token(stack, "wiki.write", other)

    response = call(
        stack, "wiki.write", token, "apply_page_edit", {"proposal_id": PROPOSAL, "payload": other}
    )

    assert refusal(response) == (401, "the token was issued for another payload")


def test_a_call_naming_another_proposal_is_refused(stack: Stack) -> None:
    stack.allow("wiki_edit", PAGE)
    token = proposal_token(stack, "wiki.write", PAGE)

    response = call(
        stack,
        "wiki.write",
        token,
        "apply_page_edit",
        {"proposal_id": str(uuid.uuid4()), "payload": PAGE},
    )

    assert refusal(response) == (403, "the call names another proposal than its token")


def test_a_preview_token_does_not_apply(stack: Stack) -> None:
    stack.allow("wiki_edit", PAGE, state="pending")
    token = proposal_token(stack, "wiki.write", PAGE, scope=PREVIEW)

    response = call(
        stack, "wiki.write", token, "apply_page_edit", {"proposal_id": PROPOSAL, "payload": PAGE}
    )

    assert refusal(response) == (
        403,
        "the token's scope preview does not allow tool 'apply_page_edit'",
    )


def test_a_token_for_another_write_group_is_refused(stack: Stack) -> None:
    stack.allow("desk_reply", REPLY)
    token = proposal_token(stack, "wiki.write", REPLY, group="desk.write")

    response = call(
        stack, "wiki.write", token, "apply_page_edit", {"proposal_id": PROPOSAL, "payload": REPLY}
    )

    assert refusal(response) == (403, "the proposal token does not grant tool group 'wiki.write'")


def test_a_token_for_another_servers_audience_is_refused(stack: Stack) -> None:
    stack.allow("wiki_edit", PAGE)
    token = proposal_token(stack, "wiki.write", PAGE, audience=stack.resources["desk.write"])

    response = call(
        stack, "wiki.write", token, "apply_page_edit", {"proposal_id": PROPOSAL, "payload": PAGE}
    )

    assert response.status_code == 401
    assert "audience" in response.json()["error_description"].lower()


def test_a_run_token_is_refused_by_a_write_server(stack: Stack) -> None:
    stack.allow("wiki_edit", PAGE)
    now = int(time.time())
    run_token = issue_run_token(
        RunClaims("run-1", "docs", "user:bob", "run-0", ("wiki.write",), now + 60), KEY, now
    )

    response = call(
        stack,
        "wiki.write",
        run_token,
        "apply_page_edit",
        {"proposal_id": PROPOSAL, "payload": PAGE},
    )

    assert response.status_code == 401
    assert stack.upstreams.confluence.requests == []


def test_a_proposal_token_is_refused_by_a_read_server(stack: Stack, mcp_audit_dsn: str) -> None:
    orchestrator = FakeOrchestrator()
    with serve(read_listener(orchestrator, (KEY,))) as tasks_url:
        keys = SigningKeys(
            partial(fetch_jwks, httpx.Client(timeout=2), f"{tasks_url}/internal/run-keys")
        )
        keys.refresh()
        gate = Gate(
            group=GROUPS["wiki.read"],
            target_system="confluence:example.test",
            verify=run_token_verifier(keys),
            statuses=RunStatuses(httpx.AsyncClient(base_url=tasks_url), ttl_seconds=0),
            audit_dsn=mcp_audit_dsn,
        )
        app = create_mcp_app(
            gate=gate,
            upstream=httpx.AsyncClient(base_url="https://acme.atlassian.net/wiki"),
            jira_deployment=None,
        )
        claims = ProposalClaims(
            audience="golem-mcp",
            decider="user:bob",
            proposal_id=PROPOSAL,
            scope=APPLY,
            group="wiki.read",
            digest=payload_digest(PAGE),
        )
        token = issue(claims, KEY, int(time.time()))
        with serve(app) as url:
            response = httpx.post(
                f"{url}/mcp",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/json, text/event-stream",
                },
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            )

    assert refusal(response) == (401, "a proposal token is not a run token")


def test_an_unreachable_task_service_refuses_with_503(mcp_audit_dsn: str) -> None:
    orchestrator = FakeOrchestrator()
    with serve(read_listener(orchestrator, (KEY,))) as tasks_url:
        keys = SigningKeys(
            partial(fetch_jwks, httpx.Client(timeout=2), f"{tasks_url}/internal/run-keys")
        )
        keys.refresh()
    gate = Gate(
        group=GROUPS["wiki.write"],
        target_system="confluence:example.test",
        verify=proposal_token_verifier(keys, "http://wiki.test/mcp"),
        statuses=None,
        audit_dsn=mcp_audit_dsn,
        proposals=ProposalStates(
            httpx.AsyncClient(base_url="http://127.0.0.1:9", timeout=1), ttl_seconds=0
        ),
    )
    app = create_mcp_app(
        gate=gate,
        upstream=httpx.AsyncClient(base_url="https://acme.atlassian.net/wiki"),
        jira_deployment=None,
        confluence_deployment=Deployment.CLOUD,
        allowed=frozenset({"OPS"}),
    )
    claims = ProposalClaims(
        audience="http://wiki.test/mcp",
        decider="user:bob",
        proposal_id=PROPOSAL,
        scope=APPLY,
        group="wiki.write",
        digest=payload_digest(PAGE),
    )
    with serve(app) as url:
        response = httpx.post(
            f"{url}/mcp",
            headers={
                "Authorization": f"Bearer {issue(claims, KEY, int(time.time()))}",
                "Accept": "application/json, text/event-stream",
            },
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "apply_page_edit",
                    "arguments": {"proposal_id": PROPOSAL, "payload": PAGE},
                },
            },
        )

    assert response.status_code == 503
    assert response.json()["error_description"].startswith("proposal state unavailable")


# The whole way, on golem_runs


def proposal_row(dsn: str, task_id: str, kind: str, payload: dict[str, Any]) -> str:
    proposal_id = str(uuid.uuid4())
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "UPDATE runs SET status = 'succeeded', proposal_settled_at = now()"
            " FROM run_tasks t WHERE t.run_id = runs.id AND t.task_id = %s",
            (task_id,),
        )
        conn.execute(
            "INSERT INTO proposals (id, run_id, task_id, agent, owner, kind, state,"
            " notified_state, payload, digest, commit, target)"
            " SELECT %s, r.id, r.task_id, r.agent, r.caller, %s, 'pending', 'pending', %s, %s,"
            " 'sha-1', 'alert-3f2a' FROM run_tasks t JOIN runs r ON r.id = t.run_id"
            " WHERE t.task_id = %s",
            (proposal_id, kind, Jsonb(payload), payload_digest(payload), task_id),
        )
    return proposal_id


@pytest.mark.parametrize(
    ("group", "payload", "applied"),
    [
        ("wiki.write", PAGE, lambda u: u.confluence.body == "<p>New</p>"),
        ("desk.write", REPLY, lambda u: len(u.desk.comments) == 1),
        ("tracker.write", ISSUE, lambda u: len(u.jira.issues) == 1),
    ],
)
def test_a_person_accepts_and_the_proposal_is_applied(
    runs_db: str, mcp_audit_dsn: str, group: str, payload: dict[str, Any], applied: Any
) -> None:
    orchestrator = PostgresOrchestrator(
        dsn=runs_db,
        limits=Limits(max_runs_per_caller=10, max_runs_per_root=10, budget_per_root=Decimal("99")),
        estimated_cost=Decimal("1"),
        launcher=FakeLauncher(),
        template=TEMPLATE,
        catalogs={"desk": CATALOG},
        signing_key=KEY,
        grants=GRANTS,
    )
    upstreams = Upstreams(Confluence(Deployment.CLOUD), Desk(), Jira())
    servers: dict[str, WriteServer] = {}
    listeners = create_listeners(
        make_card(),
        orchestrator,
        edge_token=TEST_EDGE_TOKEN,
        applier=McpApplier(servers=servers, signing_key=KEY),
        run_keys=(KEY,),
    )

    async def public(scope: Scope, receive: Receive, send: Send) -> None:
        token = (EDGE_TOKEN_HEADER.encode(), TEST_EDGE_TOKEN.encode())
        await listeners.public(
            {**scope, "headers": [*scope.get("headers", []), token]}, receive, send
        )

    resource = f"http://mcp-{group.replace('.', '-')}.test:8000/mcp"
    with (
        serve(listeners.internal_read) as tasks_url,
        serve(write_app(group, tasks_url, mcp_audit_dsn, upstreams.of(group), resource)) as url,
        TestClient(public) as client,
    ):
        servers[group] = WriteServer(url=f"{url}/mcp", resource=resource)
        task_id = start(client, "desk")
        proposal_id = proposal_row(runs_db, task_id, KINDS[group], payload)

        response = client.post(
            f"/proposals/{proposal_id}/decision",
            json={"decision": "accept"},
            headers=as_person("user:alice"),
        )

    assert response.status_code == 200
    assert response.json()["state"] == "applied"
    assert applied(upstreams)
