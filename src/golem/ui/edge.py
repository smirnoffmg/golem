"""The UI's calls to the edge: A2A JSON-RPC and the directory of agents, with the signed-in
user's own access token."""

import secrets
from typing import Any
from urllib.parse import quote

import httpx
from a2a.utils.constants import VERSION_HEADER

from golem.adapters.common import A2A_VERSION, EDGE_RPC_PATH

TASK_NOT_FOUND = -32001
TASK_NOT_CANCELABLE = -32002
INVALID_PARAMS = -32602
DIRECTORY_PATH = "/agents"
RESOLUTION_PATH = "/processes/{task_id}/resolution"
# What the edge refuses a resolution with, and the task service after it (ADR 0019).
RESOLUTION_REFUSALS = frozenset({400, 404, 409})


class EdgeUnauthorized(Exception):
    """The edge refused the user's token: the session cannot act any more."""


class EdgeLimited(Exception):
    """The edge limited the user (ADR 0012); the board waits as long as it says."""

    def __init__(self, retry_after: int) -> None:
        super().__init__(f"rate limited: retry after {retry_after} s")
        self.retry_after = retry_after


class EdgeRefused(Exception):
    """The edge refused the request itself, with a status and an error code of its own."""

    def __init__(self, status: int, error: str) -> None:
        super().__init__(f"refused {status}: {error}")
        self.status = status
        self.error = error


class EdgeError(Exception):
    def __init__(self, code: int | None, message: str) -> None:
        super().__init__(message)
        self.code = code


def retry_after(response: httpx.Response) -> int:
    try:
        return max(1, int(response.headers.get("retry-after", "1")))
    except ValueError:
        return 1


def checked(response: httpx.Response) -> None:
    if response.status_code == 401:
        raise EdgeUnauthorized()
    if response.status_code == 429:
        raise EdgeLimited(retry_after(response))


async def rpc(
    edge: httpx.AsyncClient, token: str, method: str, params: dict[str, Any]
) -> dict[str, Any]:
    request = {"jsonrpc": "2.0", "id": secrets.token_hex(8), "method": method, "params": params}
    headers = {"Authorization": f"Bearer {token}", VERSION_HEADER: A2A_VERSION}
    try:
        response = await edge.post(EDGE_RPC_PATH, json=request, headers=headers)
    except httpx.HTTPError as error:
        raise EdgeError(None, f"the edge is unreachable: {error}") from error
    checked(response)
    try:
        body = response.json()
    except ValueError as error:
        raise EdgeError(None, f"the edge answered {response.status_code}") from error
    refusal = body.get("error") if isinstance(body, dict) else None
    if isinstance(refusal, dict):
        code = refusal.get("code") if isinstance(refusal.get("code"), int) else None
        raise EdgeError(code, str(refusal.get("message") or "error"))
    result = body.get("result") if isinstance(body, dict) else None
    if not isinstance(result, dict):
        raise EdgeError(None, "the edge answered without a result")
    return result


async def directory(edge: httpx.AsyncClient, token: str) -> list[dict[str, Any]]:
    """The agents this user may call (ADR 0014): name, description and skill ids."""
    try:
        response = await edge.get(DIRECTORY_PATH, headers={"Authorization": f"Bearer {token}"})
    except httpx.HTTPError as error:
        raise EdgeError(None, f"the edge is unreachable: {error}") from error
    checked(response)
    try:
        body = response.json() if response.status_code == 200 else None
    except ValueError:
        body = None
    listed = body.get("agents") if isinstance(body, dict) else None
    if not isinstance(listed, list):
        raise EdgeError(None, f"the edge answered {response.status_code} without a directory")
    agents = []
    for entry in listed:
        if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
            continue
        description = entry.get("description")
        skills = entry.get("skills")
        agents.append(
            {
                "name": entry["name"],
                "description": description if isinstance(description, str) else "",
                "skills": [s for s in skills if isinstance(s, str)]
                if isinstance(skills, list)
                else [],
            }
        )
    return agents


async def resolve(
    edge: httpx.AsyncClient, token: str, task_id: str, action: str, reason: str | None
) -> None:
    """A process owner's answer to a process waiting for a reason (ADR 0019)."""
    body = {"action": action} | ({"reason": reason} if reason is not None else {})
    path = RESOLUTION_PATH.format(task_id=quote(task_id, safe=""))
    try:
        response = await edge.post(path, json=body, headers={"Authorization": f"Bearer {token}"})
    except httpx.HTTPError as error:
        raise EdgeError(None, f"the edge is unreachable: {error}") from error
    checked(response)
    if response.status_code == 200:
        return
    try:
        answer = response.json()
    except ValueError:
        answer = None
    code = answer.get("error") if isinstance(answer, dict) else None
    if response.status_code in RESOLUTION_REFUSALS and isinstance(code, str):
        raise EdgeRefused(response.status_code, code)
    raise EdgeError(None, f"the edge answered {response.status_code}")
