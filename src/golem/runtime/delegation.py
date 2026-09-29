"""`delegate_to_agent`: a role asks another agent for work, through the edge (ADR 0014).

The call is an ordinary A2A ``SendMessage`` to the edge with the run's call token, so the edge
authenticates, applies the call registry, depth and cycle checks, audits and forwards it like
any caller's. The role does not wait: it gets the child task's id at once, and the child run
proposes its own merge request. The message id is derived from the run, the agent and the goal,
so a retried tool call reaches the same child run instead of starting another.

The tool offers only the agent's neighbours, each with when to ask it (ADR 0019): the model
chooses among a few it is told about, never among every agent the platform runs.
"""

import asyncio
import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass, field

import httpx
from langchain_core.tools import BaseTool, StructuredTool

from golem.catalog import Neighbour
from golem.task_json import status_text

DELEGATE_TOOL = "delegate_to_agent"
A2A_VERSION = "1.0"
DESCRIPTION = (
    "Ask another agent to work on a goal in a run of its own. Returns the child task's id and"
    " state at once; it does not wait for the result. The child proposes its own merge request,"
    " so record the task id (for example in the record you write). `agent` is the agent's name,"
    " `goal` says what it should do, self-contained: it sees nothing of this run."
)


@dataclass(frozen=True)
class Delegation:
    url: str
    call_token: str = field(repr=False)
    run_id: str
    call_timeout: float
    max_result_chars: int
    # The edge is a network boundary; tests put it behind a transport.
    transport: httpx.AsyncBaseTransport | None = None


def message_id(run_id: str, agent: str, goal: str) -> str:
    digest = hashlib.sha256(json.dumps([run_id, agent, goal]).encode()).hexdigest()
    return f"delegate-{digest[:40]}"


def delegation_tool(delegation: Delegation, neighbours: Sequence[Neighbour]) -> BaseTool:
    names = [neighbour.agent for neighbour in neighbours]

    async def delegate_to_agent(agent: str, goal: str) -> str:
        # The schema's enum is advice to the model, not a check: a JSON schema is not validated.
        if agent not in names:
            return (
                f"{DELEGATE_TOOL}: {agent!r} is not a neighbour of this agent;"
                f" choose one of {', '.join(names)}"
            )
        return bounded(await send(delegation, agent, goal), delegation.max_result_chars)

    return StructuredTool.from_function(
        coroutine=delegate_to_agent,
        name=DELEGATE_TOOL,
        description=describe_neighbours(neighbours),
        args_schema={
            "type": "object",
            "properties": {
                "agent": {"type": "string", "enum": names, "description": "the neighbour to ask"},
                "goal": {"type": "string", "description": "what it should do, self-contained"},
            },
            "required": ["agent", "goal"],
        },
    )


def describe_neighbours(neighbours: Sequence[Neighbour]) -> str:
    listed = "\n".join(f"- {neighbour.agent}: {neighbour.when}" for neighbour in neighbours)
    return f"{DESCRIPTION}\n\nAsk only these agents, when their case applies:\n{listed}"


async def send(delegation: Delegation, agent: str, goal: str) -> str:
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "SendMessage",
        "params": {
            "tenant": agent,
            "message": {
                "role": "ROLE_USER",
                "messageId": message_id(delegation.run_id, agent, goal),
                "parts": [{"text": goal}],
            },
        },
    }
    headers = {
        "Authorization": f"Bearer {delegation.call_token}",
        "A2A-Version": A2A_VERSION,
    }
    try:
        async with (
            asyncio.timeout(delegation.call_timeout),
            httpx.AsyncClient(
                transport=delegation.transport, timeout=delegation.call_timeout
            ) as client,
        ):
            response = await client.post(delegation.url, json=body, headers=headers)
    except (TimeoutError, httpx.TimeoutException):
        return f"{DELEGATE_TOOL} timed out after {delegation.call_timeout:g} s"
    except httpx.HTTPError as error:
        return f"{DELEGATE_TOOL}: the edge is unreachable ({type(error).__name__})"
    return describe(agent, response)


def describe(agent: str, response: httpx.Response) -> str:
    try:
        answer = response.json()
    except ValueError:
        answer = None
    if not isinstance(answer, dict):
        return f"{DELEGATE_TOOL}: the edge answered {response.status_code} without a result"
    error = answer.get("error")
    if isinstance(error, dict):
        return f"Refused by the edge: {error.get('message', 'no reason given')}"
    result = answer.get("result")
    task = result.get("task") if isinstance(result, dict) else None
    if not isinstance(task, dict):
        return f"{DELEGATE_TOOL}: the edge answered {response.status_code} without a task"
    status = task.get("status")
    status = status if isinstance(status, dict) else {}
    reply = f"Delegated to {agent}: task {task.get('id')}, state {status.get('state')}."
    reason = status_text(status)
    return f"{reply} {reason}" if reason else reply


def bounded(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return f"{text[:limit]}\n[truncated: {len(text)} characters, the first {limit} shown]"
