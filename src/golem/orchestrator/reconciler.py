"""The reconciler process: ``python -m golem.orchestrator.reconciler``.

Every interval it reconciles finished Jobs, opens merge requests for succeeded runs and
delivers task outcomes. A failed pass is logged and the next one runs; SIGTERM stops the loop
between passes.
"""

import asyncio
import logging
import os
import signal
import sys
from collections.abc import Awaitable, Callable
from contextlib import suppress

import httpx
from psycopg import AsyncConnection

from golem.orchestrator.jobs import JobLauncher
from golem.orchestrator.launchers import launcher_for
from golem.orchestrator.merge_requests import GitLabMergeRequests, propose_merge_request
from golem.orchestrator.notify import TaskServiceNotifier
from golem.orchestrator.reconcile import SucceededRun, reconcile_once
from golem.orchestrator.runs import apply_schema
from golem.settings import (
    ReconcilerSettings,
    SettingsError,
    parse_gitlab_projects,
    reconciler_settings,
)

HTTP_TIMEOUT_SECONDS = 10
SCHEMA_LOCK = "golem_runs:schema"

log = logging.getLogger("golem.reconciler")


async def run_forever(
    reconcile_pass: Callable[[], Awaitable[None]], *, interval_seconds: float, stop: asyncio.Event
) -> None:
    while not stop.is_set():
        try:
            await reconcile_pass()
        except Exception:
            log.exception("reconcile pass failed")
        with suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), interval_seconds)


async def apply_schema_once(dsn: str) -> None:
    # The task service applies the same schema at startup; concurrent CREATE TABLE IF NOT
    # EXISTS can still collide in the catalog, so both take the same lock.
    async with await AsyncConnection.connect(dsn) as conn, conn.transaction():
        await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (SCHEMA_LOCK,))
        await apply_schema(conn)


def pass_for(
    settings: ReconcilerSettings,
    launcher: JobLauncher,
    notifier: TaskServiceNotifier,
    gitlab: GitLabMergeRequests,
) -> Callable[[], Awaitable[None]]:
    async def propose(run: SucceededRun) -> str:
        return await propose_merge_request(gitlab, run)

    async def reconcile_pass() -> None:
        async with await AsyncConnection.connect(settings.runs_dsn, autocommit=True) as conn:
            await reconcile_once(conn, launcher, notifier.notify, propose)

    return reconcile_pass


async def serve(settings: ReconcilerSettings) -> None:
    projects = parse_gitlab_projects(settings.gitlab_projects_file.read_text())
    launcher = launcher_for(settings.kubernetes, settings.namespace)
    await apply_schema_once(settings.runs_dsn)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGTERM, stop.set)
    loop.add_signal_handler(signal.SIGINT, stop.set)
    async with (
        httpx.AsyncClient(
            base_url=settings.task_service_url, timeout=HTTP_TIMEOUT_SECONDS
        ) as tasks_client,
        httpx.AsyncClient(
            base_url=f"{settings.gitlab_url}/api/v4",
            headers={"PRIVATE-TOKEN": settings.gitlab_token},
            timeout=HTTP_TIMEOUT_SECONDS,
        ) as gitlab_client,
    ):
        reconcile_pass = pass_for(
            settings,
            launcher,
            TaskServiceNotifier(tasks_client),
            GitLabMergeRequests(gitlab_client, projects),
        )
        log.info("reconciler started: a pass every %ss", settings.interval_seconds)
        await run_forever(reconcile_pass, interval_seconds=settings.interval_seconds, stop=stop)
    log.info("reconciler stopped")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        settings = reconciler_settings(os.environ)
    except SettingsError as error:
        sys.exit(f"golem reconciler: {error}")
    asyncio.run(serve(settings))


if __name__ == "__main__":
    main()
