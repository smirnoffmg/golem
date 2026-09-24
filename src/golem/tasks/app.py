from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from a2a.auth.user import UnauthenticatedUser, User
from a2a.server.context import ServerCallContext
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import (
    DefaultServerCallContextBuilder,
    create_agent_card_routes,
    create_jsonrpc_routes,
)
from a2a.server.tasks import InMemoryTaskStore, TaskStore
from a2a.types.a2a_pb2 import AgentCard, Message, Part, Role, SendMessageRequest
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


def create_app(
    card: AgentCard, orchestrator: Orchestrator, task_store: TaskStore | None = None
) -> Starlette:
    handler = DefaultRequestHandler(
        agent_executor=RunExecutor(orchestrator),
        task_store=task_store or InMemoryTaskStore(),
        agent_card=card,
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
        try:
            await handler.on_message_send(
                SendMessageRequest(
                    tenant=body["tenant"],
                    message=Message(
                        message_id=f"run-outcome-{outcome.run_id}-{body['task_id']}",
                        task_id=body["task_id"],
                        role=Role.ROLE_USER,
                        parts=[Part(text=outcome.detail)],
                    ),
                ),
                ServerCallContext(
                    user=EdgePrincipal(caller) if caller else UnauthenticatedUser(),
                    tenant=body["tenant"],
                    state={RUN_OUTCOME: outcome},
                ),
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
