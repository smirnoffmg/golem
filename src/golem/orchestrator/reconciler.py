"""The reconciler process: ``python -m golem.orchestrator.reconciler``.

Every interval it reconciles finished Jobs, opens merge requests for succeeded runs or records
what they propose for a person to decide in Golem, reads the reports of goal runs that proposed
nothing and deletes their branches (ADR 0017), delivers task outcomes, follows open merge
requests to their proposals' state, asks again for accepted proposals whose apply was lost and
lands the records of decided ones (ADR 0015), and takes every
process a step, starting its stages through the edge (ADR 0019). A failed
pass, or one that runs past its timeout, is logged and the next one runs; SIGTERM stops the
loop between passes. Its metrics are served on ``GOLEM_METRICS_PORT``, the only port it
listens on.
"""

import asyncio
import logging
import os
import signal
import sys
import time
from collections.abc import Awaitable, Callable
from contextlib import suppress

import httpx
from psycopg import AsyncConnection

from golem.metrics import Metrics, ReconcilerMetrics, metrics_app, process_registry
from golem.orchestrator.jobs import JobLauncher
from golem.orchestrator.launchers import launcher_for
from golem.orchestrator.merge_requests import (
    GitLabMergeRequests,
    check_merge_request,
    close_merge_request,
    closing_reason,
    discard_branch,
    land_record,
    propose_result,
    pushed_branch,
    read_report,
)
from golem.orchestrator.notify import TaskServiceNotifier
from golem.orchestrator.process_runs import ProcessPorts, StageRefused, StageStart
from golem.orchestrator.proposals import Landing, PendingMergeRequest, Transition
from golem.orchestrator.reconcile import (
    Pushed,
    Reporter,
    Settlement,
    SucceededRun,
    reconcile_once,
)
from golem.orchestrator.runs import apply_schema
from golem.orchestrator.service import CONNECT_TIMEOUT_SECONDS
from golem.orchestrator.stages import EdgeStages
from golem.serving import listener
from golem.settings import (
    ReconcilerSettings,
    SettingsError,
    parse_gitlab_projects,
    parse_signing_key,
    reconciler_settings,
)

HTTP_TIMEOUT_SECONDS = 10
# A pass that outlives this is abandoned and the next one starts; every call inside a pass has
# its own timeout, so this only catches what those miss.
PASS_TIMEOUT_SECONDS = 300.0
SCHEMA_LOCK = "golem_runs:schema"

log = logging.getLogger("golem.reconciler")


async def run_forever(
    reconcile_pass: Callable[[], Awaitable[None]],
    *,
    interval_seconds: float,
    stop: asyncio.Event,
    metrics: ReconcilerMetrics | None = None,
    pass_timeout_seconds: float = PASS_TIMEOUT_SECONDS,
) -> None:
    metrics = ReconcilerMetrics() if metrics is None else metrics
    while not stop.is_set():
        started = time.perf_counter()
        failed = False
        try:
            await asyncio.wait_for(reconcile_pass(), pass_timeout_seconds)
        except Exception:
            failed = True
            log.exception("reconcile pass failed")
        metrics.pass_finished(time.perf_counter() - started, failed=failed)
        with suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), interval_seconds)


async def apply_schema_once(dsn: str) -> None:
    # The task service applies the same schema at startup; concurrent CREATE TABLE IF NOT
    # EXISTS can still collide in the catalog, so both take the same lock.
    async with (
        await AsyncConnection.connect(dsn, connect_timeout=CONNECT_TIMEOUT_SECONDS) as conn,
        conn.transaction(),
    ):
        await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (SCHEMA_LOCK,))
        await apply_schema(conn)


def pass_for(
    settings: ReconcilerSettings,
    launcher: JobLauncher,
    notifier: TaskServiceNotifier,
    gitlab: GitLabMergeRequests,
    metrics: ReconcilerMetrics | None = None,
    stages: EdgeStages | None = None,
) -> Callable[[], Awaitable[None]]:
    async def propose(run: SucceededRun) -> Settlement:
        return await propose_result(gitlab, run)

    async def land(landing: Landing) -> None:
        await land_record(gitlab, landing)

    async def proposed(run: SucceededRun) -> Pushed | None:
        return await pushed_branch(gitlab, run)

    async def check(pending: PendingMergeRequest) -> Transition | None:
        return await check_merge_request(gitlab, pending)

    async def read(run: SucceededRun) -> str | None:
        return await read_report(gitlab, run)

    async def discard(run: SucceededRun) -> None:
        await discard_branch(gitlab, run)

    async def reason(agent: str, iid: int) -> str | None:
        return await closing_reason(gitlab, agent, iid)

    async def close(agent: str, iid: int) -> None:
        await close_merge_request(gitlab, agent, iid)

    async def start(stage: StageStart) -> str | StageRefused:
        assert stages is not None
        return await stages.start(stage)

    processes = None if stages is None else ProcessPorts(start, reason, close)

    async def reconcile_pass() -> None:
        async with await AsyncConnection.connect(
            settings.runs_dsn, autocommit=True, connect_timeout=CONNECT_TIMEOUT_SECONDS
        ) as conn:
            await reconcile_once(
                conn,
                launcher,
                notifier.notify,
                propose,
                metrics,
                proposed,
                check=check,
                notify_proposal=notifier.notify_proposal,
                poll_seconds=settings.mr_poll_seconds,
                report=Reporter(read, discard),
                processes=processes,
                notify_process=notifier.notify_process,
                land=land,
            )

    return reconcile_pass


def stages_for(settings: ReconcilerSettings, edge_client: httpx.AsyncClient) -> EdgeStages | None:
    if settings.stages is None:
        log.warning("no GOLEM_EDGE_URL and run-token key: processes' stages are not started")
        return None
    key = parse_signing_key(
        settings.stages.run_token_key_file.read_text(), settings.stages.run_token_kid
    )
    return EdgeStages(client=edge_client, signing_key=key)


async def serve(settings: ReconcilerSettings) -> None:
    projects = parse_gitlab_projects(settings.gitlab_projects_file.read_text())
    launcher = launcher_for(settings.kubernetes, settings.namespace)
    # The agents with a GitLab project are the ones this process names; others are "other".
    registry = process_registry()
    metrics = ReconcilerMetrics(Metrics("reconciler", registry=registry, agents=projects))
    await apply_schema_once(settings.runs_dsn)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGTERM, stop.set)
    loop.add_signal_handler(signal.SIGINT, stop.set)
    # The loop has no HTTP server of its own; a listener serves only its metrics (ADR 0013).
    exposition = listener(metrics_app(registry), settings.metrics_port)
    serving = asyncio.create_task(exposition.serve())
    # A metrics listener that stops (a port already taken) stops the process, as in serve_all.
    serving.add_done_callback(lambda _: stop.set())
    async with (
        httpx.AsyncClient(
            base_url=settings.task_service_url, timeout=HTTP_TIMEOUT_SECONDS
        ) as tasks_client,
        httpx.AsyncClient(
            base_url=f"{settings.gitlab_url}/api/v4",
            headers={"PRIVATE-TOKEN": settings.gitlab_token},
            timeout=HTTP_TIMEOUT_SECONDS,
        ) as gitlab_client,
        httpx.AsyncClient(
            base_url=settings.stages.edge_url if settings.stages else "http://unused",
            timeout=HTTP_TIMEOUT_SECONDS,
        ) as edge_client,
    ):
        reconcile_pass = pass_for(
            settings,
            launcher,
            TaskServiceNotifier(tasks_client),
            GitLabMergeRequests(gitlab_client, projects),
            metrics,
            stages_for(settings, edge_client),
        )
        log.info("reconciler started: a pass every %ss", settings.interval_seconds)
        try:
            await run_forever(
                reconcile_pass,
                interval_seconds=settings.interval_seconds,
                stop=stop,
                metrics=metrics,
            )
        finally:
            exposition.should_exit = True
            await serving
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
