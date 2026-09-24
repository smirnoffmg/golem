from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from a2a.auth.user import User
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import (
    DefaultServerCallContextBuilder,
    create_agent_card_routes,
    create_jsonrpc_routes,
)
from a2a.server.tasks import InMemoryTaskStore, TaskStore
from a2a.types.a2a_pb2 import AgentCard
from starlette.applications import Starlette
from starlette.requests import Request

from golem.tasks.executor import RunExecutor
from golem.tasks.ports import Orchestrator

RPC_PATH = "/a2a"
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

    return Starlette(
        routes=create_agent_card_routes(card)
        + create_jsonrpc_routes(handler, RPC_PATH, EdgeContextBuilder()),
        lifespan=lifespan,
    )
