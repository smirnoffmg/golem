"""The `delegate_to_agent` tool: an A2A SendMessage to the edge with the run's call token."""

import asyncio
import json
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import httpx
import pytest
from test_deepagents_runner import make_brief, scripted, tool_call

from golem.catalog import DELEGATE_GROUP, Role
from golem.runtime.deepagents_runner import DeepAgentsRunner
from golem.runtime.delegation import DELEGATE_TOOL, message_id
from golem.runtime.tools import (
    McpToolbox,
    Registry,
    RegistryError,
    ToolGroup,
    ToolLimits,
    ToolLoadError,
    parse_registry,
    toolbox_from_env,
)

CALL_TOKEN = "call-token-s3cr3t"
EDGE_URL = "http://edge.golem-system.svc:8000/a2a"
REGISTRY = Registry(groups=(ToolGroup(DELEGATE_GROUP, EDGE_URL, (DELEGATE_TOOL,)),))


@dataclass
class Edge:
    """The edge at its HTTP boundary: records requests, answers what the test says."""

    answer: Any = field(
        default_factory=lambda: {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {"task": {"id": "task-9", "status": {"state": "TASK_STATE_WORKING"}}},
        }
    )
    status_code: int = 200
    delay: float = 0.0
    requests: list[httpx.Request] = field(default_factory=list)

    async def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.delay:
            await asyncio.sleep(self.delay)
        return httpx.Response(self.status_code, json=self.answer)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def bodies(self) -> list[dict[str, Any]]:
        return [json.loads(request.content) for request in self.requests]


def planner(*tools: str) -> Role:
    return Role(name="planner", writes="hypotheses/", tools=tools)


def toolbox(edge: Edge, **changes: Any) -> McpToolbox:
    base = McpToolbox(
        registry=REGISTRY,
        call_token=CALL_TOKEN,
        run_id="run-1",
        edge_transport=edge.transport(),
    )
    return replace(base, **changes)


async def delegate(box: McpToolbox, agent: str = "reviewer", goal: str = "review S-1") -> str:
    [tool] = await box.tools_for(planner(DELEGATE_GROUP))
    return await tool.ainvoke({"agent": agent, "goal": goal})


# Offering the tool


async def test_a_role_that_does_not_list_the_group_gets_no_delegation_tool() -> None:
    assert await toolbox(Edge()).tools_for(planner()) == []


async def test_a_role_that_lists_the_group_gets_exactly_the_delegation_tool() -> None:
    tools = await toolbox(Edge()).tools_for(planner(DELEGATE_GROUP))

    assert [tool.name for tool in tools] == [DELEGATE_TOOL]


async def test_delegation_without_a_call_token_fails_closed() -> None:
    with pytest.raises(ToolLoadError, match="GOLEM_CALL_TOKEN"):
        await toolbox(Edge(), call_token=None).tools_for(planner(DELEGATE_GROUP))


def test_the_registry_entry_for_delegation_offers_only_the_delegation_tool() -> None:
    with pytest.raises(RegistryError, match=DELEGATE_GROUP):
        parse_registry({DELEGATE_GROUP: {"url": EDGE_URL, "tools": ["delete_everything"]}})


def test_toolbox_from_env_reads_the_call_token_and_run_id() -> None:
    box = toolbox_from_env({"GOLEM_CALL_TOKEN": CALL_TOKEN, "GOLEM_RUN_ID": "run-7"})

    assert (box.call_token, box.run_id) == (CALL_TOKEN, "run-7")
    assert CALL_TOKEN not in repr(box)


# The call


async def test_the_call_is_a_send_message_to_the_agent_with_the_call_token() -> None:
    edge = Edge()

    await delegate(toolbox(edge))

    [request] = edge.requests
    assert str(request.url) == EDGE_URL
    assert request.headers["authorization"] == f"Bearer {CALL_TOKEN}"
    assert request.headers["a2a-version"] == "1.0"
    [body] = edge.bodies()
    assert body["method"] == "SendMessage"
    assert body["params"]["tenant"] == "reviewer"
    message = body["params"]["message"]
    assert message["role"] == "ROLE_USER"
    assert message["parts"] == [{"text": "review S-1"}]
    assert message["messageId"] == message_id("run-1", "reviewer", "review S-1")
    assert "taskId" not in message


async def test_a_retried_call_sends_the_same_message_id_so_no_second_child_starts() -> None:
    edge = Edge()
    box = toolbox(edge)

    await delegate(box)
    await delegate(box)
    await delegate(box, goal="review S-2")
    await delegate(replace(box, run_id="run-2"))

    ids = [body["params"]["message"]["messageId"] for body in edge.bodies()]
    assert ids[0] == ids[1]
    assert len(set(ids)) == 3


async def test_the_answer_names_the_child_task_and_its_state() -> None:
    reply = await delegate(toolbox(Edge()))

    assert "task-9" in reply
    assert "TASK_STATE_WORKING" in reply


async def test_a_rejected_child_reports_the_reason() -> None:
    edge = Edge(
        answer={
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "task": {
                    "id": "task-3",
                    "status": {
                        "state": "TASK_STATE_REJECTED",
                        "message": {"parts": [{"text": "Call chain r has spent 2."}]},
                    },
                }
            },
        }
    )

    reply = await delegate(toolbox(edge))

    assert "TASK_STATE_REJECTED" in reply
    assert "Call chain r has spent 2." in reply


async def test_a_refusal_by_the_edge_reports_its_reason() -> None:
    edge = Edge(
        answer={
            "jsonrpc": "2.0",
            "id": 1,
            "error": {"code": -32041, "message": "cycle: agent 'planner' is already in chain"},
        }
    )

    reply = await delegate(toolbox(edge))

    assert "refused" in reply.lower()
    assert "cycle: agent 'planner' is already in chain" in reply


async def test_an_unauthenticated_answer_is_reported_not_raised() -> None:
    edge = Edge(
        status_code=401,
        answer={"jsonrpc": "2.0", "id": None, "error": {"code": -32040, "message": "expired"}},
    )

    assert "expired" in await delegate(toolbox(edge))


async def test_a_slow_edge_times_out_as_a_tool_answer() -> None:
    edge = Edge(delay=5)

    reply = await delegate(toolbox(edge, limits=ToolLimits(call_timeout=0.2)))

    assert "timed out after 0.2 s" in reply


async def test_an_oversized_answer_is_cut_to_the_result_limit() -> None:
    edge = Edge(
        answer={"jsonrpc": "2.0", "id": 1, "error": {"code": -32041, "message": "x" * 5_000}}
    )

    reply = await delegate(toolbox(edge, limits=ToolLimits(max_result_chars=300)))

    assert len(reply) <= 300 + 80
    assert "truncated" in reply
    assert CALL_TOKEN not in reply


async def test_an_answer_that_is_not_json_rpc_is_reported() -> None:
    edge = Edge(status_code=502, answer=["not", "a", "response"])

    assert "502" in await delegate(toolbox(edge))


# The role runner


async def test_a_role_delegates_and_reads_the_child_task_id(tmp_path: Path) -> None:
    edge = Edge()
    model = scripted(tool_call(DELEGATE_TOOL, agent="reviewer", goal="review S-1"), "Delegated.")
    brief = replace(make_brief(tmp_path), role=planner(DELEGATE_GROUP))

    result = await DeepAgentsRunner(model=model, toolbox=toolbox(edge)).run(brief)

    assert result.summary == "Delegated."
    assert DELEGATE_TOOL in model.bound_tools
    assert "task-9" in model.prompts[1][-1].text
