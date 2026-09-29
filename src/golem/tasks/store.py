from collections.abc import Awaitable, Callable

from a2a.server.context import ServerCallContext
from a2a.server.tasks import DatabasePushNotificationConfigStore, DatabaseTaskStore
from a2a.types import a2a_pb2
from a2a.utils.constants import DEFAULT_LIST_TASKS_PAGE_SIZE
from a2a.utils.errors import InvalidParamsError
from a2a.utils.task import decode_page_token, encode_page_token
from sqlalchemy import and_, case, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from golem.tasks.executor import AGENT_METADATA

AgentsOfTasks = Callable[[tuple[str, ...]], Awaitable[dict[str, str]]]

AGENT_INDEX = (
    "CREATE INDEX IF NOT EXISTS tasks_by_owner_agent"
    " ON tasks (owner, (metadata->>'golemAgent'), last_updated DESC)"
)


def tasks_engine(url: str) -> AsyncEngine:
    return create_async_engine(url, pool_pre_ping=True)


class AgentTaskStore(DatabaseTaskStore):
    """The SDK's store, whose ListTasks also filters by the tenant the request names (ADR 0018).

    ``list`` repeats the SDK's query (a2a-sdk 1.1.5) with one more predicate; a test compares
    both over the same rows, so an SDK upgrade that changes the query fails the build.
    """

    async def initialize(self) -> None:
        if self._initialized:
            return
        await super().initialize()
        async with self.engine.begin() as conn:
            await conn.execute(text(AGENT_INDEX))

    async def list(
        self, params: a2a_pb2.ListTasksRequest, context: ServerCallContext
    ) -> a2a_pb2.ListTasksResponse:
        await self._ensure_initialized()
        owner = self.owner_resolver(context)
        async with self.async_session_maker() as session:
            timestamp_col = self.task_model.last_updated
            base_stmt = select(self.task_model).where(self.task_model.owner == owner)
            if params.tenant:
                base_stmt = base_stmt.where(
                    self.task_model.task_metadata[AGENT_METADATA].as_string() == params.tenant
                )
            if params.context_id:
                base_stmt = base_stmt.where(self.task_model.context_id == params.context_id)
            if params.status:
                base_stmt = base_stmt.where(
                    self.task_model.status["state"].as_string()
                    == a2a_pb2.TaskState.Name(params.status)
                )
            if params.HasField("status_timestamp_after"):
                last_updated_after = params.status_timestamp_after.ToDatetime()
                base_stmt = base_stmt.where(timestamp_col >= last_updated_after)

            count_stmt = select(func.count()).select_from(base_stmt.alias())
            total_count = (await session.execute(count_stmt)).scalar_one()

            stmt = base_stmt.order_by(
                case((timestamp_col.is_(None), 1), else_=0).asc(),
                timestamp_col.desc(),
                self.task_model.id.desc(),
            )
            if params.page_token:
                start_task_id = decode_page_token(params.page_token)
                start_task = (
                    await session.execute(
                        select(self.task_model).where(
                            and_(
                                self.task_model.id == start_task_id,
                                self.task_model.owner == owner,
                            )
                        )
                    )
                ).scalar_one_or_none()
                if not start_task:
                    raise InvalidParamsError(f"Invalid page token: {params.page_token}")
                start_task_timestamp = start_task.last_updated
                if start_task_timestamp:
                    where_clauses = [
                        and_(
                            timestamp_col == start_task_timestamp,
                            self.task_model.id <= start_task_id,
                        ),
                        timestamp_col < start_task_timestamp,
                        timestamp_col.is_(None),
                    ]
                else:
                    where_clauses = [
                        and_(timestamp_col.is_(None), self.task_model.id <= start_task_id)
                    ]
                stmt = stmt.where(or_(*where_clauses))

            page_size = params.page_size or DEFAULT_LIST_TASKS_PAGE_SIZE
            stmt = stmt.limit(page_size + 1)
            tasks_models = (await session.execute(stmt)).scalars().all()
            tasks = [self._from_orm(task_model) for task_model in tasks_models]
            next_page_token = (
                encode_page_token(tasks[-1].id) if len(tasks) == page_size + 1 else None
            )
            return a2a_pb2.ListTasksResponse(
                tasks=tasks[:page_size],
                total_size=total_count,
                next_page_token=next_page_token,
                page_size=page_size,
            )


def tasks_store(engine: AsyncEngine) -> AgentTaskStore:
    # The store scopes tasks by the caller (the edge's principal), so one caller cannot read
    # or cancel another's task. The default table name is kept on purpose: a custom one makes
    # the SDK register a new model in global metadata, and a second store instance then fails.
    return AgentTaskStore(engine)


async def backfill_agents(engine: AsyncEngine, agents_of_tasks: AgentsOfTasks) -> None:
    """Record the agent of tasks created before the task service recorded it, from their runs.

    Idempotent: a task that has its agent is never read again. Tasks without a run (rejected
    before one started) keep none and appear on no agent's list.
    """
    await tasks_store(engine).initialize()
    async with engine.begin() as conn:
        rows = await conn.execute(
            text("SELECT id FROM tasks WHERE metadata->>'golemAgent' IS NULL")
        )
        agents = await agents_of_tasks(tuple(row[0] for row in rows))
        for task_id, agent in agents.items():
            await conn.execute(
                text(
                    # A task without metadata holds JSON null, not SQL NULL.
                    "UPDATE tasks SET metadata = (CASE WHEN json_typeof(metadata) = 'object'"
                    " THEN metadata::jsonb ELSE '{}'::jsonb END"
                    " || jsonb_build_object('golemAgent', CAST(:agent AS text)))::json"
                    " WHERE id = :id"
                ),
                {"agent": agent, "id": task_id},
            )


def push_config_store(
    engine: AsyncEngine, encryption_key: str
) -> DatabasePushNotificationConfigStore:
    # Push configs hold the receivers' tokens; they are stored encrypted with a Fernet key.
    return DatabasePushNotificationConfigStore(engine, encryption_key=encryption_key)
