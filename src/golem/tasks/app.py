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

from golem.metrics import Instrumented, Metrics
from golem.run_token import SigningKey, public_jwks
from golem.tasks.executor import ANONYMOUS, RUN_OUTCOME, RunExecutor
from golem.tasks.ports import Orchestrator

RPC_PATH = "/a2a"
# Each listener serves one kind of caller (ADR 0009): NetworkPolicy admits a caller to a port,
# not to a path, so a route on a port is reachable by every caller admitted to that port.
# The reconciler's port: the only route that changes a task without the edge, and only to the
# outcome golem_runs holds for it.
OUTCOME_PATH = "/internal/run-outcome"
# The MCP servers' port, read-only: platform MCP servers verify run tokens against these keys.
RUN_KEYS_PATH = "/internal/run-keys"
# Platform MCP servers ask whether a run is still running before serving its token: a canceled
# run's token is refused before it expires.
RUN_STATUS_PATH = "/internal/runs/{run_id}"
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


def edge_authenticated(app: ASGIApp, edge_token: str, metrics: Metrics | None = None) -> ASGIApp:
    """Serve only requests carrying the edge's shared secret; fail closed on anything else.

    NetworkPolicy already admits only the edge to the public port. The secret keeps a
    principal header from being trusted if that policy is missing, wrong or bypassed.
    """
    if not edge_token:
        raise ValueError("the edge token must not be empty")
    expected = edge_token.encode()
    metrics = Metrics("tasks") if metrics is None else metrics

    async def guarded(scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            presented = Headers(scope=scope).get(EDGE_TOKEN_HEADER, "").encode("latin-1")
            if not hmac.compare_digest(presented, expected):
                metrics.authentication_failed()
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


def public_app(
    card: AgentCard, handler: DefaultRequestHandler, edge_token: str, metrics: Metrics
) -> ASGIApp:
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
    return Instrumented(
        edge_authenticated(app, edge_token, metrics), routes=app.routes, metrics=metrics
    )


def internal_read_app(
    orchestrator: Orchestrator, run_keys: tuple[SigningKey, ...], metrics: Metrics
) -> ASGIApp:
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

    app = Starlette(
        routes=[
            Route(RUN_KEYS_PATH, run_signing_keys, methods=["GET"]),
            Route(RUN_STATUS_PATH, run_status, methods=["GET"]),
        ]
    )
    return Instrumented(app, routes=app.routes, metrics=metrics)


def user_of(caller: str) -> User:
    # The inverse of caller_of: the task store keys a task by the name of the user who sent it.
    return UnauthenticatedUser() if caller == ANONYMOUS else EdgePrincipal(caller)


def internal_write_app(
    handler: DefaultRequestHandler, orchestrator: Orchestrator, metrics: Metrics
) -> ASGIApp:
    """The reconciler's port: notifications that a task's run has a final outcome.

    The body only names the task. Its outcome, and the caller and agent that address it in the
    task store, are read from golem_runs, the system of record, so whoever can reach this port
    can at most make a true outcome arrive early, never a false one (ADR 0009).
    """

    async def run_outcome(request: Request) -> Response:
        try:
            body = await request.json()
        except ValueError:
            body = None
        task_id = body.get("task_id") if isinstance(body, dict) else None
        if not isinstance(task_id, str) or not task_id:
            return JSONResponse({"error": "task_id is required"}, status_code=422)
        run = await orchestrator.run_of_task(task_id)
        if run is None:
            return JSONResponse({"error": "task not found"}, status_code=404)
        if run.outcome is None:
            # Running, or canceled (the task was canceled through A2A already): nothing to
            # deliver. Not a 200, so a notifier that got here early retries.
            return JSONResponse(
                {"error": "the task's run has no final outcome yet"}, status_code=409
            )
        outcome = run.outcome
        context = ServerCallContext(
            user=user_of(run.caller), tenant=run.agent, state={RUN_OUTCOME: outcome}
        )
        try:
            task = await handler.on_get_task(GetTaskRequest(tenant=run.agent, id=task_id), context)
        except TaskNotFoundError:
            task = None
        if task is None:
            return JSONResponse({"error": "task not found"}, status_code=404)
        # Outcomes are delivered at least once; a task that already ended stays as it is.
        if task.status.state in TERMINAL_STATES:
            return JSONResponse({"task_id": task_id})
        try:
            await handler.on_message_send(
                SendMessageRequest(
                    tenant=run.agent,
                    message=Message(
                        message_id=f"run-outcome-{outcome.run_id}-{task_id}",
                        task_id=task_id,
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
        return JSONResponse({"task_id": task_id})

    app = Starlette(routes=[Route(OUTCOME_PATH, run_outcome, methods=["POST"])])
    return Instrumented(app, routes=app.routes, metrics=metrics)


@dataclass(frozen=True)
class Listeners:
    public: ASGIApp
    internal_read: ASGIApp
    internal_write: ASGIApp


def create_listeners(
    card: AgentCard,
    orchestrator: Orchestrator,
    *,
    edge_token: str,
    task_store: TaskStore | None = None,
    push: PushDelivery | None = None,
    run_keys: tuple[SigningKey, ...] = (),
    metrics: Metrics | None = None,
) -> Listeners:
    """One request handler behind three listeners; the public one owns its lifespan. All
    three record into one ``metrics``: one process, told apart by route."""
    handler = request_handler(card, orchestrator, task_store, push)
    metrics = Metrics("tasks") if metrics is None else metrics
    return Listeners(
        public=public_app(card, handler, edge_token, metrics),
        internal_read=internal_read_app(orchestrator, run_keys, metrics),
        internal_write=internal_write_app(handler, orchestrator, metrics),
    )
