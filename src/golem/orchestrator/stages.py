"""Starting a process's stage (ADR 0019): the reconciler calls the edge as a delegating role
would, with a call token it signs for the process.

The token acts for the process's owner (``sub``), by the process (``act``), with the process as
the whole chain and the process run as its root and its run. The edge checks it as any call
token: the registry, which lets a process call its stage agents, depth and cycles, the audit
row, and revocation by the process run's status, so a canceled process starts nothing more.
"""

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx

from golem import call_token
from golem.call_token import CallClaims
from golem.orchestrator.process_runs import StageRefused, StageStart
from golem.run_token import SigningKey

RPC_PATH = "/a2a"
A2A_VERSION = "1.0"
# A process may take days; each start gets a token of its own that outlives only the call.
TOKEN_SECONDS = 300
TARGET_METADATA = "golemTarget"
REJECTED = "TASK_STATE_REJECTED"
# JSON-RPC errors that are the edge's verdict on the call, not an outage: the call registry or
# the request itself. Anything else is retried on the next pass.
REFUSING_CODES = frozenset({-32041, -32600, -32601, -32602})


class StageUnavailable(RuntimeError):
    """The edge could not take the stage now; the next pass tries again, with the same id."""


@dataclass(frozen=True)
class EdgeStages:
    """``client`` carries the edge's base URL."""

    client: httpx.AsyncClient
    signing_key: SigningKey
    clock: Callable[[], float] = field(default=time.time)

    async def start(self, stage: StageStart) -> str | StageRefused:
        now = int(self.clock())
        token = call_token.issue(
            CallClaims(
                subject=stage.owner,
                agent=stage.process,
                chain=(stage.process,),
                root_run_id=stage.process_run_id,
                run_id=stage.process_run_id,
                expires_at=now + TOKEN_SECONDS,
            ),
            self.signing_key,
            now,
        )
        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "SendMessage",
            "params": {
                "tenant": stage.agent,
                "message": {
                    "role": "ROLE_USER",
                    "messageId": stage.message_id,
                    "parts": [{"text": stage.text}],
                    "metadata": {TARGET_METADATA: stage.target},
                },
            },
        }
        headers = {"Authorization": f"Bearer {token}", "A2A-Version": A2A_VERSION}
        try:
            response = await self.client.post(RPC_PATH, json=body, headers=headers)
        except httpx.HTTPError as error:
            raise StageUnavailable(f"the edge is unreachable: {error}") from error
        return started(response)


def started(response: httpx.Response) -> str | StageRefused:
    try:
        answer: Any = response.json()
    except ValueError:
        answer = None
    if not isinstance(answer, dict):
        raise StageUnavailable(f"the edge answered {response.status_code} without JSON-RPC")
    error = answer.get("error")
    if isinstance(error, dict):
        if error.get("code") in REFUSING_CODES and response.status_code < 500:
            return StageRefused(str(error.get("message") or "refused by the edge"))
        raise StageUnavailable(f"the edge answered {response.status_code}: {error}")
    result = answer.get("result")
    task = result.get("task") if isinstance(result, dict) else None
    if not isinstance(task, dict) or not isinstance(task.get("id"), str):
        raise StageUnavailable(f"the edge answered {response.status_code} without a task")
    status = task.get("status")
    status = status if isinstance(status, dict) else {}
    if status.get("state") == REJECTED:
        return StageRefused(status_text(status) or "the stage was refused")
    return task["id"]


def status_text(status: dict[str, Any]) -> str:
    message = status.get("message")
    parts = message.get("parts") if isinstance(message, dict) else None
    if not isinstance(parts, list):
        return ""
    return " ".join(
        p["text"] for p in parts if isinstance(p, dict) and isinstance(p.get("text"), str)
    )
