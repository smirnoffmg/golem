from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes
from a2a.server.tasks import InMemoryTaskStore, TaskStore
from a2a.types.a2a_pb2 import AgentCard
from starlette.applications import Starlette

from golem.tasks.executor import RunExecutor
from golem.tasks.ports import Orchestrator

RPC_PATH = "/a2a"


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
        routes=create_agent_card_routes(card) + create_jsonrpc_routes(handler, RPC_PATH),
        lifespan=lifespan,
    )
