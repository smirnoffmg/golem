import hmac
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

import httpx
from a2a.auth.user import UnauthenticatedUser, User
from a2a.server.context import ServerCallContext
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import (
    DefaultServerCallContextBuilder,
    create_agent_card_routes,
    create_jsonrpc_routes,
)
from a2a.server.tasks import (
    BasePushNotificationSender,
    InMemoryTaskStore,
    PushNotificationConfigStore,
    TaskStore,
)
from a2a.types.a2a_pb2 import (
    AgentCard,
    GetTaskRequest,
    Message,
    Part,
    Role,
    SendMessageRequest,
    TaskState,
)
from a2a.utils.errors import TaskNotFoundError
from starlette.applications import Starlette
from starlette.datastructures import Headers
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route
from starlette.types import ASGIApp, Receive, Scope, Send

from golem.run_token import SigningKey, public_jwks
from golem.tasks.executor import RUN_OUTCOME, RunExecutor
from golem.tasks.ports import Orchestrator, RunOutcome

RPC_PATH = "/a2a"
# Each listener serves one kind of caller (ADR 0009): NetworkPolicy admits a caller to a port,
# not to a path, so a route on a port is reachable by every caller admitted to that port.
# The reconciler's port: the only route that changes a task without the edge.
OUTCOME_PATH = "/internal/run-outcome"
# The MCP servers' port, read-only: platform MCP servers verify run tokens against these keys.
RUN_KEYS_PATH = "/internal/run-keys"
# Platform MCP servers ask whether a run is still running before serving its token: a canceled
# run's token is refused before it expires.
RUN_STATUS_PATH = "/internal/runs/{run_id}"
OUTCOME_STATUSES = {"succeeded": True, "failed": False}
TERMINAL_STATES = {
    TaskState.TASK_STATE_COMPLETED,
    TaskState.TASK_STATE_FAILED,
    TaskState.TASK_STATE_CANCELED,
    TaskState.TASK_STATE_REJECTED,
}
PRINCIPAL_HEADER = "x-golem-principal"
EDGE_TOKEN_HEADER = "x-golem-edge-token"
# Implementation-defined JSON-RPC server error, the same code the edge uses for a refused caller.
UNAUTHENTICATED = -32040


class EdgePrincipal(User):
    def __init__(self, name: str) -> None:
        self._name = name

    @property
    def is_authenticated(self) -> bool:
        return True

    @property
    def user_name(self) -> str:
        return self._name


class EdgeContextBuilder(DefaultServerCallContextBuilder):
    # The edge has already authenticated the caller, so its principal header is trusted here
    # and nowhere else: only on the public listener, and only after edge_authenticated has
    # checked the edge's shared secret.
    def build_user(self, request: Request) -> User:
        principal = request.headers.get(PRINCIPAL_HEADER)
        if principal:
            return EdgePrincipal(principal)
        return super().build_user(request)


@dataclass(frozen=True)
class PushDelivery:
    config_store: PushNotificationConfigStore
    client: httpx.AsyncClient
    # Callers choose the push URL, so without an allowlist any caller could make the task
    # service send requests to anything it can reach inside the cluster.
    allowed_prefixes: tuple[str, ...]


def push_url_allowed(url: str, allowed_prefixes: tuple[str, ...]) -> bool:
    return any(url.startswith(prefix) for prefix in allowed_prefixes if prefix.endswith("/"))


def edge_authenticated(app: ASGIApp, edge_token: str) -> ASGIApp:
    """Serve only requests carrying the edge's shared secret; fail closed on anything else.

    NetworkPolicy already admits only the edge to the public port. The secret keeps a
    principal header from being trusted if that policy is missing, wrong or bypassed.
    """
    if not edge_token:
        raise ValueError("the edge token must not be empty")
    expected = edge_token.encode()

    async def guarded(scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            presented = Headers(scope=scope).get(EDGE_TOKEN_HEADER, "").encode("latin-1")
            if not hmac.compare_digest(presented, expected):
                refusal = JSONResponse(
                    {
                        "jsonrpc": "2.0",
                        "id": None,
                        "error": {"code": UNAUTHENTICATED, "message": "edge token required"},
                    },
                    status_code=401,
                )
                await refusal(scope, receive, send)
                return
        await app(scope, receive, send)

    return guarded


def request_handler(
    card: AgentCard,
    orchestrator: Orchestrator,
    task_store: TaskStore | None = None,
    push: PushDelivery | None = None,
) -> DefaultRequestHandler:
    push_options = {}
    if push is not None:

        async def validate(url: str) -> bool:
            return push_url_allowed(url, push.allowed_prefixes)

        push_options = {
            "push_config_store": push.config_store,
            "push_sender": BasePushNotificationSender(
                push.client, push.config_store, push_url_validator=validate
            ),
            "push_url_validator": validate,
        }
    return DefaultRequestHandler(
        agent_executor=RunExecutor(orchestrator),
        task_store=task_store or InMemoryTaskStore(),
        agent_card=card,
        **push_options,
    )


def public_app(card: AgentCard, handler: DefaultRequestHandler, edge_token: str) -> ASGIApp:
    """The edge's port: the agent card and A2A, nothing internal."""

    @asynccontextmanager
    async def lifespan(_: Starlette) -> AsyncIterator[None]:
        yield
        await handler.aclose()

    app = Starlette(
        routes=create_agent_card_routes(card)
        + create_jsonrpc_routes(handler, RPC_PATH, EdgeContextBuilder()),
        lifespan=lifespan,
    )
    return edge_authenticated(app, edge_token)


def internal_read_app(orchestrator: Orchestrator, run_keys: tuple[SigningKey, ...]) -> Starlette:
    """The MCP servers' port: run keys and run status, nothing that changes state."""
    jwks = public_jwks(run_keys)

    async def run_signing_keys(_: Request) -> Response:
        return JSONResponse(jwks)

    async def run_status(request: Request) -> Response:
        run_id = request.path_params["run_id"]
        status = await orchestrator.status(run_id)
        if status is None:
            return JSONResponse({"error": "run not found"}, status_code=404)
        return JSONResponse({"run_id": run_id, "status": status})

    return Starlette(
        routes=[
            Route(RUN_KEYS_PATH, run_signing_keys, methods=["GET"]),
            Route(RUN_STATUS_PATH, run_status, methods=["GET"]),
        ]
    )


def internal_write_app(handler: DefaultRequestHandler) -> Starlette:
    """The reconciler's port: run outcomes only."""

    async def run_outcome(request: Request) -> Response:
        body = await request.json()
        if body.get("status") not in OUTCOME_STATUSES:
            return JSONResponse({"error": "status must be succeeded or failed"}, status_code=422)
        outcome = RunOutcome(
            run_id=body["run_id"],
            succeeded=OUTCOME_STATUSES[body["status"]],
            detail=body.get("detail") or f"Run {body['run_id']} {body['status']}.",
        )
        caller = body.get("caller") or ""
        context = ServerCallContext(
            user=EdgePrincipal(caller) if caller else UnauthenticatedUser(),
            tenant=body["tenant"],
            state={RUN_OUTCOME: outcome},
        )
        try:
            task = await handler.on_get_task(
                GetTaskRequest(tenant=body["tenant"], id=body["task_id"]), context
            )
        except TaskNotFoundError:
            task = None
        if task is None:
            return JSONResponse({"error": "task not found"}, status_code=404)
        # Outcomes are delivered at least once; a task that already ended stays as it is.
        if task.status.state in TERMINAL_STATES:
            return JSONResponse({"task_id": body["task_id"]})
        try:
            await handler.on_message_send(
                SendMessageRequest(
                    tenant=body["tenant"],
                    message=Message(
                        message_id=f"run-outcome-{outcome.run_id}-{body['task_id']}",
                        task_id=body["task_id"],
                        # A restarted service has no live task to infer the context from.
                        context_id=task.context_id,
                        role=Role.ROLE_USER,
                        parts=[Part(text=outcome.detail)],
                    ),
                ),
                context,
            )
        except TaskNotFoundError:
            return JSONResponse({"error": "task not found"}, status_code=404)
        return JSONResponse({"task_id": body["task_id"]})

    return Starlette(routes=[Route(OUTCOME_PATH, run_outcome, methods=["POST"])])


@dataclass(frozen=True)
class Listeners:
    public: ASGIApp
    internal_read: Starlette
    internal_write: Starlette


def create_listeners(
    card: AgentCard,
    orchestrator: Orchestrator,
    *,
    edge_token: str,
    task_store: TaskStore | None = None,
    push: PushDelivery | None = None,
    run_keys: tuple[SigningKey, ...] = (),
) -> Listeners:
    """One request handler behind three listeners; the public one owns its lifespan."""
    handler = request_handler(card, orchestrator, task_store, push)
    return Listeners(
        public=public_app(card, handler, edge_token),
        internal_read=internal_read_app(orchestrator, run_keys),
        internal_write=internal_write_app(handler),
    )
