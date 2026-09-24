import asyncio
import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

import httpx
import psycopg
from a2a.server.request_handlers.response_helpers import agent_card_to_dict
from a2a.types.a2a_pb2 import AgentCard
from a2a.utils.constants import AGENT_CARD_WELL_KNOWN_PATH, VERSION_HEADER
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from golem.edge.audit import audit_entry, record, source_ip_of
from golem.edge.auth import AuthFailure, Principal
from golem.edge.policy import Call, ChainLimits, Deny, Registry, evaluate

RPC_PATH = "/a2a"
PRINCIPAL_HEADER = "X-Golem-Principal"
FORWARDED_METHODS = frozenset({"SendMessage", "GetTask", "CancelTask"})
AUDIT_CONNECT_TIMEOUT_SECONDS = 2

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
# Implementation-defined server errors, outside the -32001..-32009 range A2A reserves.
UNAUTHENTICATED = -32040
CALL_DENIED = -32041
AUDIT_UNAVAILABLE = INTERNAL_ERROR

RpcId = str | int | None


@dataclass(frozen=True)
class RpcCall:
    id: RpcId
    method: str
    tenant: str


@dataclass(frozen=True)
class Refusal:
    code: int
    message: str


@dataclass(frozen=True)
class Rejected:
    id: RpcId
    method: str
    tenant: str
    refusal: Refusal


def bearer_token(authorization: str | None) -> str | None:
    scheme, _, token = (authorization or "").partition(" ")
    token = token.strip()
    return token if scheme.lower() == "bearer" and token else None


def parse_call(body: bytes) -> RpcCall | Rejected:
    try:
        request = json.loads(body)
    except ValueError:
        return Rejected(None, "", "", Refusal(PARSE_ERROR, "parse error"))
    if not isinstance(request, dict):
        return Rejected(None, "", "", Refusal(INVALID_REQUEST, "request must be a JSON object"))
    rpc_id = _rpc_id(request.get("id"))
    method = request.get("method")
    if request.get("jsonrpc") != "2.0" or not isinstance(method, str):
        return Rejected(rpc_id, "", "", Refusal(INVALID_REQUEST, "invalid JSON-RPC request"))
    params = request.get("params")
    tenant = params.get("tenant") if isinstance(params, dict) else None
    tenant = tenant if isinstance(tenant, str) else ""
    if method not in FORWARDED_METHODS:
        refusal = Refusal(METHOD_NOT_FOUND, f"method {method!r} is not supported")
        return Rejected(rpc_id, method, tenant, refusal)
    if not isinstance(params, dict):
        return Rejected(rpc_id, method, "", Refusal(INVALID_PARAMS, "params must be an object"))
    if not tenant:
        refusal = Refusal(INVALID_PARAMS, "params.tenant must name the called agent")
        return Rejected(rpc_id, method, "", refusal)
    return RpcCall(rpc_id, method, tenant)


def policy_refusal(
    principal: Principal, call: RpcCall, registry: Registry, limits: ChainLimits
) -> Refusal | None:
    decision = evaluate(
        Call(caller=principal.name, callee=call.tenant, chain=principal.chain), registry, limits
    )
    if isinstance(decision, Deny):
        return Refusal(CALL_DENIED, f"{decision.reason.value}: {decision.detail}")
    return None


TRACEPARENT = re.compile(r"^[0-9a-f]{2}-[0-9a-f]{32}-[0-9a-f]{16}-[0-9a-f]{2}$")
TRACESTATE_MAX = 512


def trace_headers(request: Request) -> dict[str, str]:
    """W3C Trace Context, so a run joins its caller's trace; only well-formed values pass."""
    traceparent = request.headers.get("traceparent", "")
    if not TRACEPARENT.fullmatch(traceparent):
        return {}
    headers = {"traceparent": traceparent}
    tracestate = request.headers.get("tracestate", "")
    if tracestate and len(tracestate) <= TRACESTATE_MAX and tracestate.isascii():
        headers["tracestate"] = tracestate
    return headers


def forward_headers(request: Request, principal: Principal) -> dict[str, str]:
    headers = {"Content-Type": "application/json", PRINCIPAL_HEADER: principal.name}
    version = request.headers.get(VERSION_HEADER)
    if version is not None:
        headers[VERSION_HEADER] = version
    return headers | trace_headers(request)


def rpc_error(rpc_id: RpcId, refusal: Refusal, status_code: int = 200) -> JSONResponse:
    body = {
        "jsonrpc": "2.0",
        "id": rpc_id,
        "error": {"code": refusal.code, "message": refusal.message},
    }
    return JSONResponse(body, status_code=status_code)


def unauthenticated(failure: AuthFailure | None) -> JSONResponse:
    response = rpc_error(
        None,
        Refusal(UNAUTHENTICATED, failure.reason if failure else "bearer token required"),
        status_code=401,
    )
    response.headers["WWW-Authenticate"] = (
        'Bearer error="invalid_token"' if failure else 'Bearer realm="golem"'
    )
    return response


def create_edge_app(
    *,
    authenticate: Callable[[str], Principal | AuthFailure],
    registry: Registry,
    limits: ChainLimits,
    audit_dsn: str,
    forward: httpx.AsyncClient,
    cards: Mapping[str, AgentCard],
) -> Starlette:
    async def audited(request: Request, principal: Principal, call: RpcCall | Rejected) -> bool:
        refusal = call.refusal if isinstance(call, Rejected) else None
        entry = audit_entry(
            principal=principal,
            callee=call.tenant,
            method=call.method,
            refusal=refusal.message if refusal else None,
            source_ip=source_ip_of(request.client.host if request.client else None),
        )
        try:
            async with await psycopg.AsyncConnection.connect(
                audit_dsn, autocommit=True, connect_timeout=AUDIT_CONNECT_TIMEOUT_SECONDS
            ) as conn:
                await record(conn, entry)
        except (psycopg.Error, OSError):
            return False
        return True

    async def a2a(request: Request) -> Response:
        token = bearer_token(request.headers.get("Authorization"))
        if token is None:
            return unauthenticated(None)
        # Verification may refetch the identity provider's keys; that must not stall the loop.
        principal = await asyncio.to_thread(authenticate, token)
        if isinstance(principal, AuthFailure):
            return unauthenticated(principal)
        body = await request.body()
        call = parse_call(body)
        if isinstance(call, RpcCall):
            refusal = policy_refusal(principal, call, registry, limits)
            if refusal is not None:
                call = Rejected(call.id, call.method, call.tenant, refusal)
        if not await audited(request, principal, call):
            return rpc_error(call.id, Refusal(AUDIT_UNAVAILABLE, "audit log unavailable"))
        if isinstance(call, Rejected):
            return rpc_error(call.id, call.refusal)
        try:
            upstream = await forward.post(
                RPC_PATH, content=body, headers=forward_headers(request, principal)
            )
        except httpx.HTTPError:
            return rpc_error(call.id, Refusal(INTERNAL_ERROR, "task service unavailable"))
        return Response(
            upstream.content,
            status_code=upstream.status_code,
            media_type=upstream.headers.get("content-type"),
        )

    async def agent_card(request: Request) -> Response:
        card = cards.get(request.path_params["name"])
        if card is None:
            return JSONResponse({"error": "unknown agent"}, status_code=404)
        return JSONResponse(agent_card_to_dict(card))

    return Starlette(
        routes=[
            Route(RPC_PATH, a2a, methods=["POST"]),
            Route(f"/agents/{{name}}{AGENT_CARD_WELL_KNOWN_PATH}", agent_card, methods=["GET"]),
        ]
    )


def _rpc_id(value: Any) -> RpcId:
    return value if isinstance(value, str | int) and not isinstance(value, bool) else None
