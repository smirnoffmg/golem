import asyncio
import hashlib
import json
import logging
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx
import psycopg
from a2a.server.request_handlers.response_helpers import agent_card_to_dict
from a2a.types.a2a_pb2 import AgentCard
from a2a.utils.constants import AGENT_CARD_WELL_KNOWN_PATH, VERSION_HEADER
from starlette.applications import Starlette
from starlette.datastructures import MutableHeaders
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from golem.edge.audit import (
    AuditEntry,
    audit_entry,
    directory_entry,
    record,
    resolution_entry,
    source_ip_of,
)
from golem.edge.auth import AuthFailure, Principal
from golem.edge.card_signing import KEYS_PATH
from golem.edge.policy import Call, ChainLimits, Deny, Registry, callable_agents, evaluate
from golem.metrics import Instrumented, Metrics
from golem.ratelimit import (
    AUTH_FAILURE_RATE,
    CALLER_RATE,
    DIRECTORY_RATE,
    Decision,
    Limiter,
    Network,
    address_key,
    client_address,
)
from golem.resolution import ACTIONS as RESOLUTION_ACTIONS
from golem.resolution import MAX_REASON_CHARS, RERUN
from golem.run_status import RUNNING, RunStatuses, StatusUnavailable

RPC_PATH = "/a2a"
DIRECTORY_PATH = "/agents"
# A process's owner answers a process waiting for a reason (ADR 0019); not an A2A method.
RESOLUTION_PATH = "/processes/{task_id}/resolution"
MAX_RESOLUTION_BYTES = 16 * 1024
TASK_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
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
# golem_edge's CONNECTION LIMIT (10) is shared by every replica, a surge pod during a rollout
# included: three replicas of three stay under it. A call past its replica's share waits for a
# connection as long as it would for the database, then is refused as the audit's failure.
AUDIT_CONNECTIONS = 3
# Far above any A2A message a person or an agent writes; the body is buffered before the
# caller's rate limit is taken, so without a bound one caller could hold gigabytes per request.
MAX_BODY_BYTES = 1024 * 1024

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
RATE_LIMITED_REASON = "rate_limited"
RUN_NOT_ACTIVE = "run_not_active"

RpcId = str | int | None

log = logging.getLogger("golem.edge")

# A2A 1.0, 8.6.1: card endpoints "SHOULD include a Cache-Control response header with a
# max-age directive" and "an ETag". The directory changes as rarely, but only with a restart.
CARD_MAX_AGE_SECONDS = 300
DIRECTORY_MAX_AGE_SECONDS = 60
EMPTY_KEY_SET: Mapping[str, Any] = {"keys": []}
# JSON only, never rendered: nothing may load, frame or be sniffed from an edge response.
CONTENT_SECURITY_POLICY = "default-src 'none'; frame-ancestors 'none'"
HSTS = "max-age=31536000; includeSubDomains"


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


async def read_body(request: Request, limit: int = MAX_BODY_BYTES) -> bytes | None:
    """The body, or None as soon as it passes `limit`: the rest is never read."""
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > limit:
        return None
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > limit:
            return None
    return bytes(body)


def parse_call(body: bytes | None) -> RpcCall | Rejected:
    if body is None:
        refusal = Refusal(INVALID_REQUEST, f"request body exceeds {MAX_BODY_BYTES} bytes", 413)
        return Rejected(None, "", "", refusal)
    try:
        request = json.loads(body)
    # Deep nesting exhausts the parser's recursion, not its grammar.
    except (ValueError, RecursionError):
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


def card_url(base_url: str, name: str) -> str:
    return f"{base_url}/agents/{name}{AGENT_CARD_WELL_KNOWN_PATH}"


def directory_of(
    cards: Mapping[str, AgentCard], names: Iterable[str], base_url: str
) -> dict[str, Any]:
    return {
        "agents": [
            {
                "name": name,
                "description": cards[name].description,
                "card_url": card_url(base_url, name),
                "skills": [skill.id for skill in cards[name].skills],
            }
            for name in names
        ]
    }


def entity_tag(body: bytes) -> str:
    return f'"{hashlib.sha256(body).hexdigest()[:32]}"'


def matches(if_none_match: str | None, tag: str) -> bool:
    if if_none_match is None:
        return False
    tags = {t.strip().removeprefix("W/") for t in if_none_match.split(",")}
    return tag in tags or "*" in tags


def public_json(request: Request, body: bytes, max_age: int) -> Response:
    """The same bytes for everyone: cacheable anywhere, revalidated by its entity tag."""
    tag = entity_tag(body)
    headers = {"ETag": tag, "Cache-Control": f"public, max-age={max_age}"}
    if matches(request.headers.get("if-none-match"), tag):
        return Response(status_code=304, headers=headers)
    return Response(body, media_type="application/json", headers=headers)


def json_bytes(content: Any) -> bytes:
    return bytes(JSONResponse(content).body)


def plain_error(message: str, status_code: int, **headers: str) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status_code, headers=headers)


def plain_too_many(decision: Decision) -> JSONResponse:
    return plain_error(
        f"rate limited: retry after {decision.retry_after} s",
        429,
        **{"Retry-After": str(decision.retry_after)},
    )


def plain_unauthenticated(failure: AuthFailure | None) -> JSONResponse:
    challenge = 'Bearer error="invalid_token"' if failure else 'Bearer realm="golem"'
    return plain_error(
        failure.reason if failure else "bearer token required",
        401,
        **{"WWW-Authenticate": challenge},
    )


def edge_headers(app: ASGIApp, *, hsts: bool) -> ASGIApp:
    """ASVS 5.0, 3.4 on every response, refusals included; a response that set no cache
    policy of its own is never stored, since most carry a caller's tasks or refusals."""

    async def wrapped(scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers["X-Content-Type-Options"] = "nosniff"
                headers["Content-Security-Policy"] = CONTENT_SECURITY_POLICY
                headers["Referrer-Policy"] = "no-referrer"
                if "cache-control" not in headers:
                    headers["Cache-Control"] = "no-store"
                if hsts:
                    headers["Strict-Transport-Security"] = HSTS
            await send(message)

        await app(scope, receive, send_with_headers)

    return wrapped


def create_edge_app(
    *,
    authenticate: Callable[[str], Principal | AuthFailure],
    registry: Registry,
    limits: ChainLimits,
    audit_dsn: str,
    forward: httpx.AsyncClient,
    edge_token: str,
    cards: Mapping[str, AgentCard],
    card_keys: Mapping[str, Any] = EMPTY_KEY_SET,
    public_base_url: str = "",
    callers: Limiter | None = None,
    auth_failures: Limiter | None = None,
    directory: Limiter | None = None,
    trusted_proxies: tuple[Network, ...] = (),
    metrics: Metrics | None = None,
    run_statuses: RunStatuses | None = None,
    audit_connections: int = AUDIT_CONNECTIONS,
) -> ASGIApp:
    """``cards`` are served as given, so they arrive signed (``golem.edge.card_signing``), and
    ``card_keys`` is the JWKS that verifies them."""
    callers = Limiter(CALLER_RATE) if callers is None else callers
    auth_failures = Limiter(AUTH_FAILURE_RATE) if auth_failures is None else auth_failures
    directory = Limiter(DIRECTORY_RATE) if directory is None else directory
    metrics = Metrics("edge") if metrics is None else metrics
    card_bodies = {name: json_bytes(agent_card_to_dict(card)) for name, card in cards.items()}
    key_set_body = json_bytes(dict(card_keys))
    published = tuple(sorted(cards))
    audit_slots = asyncio.Semaphore(audit_connections)

    async def audited(address: str | None, principal: Principal, call: RpcCall | Rejected) -> bool:
        refusal = call.refusal if isinstance(call, Rejected) else None
        entry = audit_entry(
            principal=principal,
            callee=call.tenant,
            method=call.method,
            refusal=refusal.message if refusal else None,
            source_ip=source_ip_of(address),
        )
        return await written(entry)

    async def written(entry: AuditEntry) -> bool:
        try:
            await asyncio.wait_for(audit_slots.acquire(), AUDIT_CONNECT_TIMEOUT_SECONDS)
        except TimeoutError:
            metrics.audit_write_failed()
            return False
        try:
            async with await psycopg.AsyncConnection.connect(
                audit_dsn, autocommit=True, connect_timeout=AUDIT_CONNECT_TIMEOUT_SECONDS
            ) as conn:
                await record(conn, entry)
        except (psycopg.Error, OSError):
            metrics.audit_write_failed()
            return False
        finally:
            audit_slots.release()
        return True

    async def verified(request: Request, key: str) -> Principal | AuthFailure | Decision | None:
        """The caller, a failure (None: no bearer token), or the address's refusal."""
        # An address that keeps failing is refused before its tokens cost a verification.
        admitted = auth_failures.admits(key)
        if not admitted.allowed:
            metrics.rate_limit_refused("auth_failures")
            return admitted
        token = bearer_token(request.headers.get("Authorization"))
        if token is None:
            auth_failures.take(key)
            metrics.authentication_failed()
            return None
        # Verification may refetch the identity provider's keys; that must not stall the loop.
        principal = await asyncio.to_thread(authenticate, token)
        if isinstance(principal, AuthFailure):
            auth_failures.take(key)
            metrics.authentication_failed()
        return principal

    async def a2a(request: Request) -> Response:
        address = request_address(request, trusted_proxies)
        outcome = await verified(request, address_key(address))
        if isinstance(outcome, Decision):
            return too_many(None, outcome)
        if not isinstance(outcome, Principal):
            return unauthenticated(outcome)
        principal = outcome
        key = address_key(address)
        body = await read_body(request)
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
                auth_failures.take(key)
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
            return rpc_error(call.id, call.refusal, status_code=call.refusal.status_code)
        try:
            upstream = await forward.post(
                RPC_PATH, content=body, headers=forward_headers(request, principal, edge_token)
            )
        except httpx.HTTPError:
            return rpc_error(call.id, Refusal(INTERNAL_ERROR, "task service unavailable"))
        if upstream.status_code == 401:
            # The task service checks only the edge's own token, never the caller's: this is
            # the edge misconfigured, and passing the 401 on would sign the UI's users out.
            log.error("the task service refused the edge token; check GOLEM_EDGE_TOKEN")
            refusal = Refusal(INTERNAL_ERROR, "task service refused the edge")
            return rpc_error(call.id, refusal, status_code=502)
        return Response(
            upstream.content,
            status_code=upstream.status_code,
            media_type=upstream.headers.get("content-type"),
        )

    async def agent_card(request: Request) -> Response:
        # Not limited per address: the UI fetches every card for every user from its own.
        body = card_bodies.get(request.path_params["name"])
        if body is None:
            return plain_error("unknown agent", 404)
        return public_json(request, body, CARD_MAX_AGE_SECONDS)

    def anonymous_refusal(request: Request) -> Response | None:
        admitted = directory.take(address_key(request_address(request, trusted_proxies)))
        if admitted.allowed:
            return None
        metrics.rate_limit_refused("directory")
        return plain_too_many(admitted)

    async def key_set(request: Request) -> Response:
        return anonymous_refusal(request) or public_json(
            request, key_set_body, CARD_MAX_AGE_SECONDS
        )

    async def agents(request: Request) -> Response:
        if "authorization" not in request.headers:
            return anonymous_refusal(request) or listing(published, public=True)
        address = request_address(request, trusted_proxies)
        outcome = await verified(request, address_key(address))
        if isinstance(outcome, Decision):
            return plain_too_many(outcome)
        if not isinstance(outcome, Principal):
            return plain_unauthenticated(outcome)
        principal = outcome
        source_ip = source_ip_of(address)
        decision = callers.take(principal.name)
        if not decision.allowed:
            metrics.rate_limit_refused("caller")
            if decision.first_refusal:
                await written(
                    directory_entry(
                        principal=principal,
                        listed=(),
                        refusal=RATE_LIMITED_REASON,
                        source_ip=source_ip,
                    )
                )
            return plain_too_many(decision)
        revoked = await revocation_refusal(principal, run_statuses)
        if revoked is not None:
            if revoked.status_code == 401:
                auth_failures.take(address_key(address))
                metrics.authentication_failed()
            await written(
                directory_entry(
                    principal=principal, listed=(), refusal=revoked.message, source_ip=source_ip
                )
            )
            response = plain_error(revoked.message, revoked.status_code)
            if revoked.status_code == 401:
                response.headers["WWW-Authenticate"] = 'Bearer error="invalid_token"'
            return response
        names = callable_agents(principal.name, principal.chain, published, registry, limits)
        entry = directory_entry(
            principal=principal, listed=names, refusal=None, source_ip=source_ip
        )
        if not await written(entry):
            return plain_error("audit log unavailable", 503)
        return listing(names, public=False)

    async def resolution(request: Request) -> Response:
        """Only a person decides; the task service checks it is the process task's owner."""
        address = request_address(request, trusted_proxies)
        outcome = await verified(request, address_key(address))
        if isinstance(outcome, Decision):
            return plain_too_many(outcome)
        if not isinstance(outcome, Principal):
            return plain_unauthenticated(outcome)
        principal = outcome
        task_id = request.path_params["task_id"]
        body = await read_body(request, MAX_RESOLUTION_BYTES)
        action, reason, problem = resolution_of(body)
        decision = callers.take(principal.name)
        if not decision.allowed:
            metrics.rate_limit_refused("caller")
            return plain_too_many(decision)
        refusal: tuple[str, int] | None = None
        if principal.chain:
            refusal = ("agents_do_not_decide", 403)
        elif not TASK_ID.fullmatch(task_id):
            refusal = ("not_found", 404)
        elif problem is not None:
            refusal = (problem, 400)
        entry = resolution_entry(
            principal=principal,
            task_id=task_id if TASK_ID.fullmatch(task_id) else "",
            action=action or "",
            refusal=refusal[0] if refusal else None,
            source_ip=source_ip_of(address),
        )
        if not await written(entry):
            return plain_error("audit log unavailable", 503)
        if refusal is not None:
            return plain_error(*refusal)
        payload = {"action": action} | ({"reason": reason} if reason is not None else {})
        try:
            upstream = await forward.post(
                f"/processes/{task_id}/resolution",
                json=payload,
                headers=forward_headers(request, principal, edge_token),
            )
        except httpx.HTTPError:
            return plain_error("task service unavailable", 502)
        if upstream.status_code == 401:
            log.error("the task service refused the edge token; check GOLEM_EDGE_TOKEN")
            return plain_error("task service refused the edge", 502)
        return Response(
            upstream.content,
            status_code=upstream.status_code,
            media_type=upstream.headers.get("content-type"),
        )

    def listing(names: tuple[str, ...], *, public: bool) -> Response:
        # One URL, two answers: a cache must key on Authorization and never share a caller's.
        scope = "public" if public else "private"
        return JSONResponse(
            directory_of(cards, names, public_base_url),
            headers={
                "Cache-Control": f"{scope}, max-age={DIRECTORY_MAX_AGE_SECONDS}",
                "Vary": "Authorization",
            },
        )

    app = Starlette(
        routes=[
            Route(RPC_PATH, a2a, methods=["POST"]),
            Route(DIRECTORY_PATH, agents, methods=["GET"]),
            Route(RESOLUTION_PATH, resolution, methods=["POST"]),
            Route(f"/agents/{{name}}{AGENT_CARD_WELL_KNOWN_PATH}", agent_card, methods=["GET"]),
            Route(KEYS_PATH, key_set, methods=["GET"]),
        ]
    )
    headed = edge_headers(app, hsts=urlsplit(public_base_url).scheme == "https")
    return Instrumented(headed, routes=app.routes, metrics=metrics)


def resolution_of(body: bytes | None) -> tuple[str | None, str | None, str | None]:
    """The action and the reason of a resolution, or what is wrong with it."""
    try:
        parsed = json.loads(body) if body is not None else None
    except (ValueError, RecursionError):
        parsed = None
    if not isinstance(parsed, dict) or parsed.get("action") not in RESOLUTION_ACTIONS:
        return None, None, "malformed"
    action, reason = parsed["action"], parsed.get("reason")
    if reason is not None and not isinstance(reason, str):
        return action, None, "malformed"
    if action == RERUN and not (reason and len(reason) <= MAX_REASON_CHARS):
        return action, None, "reason_required"
    return action, reason if action == RERUN else None, None


def _rpc_id(value: Any) -> RpcId:
    return value if isinstance(value, str | int) and not isinstance(value, bool) else None
