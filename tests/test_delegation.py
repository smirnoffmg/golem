"""Delegation end to end, in process (ADR 0014): a role in agent A asks agent B for work.

Everything on the path is real: the role runner with the delegation tool, the edge (call token
verification against the task service's run keys, revocation by the issuing run's status,
chain policy, audit in Postgres), the task service and the orchestrator on Postgres. Fakes only
at the boundaries: the model, Kubernetes (the fake launcher) and the identity provider's keys.
"""

import re
import time
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, replace
from decimal import Decimal
from functools import partial
from pathlib import Path
from typing import Any

import httpx
import jwt
import psycopg
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from starlette.testclient import TestClient
from support.idp import jwk
from test_deepagents_runner import make_brief, scripted, tool_call
from test_tasks_service import make_card
from test_tasks_to_runs import CATALOG, TEMPLATE, FakeLauncher

from golem.catalog import DELEGATE_GROUP, Neighbour, Role
from golem.edge.__main__ import authenticator, call_authenticator
from golem.edge.app import create_edge_app
from golem.edge.auth import authenticate_any
from golem.edge.policy import ChainLimits, Registry
from golem.jwks import SigningKeys, fetch_jwks
from golem.orchestrator.admission import Limits
from golem.orchestrator.jobs import JobSpec
from golem.orchestrator.service import PostgresOrchestrator
from golem.run_status import RunStatuses
from golem.run_token import SigningKey
from golem.runtime.deepagents_runner import DeepAgentsRunner
from golem.runtime.delegation import DELEGATE_TOOL, Delegation, delegation_tool
from golem.runtime.tools import McpToolbox, ToolGroup
from golem.runtime.tools import Registry as ToolRegistry
from golem.tasks.app import RUN_KEYS_PATH, create_listeners

EDGE_TOKEN = "edge-shared-secret"
EDGE_A2A = "http://edge/a2a"
ISSUER = "https://idp.example.test/realms/golem"
AUDIENCE = "golem-edge"
IDP_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
RUN_KEY = SigningKey.generate(kid="golem-1")
# A may hand work to B, B to C; nobody may hand work to A, and A may not call C.
CALL_REGISTRY = Registry(
    allowed_callers={
        "a": frozenset({"user:*"}),
        "b": frozenset({"user:*", "agent:a"}),
        "c": frozenset({"user:*", "agent:b"}),
    }
)
# The tool offers all three, so that what refuses a call in these tests is the edge.
NEIGHBOURS = tuple(Neighbour(agent=name, when=f"ask {name}") for name in ("a", "b", "c"))
# Budget for two runs per chain: A and one child.
LIMITS = Limits(max_runs_per_caller=10, max_runs_per_root=10, budget_per_root=Decimal("2"))


def idp_token(user: str) -> str:
    now = int(time.time())
    claims = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "iat": now,
        "exp": now + 300,
        "sub": user,
        "preferred_username": user,
    }
    return jwt.encode(claims, IDP_KEY, algorithm="RS256", headers={"kid": "idp-1"})


@dataclass
class Golem:
    edge: httpx.AsyncClient
    transport: httpx.AsyncBaseTransport
    launcher: FakeLauncher
    runs_dsn: str

    async def rpc(
        self, token: str, method: str, params: dict[str, Any], headers: dict[str, str] | None = None
    ) -> httpx.Response:
        return await self.edge.post(
            "/a2a",
            json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
            headers={"Authorization": f"Bearer {token}", "A2A-Version": "1.0", **(headers or {})},
        )

    async def start(
        self, user: str, agent: str, message_id: str, headers: dict[str, str] | None = None
    ) -> dict[str, Any]:
        message = {"role": "ROLE_USER", "messageId": message_id, "parts": [{"text": "go"}]}
        response = await self.rpc(
            idp_token(user), "SendMessage", {"tenant": agent, "message": message}, headers
        )
        return response.json()["result"]["task"]

    def run_of(self, agent: str) -> JobSpec:
        [spec] = [s for s in self.launcher.launched if s.agent == agent]
        return spec

    def roots(self) -> dict[str, str]:
        with psycopg.connect(self.runs_dsn) as conn:
            rows = conn.execute("SELECT id, root_run_id FROM runs").fetchall()
        return {str(run): str(root) for run, root in rows}

    async def delegate(self, spec: JobSpec, agent: str, goal: str) -> str:
        """The tool as the role in `spec`'s Job would call it."""
        tool = delegation_tool(
            Delegation(
                url=EDGE_A2A,
                call_token=spec.call_token,
                run_id=spec.run_id,
                call_timeout=10,
                max_result_chars=10_000,
                transport=self.transport,
            ),
            NEIGHBOURS,
        )
        return await tool.ainvoke({"agent": agent, "goal": goal})


@pytest.fixture
def launcher() -> FakeLauncher:
    return FakeLauncher()


@pytest.fixture
def run_keys_client(runs_db: str, launcher: FakeLauncher) -> Iterator[tuple[Any, TestClient]]:
    orchestrator = PostgresOrchestrator(
        dsn=runs_db,
        limits=LIMITS,
        estimated_cost=Decimal("1"),
        launcher=launcher,
        template=TEMPLATE,
        catalogs={"a": CATALOG, "b": CATALOG, "c": CATALOG},
        signing_key=RUN_KEY,
        grants={},
    )
    listeners = create_listeners(
        make_card(), orchestrator, edge_token=EDGE_TOKEN, run_keys=(RUN_KEY,)
    )
    with TestClient(listeners.internal_read) as read_port:
        yield listeners, read_port


@pytest.fixture
async def golem(
    run_keys_client: tuple[Any, TestClient],
    launcher: FakeLauncher,
    runs_db: str,
    audit_dsn: str,
    audit_admin_dsn: str,
) -> AsyncIterator[Golem]:
    listeners, read_port = run_keys_client
    idp_keys = SigningKeys(lambda: jwt.PyJWKSet.from_dict({"keys": [jwk(IDP_KEY, "idp-1")]}))
    golem_keys = SigningKeys(partial(fetch_jwks, read_port, RUN_KEYS_PATH))
    edge = create_edge_app(
        authenticate=partial(
            authenticate_any,
            idp=authenticator(idp_keys, issuer=ISSUER, audience=AUDIENCE),
            golem=call_authenticator(golem_keys),
        ),
        registry=CALL_REGISTRY,
        limits=ChainLimits(max_depth=2),
        audit_dsn=audit_dsn,
        forward=httpx.AsyncClient(
            transport=httpx.ASGITransport(app=listeners.public), base_url="http://tasks"
        ),
        edge_token=EDGE_TOKEN,
        cards={},
        run_statuses=RunStatuses(
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=listeners.internal_read),
                base_url="http://tasks-read",
            ),
            ttl_seconds=10,
        ),
    )
    transport = httpx.ASGITransport(app=edge, client=("10.0.0.9", 40000))
    async with httpx.AsyncClient(transport=transport, base_url="http://edge") as client:
        yield Golem(client, transport, launcher, runs_db)


async def audit_rows(admin_dsn: str) -> list[tuple[str, str, str, list[str]]]:
    async with await psycopg.AsyncConnection.connect(admin_dsn) as conn:
        cursor = await conn.execute(
            "SELECT account, target_system, result, chain FROM audit_log ORDER BY id"
        )
        return await cursor.fetchall()


def planner() -> Role:
    return Role(name="planner", writes="hypotheses/", tools=(DELEGATE_GROUP,))


async def test_a_role_in_a_delegates_to_b_for_the_human_under_as_root(
    golem: Golem, audit_admin_dsn: str, tmp_path: Path
) -> None:
    task_a = await golem.start("alice", "a", "m-a")
    run_a = golem.run_of("a")
    model = scripted(tool_call(DELEGATE_TOOL, agent="b", goal="Review solution S-1."), "Handed.")
    toolbox = McpToolbox(
        registry=ToolRegistry(groups=(ToolGroup(DELEGATE_GROUP, EDGE_A2A, (DELEGATE_TOOL,)),)),
        call_token=run_a.call_token,
        run_id=run_a.run_id,
        edge_transport=golem.transport,
    )

    result = await DeepAgentsRunner(model=model, toolbox=toolbox).run(
        replace(make_brief(tmp_path), role=planner(), delegates=NEIGHBOURS)
    )

    run_b = golem.run_of("b")
    assert result.summary == "Handed."
    assert run_b.goal == "Review solution S-1."
    # One budget per chain: B is admitted under A's run as its root.
    assert golem.roots() == {run_a.run_id: run_a.run_id, run_b.run_id: run_a.run_id}
    # The chain policy and the audit saw the acting agent; the account is the human.
    assert (await audit_rows(audit_admin_dsn))[-1] == ("user:alice", "agent:b", "allow", ["a"])
    # B's task is the human's: alice reads it, with the chain; bob does not see it.
    [task_b_id] = re.findall(r"task (\S+), state", model.prompts[1][-1].text)
    assert task_b_id != task_a["id"]
    got = await golem.rpc(idp_token("alice"), "GetTask", {"tenant": "b", "id": task_b_id})
    task_b = got.json()["result"]
    assert task_b["status"]["state"] == "TASK_STATE_WORKING"
    assert task_b["metadata"] == {"golemAgent": "b", "runId": run_b.run_id, "chain": ["a"]}
    hidden = await golem.rpc(idp_token("bob"), "GetTask", {"tenant": "b", "id": task_b_id})
    assert "error" in hidden.json()


async def test_a_delegating_to_itself_is_refused_as_a_cycle(golem: Golem) -> None:
    await golem.start("alice", "a", "m-a")

    reply = await golem.delegate(golem.run_of("a"), "a", "Do it again.")

    assert "cycle" in reply
    assert len(golem.launcher.launched) == 1


async def test_a_chain_deeper_than_the_limit_is_refused(golem: Golem) -> None:
    await golem.start("alice", "a", "m-a")
    await golem.delegate(golem.run_of("a"), "b", "Review S-1.")

    reply = await golem.delegate(golem.run_of("b"), "c", "Check the review.")

    assert "depth_exceeded" in reply
    assert [s.agent for s in golem.launcher.launched] == ["a", "b"]


async def test_an_agent_the_registry_does_not_allow_is_refused(
    golem: Golem, audit_admin_dsn: str
) -> None:
    await golem.start("alice", "a", "m-a")

    reply = await golem.delegate(golem.run_of("a"), "c", "Check something.")

    assert "not_allowed" in reply
    account, target, result, chain = (await audit_rows(audit_admin_dsn))[-1]
    assert (account, target, chain) == ("user:alice", "agent:c", ["a"])
    assert result.startswith("deny: not_allowed")


async def test_a_chain_whose_budget_is_spent_gets_its_child_rejected(golem: Golem) -> None:
    await golem.start("alice", "a", "m-a")
    run_a = golem.run_of("a")
    first = await golem.delegate(run_a, "b", "Review S-1.")

    second = await golem.delegate(run_a, "b", "Review S-2.")

    assert "TASK_STATE_WORKING" in first
    assert "TASK_STATE_REJECTED" in second
    assert "budget" in second
    assert len(golem.launcher.launched) == 2


async def test_a_retried_delegation_reaches_the_same_child(golem: Golem) -> None:
    await golem.start("alice", "a", "m-a")
    run_a = golem.run_of("a")

    first = await golem.delegate(run_a, "b", "Review S-1.")
    retry = await golem.delegate(run_a, "b", "Review S-1.")

    assert "TASK_STATE_WORKING" in first
    assert "TASK_STATE_WORKING" in retry
    assert len(golem.roots()) == 2


async def test_chain_headers_a_client_sends_are_stripped(golem: Golem) -> None:
    await golem.start("alice", "a", "m-a")
    run_a = golem.run_of("a")

    task = await golem.start(
        "alice",
        "b",
        "m-spoof",
        headers={"X-Golem-Chain": "a", "X-Golem-Root-Run": run_a.run_id},
    )

    run_b = golem.run_of("b")
    assert golem.roots()[run_b.run_id] == run_b.run_id
    assert "chain" not in task["metadata"]


async def test_a_run_token_presented_to_the_edge_is_refused(golem: Golem) -> None:
    await golem.start("alice", "a", "m-a")
    message = {"role": "ROLE_USER", "messageId": "m-x", "parts": [{"text": "go"}]}

    response = await golem.rpc(
        golem.run_of("a").run_token, "SendMessage", {"tenant": "b", "message": message}
    )

    assert response.status_code == 401
    assert "audience" in response.json()["error"]["message"].lower()
    assert len(golem.launcher.launched) == 1


# Revocation: a call token stops working when its run stops running


async def test_a_canceled_parent_can_no_longer_delegate(golem: Golem) -> None:
    task_a = await golem.start("alice", "a", "m-a")
    canceled = await golem.rpc(
        idp_token("alice"), "CancelTask", {"tenant": "a", "id": task_a["id"]}
    )
    assert canceled.json()["result"]["status"]["state"] == "TASK_STATE_CANCELED"

    reply = await golem.delegate(golem.run_of("a"), "b", "Review S-1.")

    assert "run_not_active: the run is canceled" in reply
    assert [s.agent for s in golem.launcher.launched] == ["a"]


async def test_a_finished_parent_can_no_longer_delegate(golem: Golem) -> None:
    await golem.start("alice", "a", "m-a")
    run_a = golem.run_of("a")
    with psycopg.connect(golem.runs_dsn, autocommit=True) as conn:
        conn.execute("UPDATE runs SET status = 'succeeded' WHERE id = %s", (run_a.run_id,))

    reply = await golem.delegate(run_a, "b", "Review S-1.")

    assert "run_not_active: the run is succeeded" in reply
    assert [s.agent for s in golem.launcher.launched] == ["a"]
