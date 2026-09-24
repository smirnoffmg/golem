from a2a.server.tasks import DatabaseTaskStore
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine


def tasks_engine(url: str) -> AsyncEngine:
    return create_async_engine(url, pool_pre_ping=True)


def tasks_store(engine: AsyncEngine) -> DatabaseTaskStore:
    # The store scopes tasks by the caller (the edge's principal), so one caller cannot read
    # or cancel another's task. The default table name is kept on purpose: a custom one makes
    # the SDK register a new model in global metadata, and a second store instance then fails.
    return DatabaseTaskStore(engine)
