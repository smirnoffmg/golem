"""The UI's calls to the edge: A2A JSON-RPC with the signed-in user's own access token, and the
public agent cards."""

import secrets
from typing import Any

import httpx
from a2a.utils.constants import AGENT_CARD_WELL_KNOWN_PATH, VERSION_HEADER

from golem.adapters.common import A2A_VERSION, EDGE_RPC_PATH

TASK_NOT_FOUND = -32001


class EdgeUnauthorized(Exception):
    """The edge refused the user's token: the session cannot act any more."""


class EdgeError(Exception):
    def __init__(self, code: int | None, message: str) -> None:
        super().__init__(message)
        self.code = code


async def rpc(
    edge: httpx.AsyncClient, token: str, method: str, params: dict[str, Any]
) -> dict[str, Any]:
    request = {"jsonrpc": "2.0", "id": secrets.token_hex(8), "method": method, "params": params}
    headers = {"Authorization": f"Bearer {token}", VERSION_HEADER: A2A_VERSION}
    try:
        response = await edge.post(EDGE_RPC_PATH, json=request, headers=headers)
    except httpx.HTTPError as error:
        raise EdgeError(None, f"the edge is unreachable: {error}") from error
    if response.status_code == 401:
        raise EdgeUnauthorized()
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


async def agent_card(edge: httpx.AsyncClient, name: str) -> dict[str, Any] | None:
    try:
        response = await edge.get(f"/agents/{name}{AGENT_CARD_WELL_KNOWN_PATH}")
        card = response.json() if response.status_code == 200 else None
    except (httpx.HTTPError, ValueError):
        return None
    return card if isinstance(card, dict) else None
