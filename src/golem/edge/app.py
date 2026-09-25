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
from starlette.types import ASGIApp

from golem.edge.audit import audit_entry, record, source_ip_of
from golem.edge.auth import AuthFailure, Principal
from golem.edge.policy import Call, ChainLimits, Deny, Registry, evaluate
from golem.metrics import Instrumented, Metrics
from golem.ratelimit import Decision, Limiter, Network, Rate, client_address
from golem.run_status import RUNNING, RunStatuses, StatusUnavailable

RPC_PATH = "/a2a"
PRINCIPAL_HEADER = "X-Golem-Principal"
EDGE_TOKEN_HEADER = "X-Golem-Edge-Token"
# A delegated call's chain and root run, for the child run's admission and its task (ADR 0014).
CHAIN_HEADER = "X-Golem-Chain"
ROOT_RUN_HEADER = "X-Golem-Root-Run"
# ListTasks is scoped to the caller by the task store (the owner is the edge principal).
FORWARDED_METHODS = frozenset({"SendMessage", "GetTask", "ListTasks", "CancelTask"})
# An agent acts for its subject, whose tasks the task service would show it: delegation may
# start a task and nothing else, so an agent never reads, lists or cancels the subject's tasks.
DELEGATED_METHOD = "SendMessage"
AUDIT_CONNECT_TIMEOUT_SECONDS = 2

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
# Implementation-defined server errors, outside the -32001..-32009 range A2A reserves.
UNAUTHENTICATED = -32040
CALL_DENIED = -32041
RATE_LIMITED = -32042
AUDIT_UNAVAILABLE = INTERNAL_ERROR
# ADR 0012: per authenticated caller, and per client address for failed authentications.
CALLER_RATE = Rate(per_minute=60, burst=20)
AUTH_FAILURE_RATE = Rate(per_minute=30, burst=10)
RATE_LIMITED_REASON = "rate_limited"
RUN_NOT_ACTIVE = "run_not_active"
# An address that is not an IP (a test client, a Unix socket) shares one bucket.
UNKNOWN_ADDRESS = "unknown"

RpcId = str | int | None


@dataclass(frozen=True)
class RpcCall:
    id: RpcId
    method: str
    tenant: str
    # A message into a task that already exists, rather than a new task.
    continues_task: bool = False


@dataclass(frozen=True)
class Refusal:
    code: int
    message: str
    # The HTTP status of the refusal; JSON-RPC errors of a served call are 200.
    status_code: int = 200


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
    return RpcCall(rpc_id, method, tenant, continues_task=_names_a_task(params.get("message")))


def _names_a_task(message: object) -> bool:
    # ProtoJSON accepts both the camelCase and the original field name.
    return isinstance(message, dict) and any(message.get(k) for k in ("taskId", "task_id"))


def policy_denial(
    principal: Principal, call: RpcCall, registry: Registry, limits: ChainLimits
) -> Deny | None:
    decision = evaluate(
        Call(caller=principal.name, callee=call.tenant, chain=principal.chain), registry, limits
    )
    return decision if isinstance(decision, Deny) else None


def denial_refusal(denial: Deny) -> Refusal:
    return Refusal(CALL_DENIED, f"{denial.reason.value}: {denial.detail}")


def delegation_refusal(principal: Principal, call: RpcCall) -> Refusal | None:
    if not principal.chain:
        return None
    if call.method != DELEGATED_METHOD:
        return Refusal(CALL_DENIED, f"method_not_allowed: an agent may only {DELEGATED_METHOD}")
    if call.continues_task:
        return Refusal(CALL_DENIED, "method_not_allowed: an agent may only start a new task")
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


def forward_headers(request: Request, principal: Principal, edge_token: str) -> dict[str, str]:
    # Built from nothing, so a client's own principal, chain or edge token header never reaches
    # the task service. A delegated call runs as its subject: the child task is the person's,
    # and the agent's own name travels in the chain.
    headers = {
        "Content-Type": "application/json",
        PRINCIPAL_HEADER: principal.on_behalf_of,
        EDGE_TOKEN_HEADER: edge_token,
    }
    if principal.chain:
        headers[CHAIN_HEADER] = ",".join(principal.chain)
        headers[ROOT_RUN_HEADER] = principal.root_run_id
    version = request.headers.get(VERSION_HEADER)
    if version is not None:
        headers[VERSION_HEADER] = version
    return headers | trace_headers(request)


async def revocation_refusal(principal: Principal, statuses: RunStatuses | None) -> Refusal | None:
    """A call token is revoked once its run stops running (ASVS 10.4.9), as run tokens are at
    the MCP servers; anything that cannot be checked is refused."""
    if not principal.chain:
        return None
    if statuses is None:
        return Refusal(INTERNAL_ERROR, "run status unavailable: not configured", 503)
    status = await statuses.status_of(principal.run_id)
    if isinstance(status, StatusUnavailable):
        return Refusal(INTERNAL_ERROR, f"run status unavailable: {status.reason}", 503)
    if status != RUNNING:
        return Refusal(UNAUTHENTICATED, f"{RUN_NOT_ACTIVE}: the run is {status}", 401)
    return None


def revoked_response(rpc_id: RpcId, refusal: Refusal) -> JSONResponse:
    response = rpc_error(rpc_id, refusal, status_code=refusal.status_code)
    if refusal.status_code == 401:
        # RFC 6750: a revoked token is an invalid token.
        response.headers["WWW-Authenticate"] = 'Bearer error="invalid_token"'
    return response


def rpc_error(rpc_id: RpcId, refusal: Refusal, status_code: int = 200) -> JSONResponse:
    body = {
        "jsonrpc": "2.0",
        "id": rpc_id,
        "error": {"code": refusal.code, "message": refusal.message},
    }
    return JSONResponse(body, status_code=status_code)


def too_many(rpc_id: RpcId, decision: Decision) -> JSONResponse:
    response = rpc_error(
        rpc_id,
        Refusal(RATE_LIMITED, f"rate limited: retry after {decision.retry_after} s"),
        status_code=429,
    )
    response.headers["Retry-After"] = str(decision.retry_after)
    return response


def request_address(request: Request, trusted: tuple[Network, ...]) -> str | None:
    peer = request.client.host if request.client else None
    return client_address(peer, request.headers.getlist("x-forwarded-for"), trusted)


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
    edge_token: str,
    cards: Mapping[str, AgentCard],
    callers: Limiter | None = None,
    auth_failures: Limiter | None = None,
    trusted_proxies: tuple[Network, ...] = (),
    metrics: Metrics | None = None,
    run_statuses: RunStatuses | None = None,
) -> ASGIApp:
    callers = Limiter(CALLER_RATE) if callers is None else callers
    auth_failures = Limiter(AUTH_FAILURE_RATE) if auth_failures is None else auth_failures
    metrics = Metrics("edge") if metrics is None else metrics

    async def audited(address: str | None, principal: Principal, call: RpcCall | Rejected) -> bool:
        refusal = call.refusal if isinstance(call, Rejected) else None
        entry = audit_entry(
            principal=principal,
            callee=call.tenant,
            method=call.method,
            refusal=refusal.message if refusal else None,
            source_ip=source_ip_of(address),
        )
        try:
            async with await psycopg.AsyncConnection.connect(
                audit_dsn, autocommit=True, connect_timeout=AUDIT_CONNECT_TIMEOUT_SECONDS
            ) as conn:
                await record(conn, entry)
        except (psycopg.Error, OSError):
            metrics.audit_write_failed()
            return False
        return True

    async def a2a(request: Request) -> Response:
        address = request_address(request, trusted_proxies)
        address_key = address or UNKNOWN_ADDRESS
        # An address that keeps failing is refused before its tokens cost a verification.
        admitted = auth_failures.admits(address_key)
        if not admitted.allowed:
            metrics.rate_limit_refused("auth_failures")
            return too_many(None, admitted)
        token = bearer_token(request.headers.get("Authorization"))
        if token is None:
            auth_failures.take(address_key)
            metrics.authentication_failed()
            return unauthenticated(None)
        # Verification may refetch the identity provider's keys; that must not stall the loop.
        principal = await asyncio.to_thread(authenticate, token)
        if isinstance(principal, AuthFailure):
            auth_failures.take(address_key)
            metrics.authentication_failed()
            return unauthenticated(principal)
        body = await request.body()
        call = parse_call(body)
        decision = callers.take(principal.name)
        if not decision.allowed:
            metrics.rate_limit_refused("caller")
            # One row per streak of refusals: the first shows the caller hit the limit, the
            # rest would only grow the insert-only log at the rate of the flood.
            if decision.first_refusal:
                refused = Refusal(RATE_LIMITED, RATE_LIMITED_REASON)
                await audited(
                    address, principal, Rejected(call.id, call.method, call.tenant, refused)
                )
            return too_many(call.id, decision)
        revoked = await revocation_refusal(principal, run_statuses)
        if revoked is not None:
            if revoked.status_code == 401:
                auth_failures.take(address_key)
                metrics.authentication_failed()
            await audited(address, principal, Rejected(call.id, call.method, call.tenant, revoked))
            return revoked_response(call.id, revoked)
        if isinstance(call, RpcCall):
            denial = policy_denial(principal, call, registry, limits)
            if denial is not None:
                metrics.policy_denied(denial.reason.value)
                call = Rejected(call.id, call.method, call.tenant, denial_refusal(denial))
        if isinstance(call, RpcCall):
            refusal = delegation_refusal(principal, call)
            if refusal is not None:
                metrics.policy_denied("method_not_allowed")
                call = Rejected(call.id, call.method, call.tenant, refusal)
        if not await audited(address, principal, call):
            return rpc_error(call.id, Refusal(AUDIT_UNAVAILABLE, "audit log unavailable"))
        if isinstance(call, Rejected):
            return rpc_error(call.id, call.refusal)
        try:
            upstream = await forward.post(
                RPC_PATH, content=body, headers=forward_headers(request, principal, edge_token)
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

    app = Starlette(
        routes=[
            Route(RPC_PATH, a2a, methods=["POST"]),
            Route(f"/agents/{{name}}{AGENT_CARD_WELL_KNOWN_PATH}", agent_card, methods=["GET"]),
        ]
    )
    return Instrumented(app, routes=app.routes, metrics=metrics)


def _rpc_id(value: Any) -> RpcId:
    return value if isinstance(value, str | int) and not isinstance(value, bool) else None
