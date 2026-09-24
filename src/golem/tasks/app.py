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
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from golem.tasks.executor import RUN_OUTCOME, RunExecutor
from golem.tasks.ports import Orchestrator, RunOutcome

RPC_PATH = "/a2a"
OUTCOME_PATH = "/internal/run-outcome"
OUTCOME_STATUSES = {"succeeded": True, "failed": False}
TERMINAL_STATES = {
    TaskState.TASK_STATE_COMPLETED,
    TaskState.TASK_STATE_FAILED,
    TaskState.TASK_STATE_CANCELED,
    TaskState.TASK_STATE_REJECTED,
}
PRINCIPAL_HEADER = "x-golem-principal"


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
    # Only the A2A edge can reach the task service (network policy), and the edge has already
    # authenticated the caller, so its principal header is trusted here and nowhere else.
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


def create_app(
    card: AgentCard,
    orchestrator: Orchestrator,
    task_store: TaskStore | None = None,
    push: PushDelivery | None = None,
) -> Starlette:
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
    handler = DefaultRequestHandler(
        agent_executor=RunExecutor(orchestrator),
        task_store=task_store or InMemoryTaskStore(),
        agent_card=card,
        **push_options,
    )

    @asynccontextmanager
    async def lifespan(_: Starlette) -> AsyncIterator[None]:
        yield
        await handler.aclose()

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

    return Starlette(
        routes=create_agent_card_routes(card)
        + create_jsonrpc_routes(handler, RPC_PATH, EdgeContextBuilder())
        + [Route(OUTCOME_PATH, run_outcome, methods=["POST"])],
        lifespan=lifespan,
    )
