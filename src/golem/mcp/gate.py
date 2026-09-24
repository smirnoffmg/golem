"""The gate in front of a platform MCP server: authenticate, authorize, audit, then serve.

Every HTTP request is decided here before the MCP server sees it, because the audit row must
name the operation and its arguments, which only the JSON-RPC message carries: the SDK's own
bearer middleware decides on the token alone and answers without a hook for the audit. The
order is fixed: token, grant, run status, message. A refusal and an allowed request are both
audited, and a request whose row cannot be written is refused.
"""

import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from mcp.server.transport_security import DEFAULT_MAX_REQUEST_BODY_SIZE
from starlette.datastructures import Headers
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from golem.edge.app import bearer_token
from golem.edge.audit import source_ip_of
from golem.mcp.audit import Operation, audit_entry, written
from golem.mcp.auth import Refusal, RunStatuses, grant_refusal, status_refusal
from golem.mcp.groups import Group
from golem.run_token import RunClaims, RunTokenError

REALM = "golem-mcp"
TOOLS_CALL = "tools/call"
AUDIT_UNAVAILABLE = Refusal(503, "temporarily_unavailable", "audit log unavailable")
TOO_LARGE = Refusal(413, "invalid_request", "request body too large")
NOT_JSON_RPC = Refusal(400, "invalid_request", "the body is not one JSON-RPC message")


@dataclass(frozen=True)
class Gate:
    group: Group
    target_system: str
    verify: Callable[[str], RunClaims | RunTokenError]
    statuses: RunStatuses
    audit_dsn: str


class _TooLarge:
    pass


def operation_of(http_method: str, body: bytes | _TooLarge) -> Operation:
    if isinstance(body, _TooLarge):
        return Operation(method=http_method, problem=TOO_LARGE)
    if not body:
        return Operation(method=http_method)
    try:
        message = json.loads(body)
    except ValueError:
        return Operation(method=http_method, problem=NOT_JSON_RPC)
    if not isinstance(message, dict) or not isinstance(message.get("method"), str):
        return Operation(method=http_method, problem=NOT_JSON_RPC)
    method = message["method"]
    params = message.get("params") if isinstance(message.get("params"), dict) else {}
    if method != TOOLS_CALL:
        return Operation(method=method)
    tool = params.get("name")
    arguments = params.get("arguments")
    return Operation(
        method=method,
        tool=tool if isinstance(tool, str) and tool else "<unnamed>",
        arguments=arguments if isinstance(arguments, dict) else {},
    )


def operation_refusal(operation: Operation, group: Group) -> Refusal | None:
    if operation.problem is not None:
        return operation.problem
    if operation.tool is not None and operation.tool not in group.tools:
        return Refusal(
            403, "forbidden", f"tool {operation.tool!r} is not in tool group {group.name!r}"
        )
    return None


async def decide(
    gate: Gate, token: str | None, operation: Operation
) -> tuple[RunClaims | None, Refusal | None]:
    if token is None:
        return None, Refusal(401, "", "bearer token required")
    # Verification may refetch the signing keys over HTTP, synchronously.
    verified = await asyncio.to_thread(gate.verify, token)
    if isinstance(verified, RunTokenError):
        return None, Refusal(401, "invalid_token", verified.reason)
    refusal = grant_refusal(verified, gate.group)
    if refusal is None:
        refusal = status_refusal(await gate.statuses.status_of(verified.run_id))
    if refusal is None:
        refusal = operation_refusal(operation, gate.group)
    return verified, refusal


def refusal_response(refusal: Refusal, group: Group) -> Response:
    response = JSONResponse(
        {"error": refusal.error or "unauthorized", "error_description": refusal.reason},
        status_code=refusal.status_code,
    )
    # RFC 6750 section 3: no error code when the request had no credentials at all.
    if refusal.status_code == 401:
        response.headers["WWW-Authenticate"] = (
            f'Bearer error="{refusal.error}"' if refusal.error else f'Bearer realm="{REALM}"'
        )
    elif refusal.error == "insufficient_scope":
        response.headers["WWW-Authenticate"] = (
            f'Bearer error="insufficient_scope", scope="{group.name}"'
        )
    return response


async def read_body(receive: Receive, limit: int) -> bytes | _TooLarge | None:
    chunks: list[bytes] = []
    size = 0
    while True:
        message = await receive()
        if message["type"] == "http.disconnect":
            return None
        chunk = message.get("body", b"")
        size += len(chunk)
        if size > limit:
            return _TooLarge()
        chunks.append(chunk)
        if not message.get("more_body", False):
            return b"".join(chunks)


def replaying(body: bytes, receive: Receive) -> Receive:
    replayed = False

    async def replay() -> Message:
        nonlocal replayed
        if replayed:
            return await receive()
        replayed = True
        return {"type": "http.request", "body": body, "more_body": False}

    return replay


def gated(app: ASGIApp, gate: Gate) -> ASGIApp:
    async def guarded(scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await app(scope, receive, send)
            return
        body = await read_body(receive, DEFAULT_MAX_REQUEST_BODY_SIZE)
        if body is None:
            return
        operation = operation_of(scope["method"], body)
        token = bearer_token(Headers(scope=scope).get("authorization"))
        claims, refusal = await decide(gate, token, operation)
        entry = audit_entry(
            group=gate.group,
            target_system=gate.target_system,
            operation=operation,
            token=token,
            claims=claims,
            refusal=refusal,
            source_ip=source_ip_of(client_host(scope)),
        )
        if not await written(gate.audit_dsn, entry):
            refusal = AUDIT_UNAVAILABLE
        if refusal is not None:
            await refusal_response(refusal, gate.group)(scope, receive, send)
            return
        assert isinstance(body, bytes)
        await app(scope, replaying(body, receive), send)

    return guarded


def client_host(scope: Scope) -> str | None:
    client: Any = scope.get("client")
    return client[0] if client else None
