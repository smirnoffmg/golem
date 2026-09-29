"""The web UI with everything behind it, on localhost ports: the board's nginx and the
backend-for-frontend behind one front that routes like the ingress (support/front.py), the real
edge and task service, the Postgres-backed orchestrator with a fake Job launcher, and the fake
identity provider, each served by uvicorn in its own thread.

The stack runs as a deployment does, one of two ways. Without a process pinned, people start
the ``discovery`` agent: ``seed`` gives the user ``alice`` a task in every state the UI shows,
through the same code paths as production: A2A calls to the edge, admission in golem_runs, a
reconcile pass that opens a merge request (GitLab faked at its HTTP boundary) and delivers the
outcome. With ``processes=True`` a process is pinned, the edge derives its call registry from
the catalogs as it does in production, and people start only the process (ADR 0019): ``seed``
starts two, whose stages the reconciler starts through the edge with call tokens; one waits
for review of its stage's merge request, the other for a reason after its merge request was
closed without one.
"""

import asyncio
import json
import socket
import threading
import time
import uuid
from collections.abc import Coroutine, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from functools import partial
from pathlib import Path
from random import Random
from typing import Any
from unittest import mock

import httpx
import psycopg
import uvicorn
from cryptography.fernet import Fernet
from starlette.types import ASGIApp
from testcontainers.community.postgres import PostgresContainer

from golem.catalog import Catalogs, ProcessCatalog, load_catalogs
from golem.edge.__main__ import authenticator, call_authenticator, public_cards
from golem.edge.app import create_edge_app
from golem.edge.auth import authenticate_any
from golem.edge.card_signing import card_keys
from golem.edge.policy import ChainLimits, Registry
from golem.edge.registry import derive_registry
from golem.jwks import SigningKeys, fetch_jwks
from golem.orchestrator import runs
from golem.orchestrator.admission import Limits
from golem.orchestrator.jobs import CatalogRef, JobSpec, JobStatus
from golem.orchestrator.merge_requests import (
    GitLabMergeRequests,
    GitLabProject,
    check_merge_request,
    close_merge_request,
    closing_reason,
    propose_merge_request,
)
from golem.orchestrator.notify import TaskServiceNotifier
from golem.orchestrator.process_runs import ProcessPorts
from golem.orchestrator.reconcile import SucceededRun, reconcile_once
from golem.orchestrator.service import JobTemplate, PostgresOrchestrator
from golem.orchestrator.stages import EdgeStages
from golem.ratelimit import Limiter, Rate
from golem.run_status import RunStatuses
from golem.run_token import SigningKey
from golem.runtime.main import Outcome, RunReport, report_json
from golem.serving import Listener
from golem.tasks.__main__ import service_card
from golem.tasks.app import RUN_KEYS_PATH, create_listeners
from golem.tasks.store import tasks_engine, tasks_store
from golem.ui import store
from golem.ui.app import create_ui_app
from golem.ui.edge import rpc
from golem.ui.oidc import OidcClient
from support.front import Front
from support.idp import FakeIdP, idp_app

ROOT = Path(__file__).resolve().parents[2]
INIT_SQL = ROOT / "deploy" / "postgres" / "init.sql"
EXAMPLES = ROOT / "examples"
USER = "alice"
AGENT = "discovery"
# A process a person starts, and the worker its stages run; people cannot call the worker.
PROCESS = "discovery-flow"
STAGE_AGENT = "researcher"
PROCESS_CATALOG = ProcessCatalog(
    name=PROCESS,
    description="From interview notes to evidenced hypotheses, then to a reviewed solution.",
    version="0.1.0",
    stages=(
        {"name": "evidence", "agent": STAGE_AGENT, "goal": "Collect evidence: {input}"},
        {"name": "solution", "agent": STAGE_AGENT, "goal": "Propose a solution for it."},
    ),
)
EDGE_TOKEN = "demo-edge-token"
CLIENT_SECRET = "demo-client-secret"
EDGE_AUDIENCE = "golem-edge"
CATALOG_URL = f"https://git.example.com/agents/{AGENT}.git"
GITLAB_API = "https://git.example.com/api/v4"
CONTEXT_PROJECT = "product/discovery-context"
MERGE_REQUEST_IID = 42
MERGE_REQUEST_URL = (
    f"https://git.example.com/{CONTEXT_PROJECT}/-/merge_requests/{MERGE_REQUEST_IID}"
)
GITLAB_TOKEN = "demo-gitlab-token"
# Four at once, so the fifth task of the seed is refused by admission with the real reason.
LIMITS = Limits(max_runs_per_caller=4, max_runs_per_root=4, budget_per_root=Decimal("100"))
# One person clicking through every page in seconds; the edge's default would refuse them.
DEMO_CALLER_RATE = Rate(per_minute=600, burst=100)
START_TIMEOUT_SECONDS = 15
STOP_TIMEOUT_SECONDS = 10
GOALS = {
    "completed": "Collect evidence for hypothesis H-2 from the latest interview notes.",
    "failed": "Propose a solution for the validated hypothesis H-3.",
    "canceled": "Review the proposed solution S-1 against the evidence it relies on.",
    "working": "Research hypothesis H-1: do teams re-open discussions already decided?",
    "rejected": "Draft a second solution for H-1.",
}
PROCESS_GOALS = {
    "review": "Interview notes from the September round on onboarding friction.",
    "needs_reason": "Support tickets tagged 'export' from the last quarter.",
}
VALIDATION_REASON = (
    "H-3 changed status from 'validated' to 'accepted'; status changes are human decisions"
)


def postgres_container() -> PostgresContainer:
    # The same init.sql as deploy/, so the stack runs with the real roles and grants.
    return PostgresContainer("postgres:17", driver=None).with_volume_mapping(
        str(INIT_SQL), "/docker-entrypoint-initdb.d/init.sql", "ro"
    )


@dataclass(frozen=True)
class Databases:
    host: str
    port: int

    def dsn(self, database: str, user: str) -> str:
        password = f"dev-only-{user.replace('_', '-')}"
        return (
            f"host={self.host} port={self.port} dbname={database} user={user} password={password}"
        )

    @property
    def runs(self) -> str:
        return self.dsn("golem_runs", "golem_runs")

    @property
    def audit(self) -> str:
        return self.dsn("golem_audit", "golem_edge")

    @property
    def ui(self) -> str:
        return self.dsn("golem_ui", "golem_ui")

    @property
    def tasks(self) -> str:
        return self.dsn("golem_tasks", "golem_tasks")

    @property
    def tasks_url(self) -> str:
        return (
            f"postgresql+asyncpg://golem_tasks:dev-only-golem-tasks@{self.host}:{self.port}"
            "/golem_tasks"
        )


def databases_of(container: PostgresContainer) -> Databases:
    return Databases(container.get_container_host_ip(), int(container.get_exposed_port(5432)))


async def reset(databases: Databases) -> None:
    """Empty schemas, so the seeded tasks are the only ones alice has."""
    async with await psycopg.AsyncConnection.connect(databases.runs, autocommit=True) as conn:
        await runs.apply_schema(conn)
        await conn.execute("TRUNCATE runs, run_tasks, proposals, process_stages")
    async with await psycopg.AsyncConnection.connect(databases.ui, autocommit=True) as conn:
        await store.apply_schema(conn)
        await conn.execute("TRUNCATE sessions, logins")
    async with await psycopg.AsyncConnection.connect(databases.tasks, autocommit=True) as conn:
        cursor = await conn.execute("SELECT to_regclass('tasks') IS NOT NULL")
        row = await cursor.fetchone()
        if row is not None and row[0]:
            await conn.execute("TRUNCATE tasks")


@dataclass
class DemoLauncher:
    """Kubernetes is the boundary: Jobs run until ``finish`` says how they ended."""

    launched: list[JobSpec] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    ended: dict[str, tuple[JobStatus, str | None]] = field(default_factory=dict)

    def launch(self, spec: JobSpec) -> None:
        self.launched.append(spec)

    def status(self, run_id: str) -> JobStatus:
        return self.ended.get(run_id, (JobStatus.RUNNING, None))[0]

    def delete(self, run_id: str) -> None:
        self.deleted.append(run_id)

    def termination_message(self, run_id: str) -> str | None:
        return self.ended.get(run_id, (JobStatus.RUNNING, None))[1]

    def finish(self, run_id: str, status: JobStatus, report: str | None = None) -> None:
        self.ended[run_id] = (status, report)


@dataclass
class Served:
    servers: list[uvicorn.Server]
    thread: threading.Thread

    def stop(self) -> None:
        for server in self.servers:
            server.should_exit = True
        self.thread.join(STOP_TIMEOUT_SECONDS)


def bound(port: int = 0) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", port))
    return sock


def port_of(sock: socket.socket) -> int:
    return sock.getsockname()[1]


def serve(*apps: tuple[ASGIApp, socket.socket]) -> Served:
    """The apps in one thread and event loop of their own.

    Apps of one process share a loop, as they would in production: the task service's
    listeners share a database pool bound to it. Processes get separate loops: the edge
    fetches signing keys with a blocking call that must not stall the identity provider.
    """
    servers = [Listener(uvicorn.Config(app, log_level="warning")) for app, _ in apps]

    async def serve_all() -> None:
        await asyncio.gather(
            *(server.serve(sockets=[sock]) for server, (_, sock) in zip(servers, apps, strict=True))
        )

    thread = threading.Thread(target=asyncio.run, args=(serve_all(),), daemon=True)
    thread.start()
    deadline = time.monotonic() + START_TIMEOUT_SECONDS
    while not all(server.started for server in servers):
        if not thread.is_alive() or time.monotonic() > deadline:
            ports = [port_of(sock) for _, sock in apps]
            raise RuntimeError(f"the servers on ports {ports} did not start")
        time.sleep(0.02)
    return Served(servers, thread)


def in_own_loop[T](work: Coroutine[Any, Any, T]) -> T:
    """``work`` run to completion in a thread of its own: the caller's thread may be running
    an event loop already, as Playwright's sync API does."""
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, work).result()


@dataclass
class DemoGitLab:
    """The GitLab calls the reconciler makes, with merge requests that keep their state: a run's
    branch is found, a merge request opened, read back, closed, and its notes listed."""

    merge_requests: list[dict[str, Any]] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        with self.lock:
            if path.endswith("/repository/branches"):
                run_id = request.url.params["search"].strip("/$")
                return httpx.Response(200, json=[{"name": f"golem/H-2/{run_id}"}])
            if path.endswith("/merge_requests") and request.method == "GET":
                source = request.url.params.get("source_branch")
                state = request.url.params.get("state", "all")
                found = [
                    mr
                    for mr in self.merge_requests
                    if mr["source_branch"] == source and state in ("all", mr["state"])
                ]
                return httpx.Response(200, json=found)
            if path.endswith("/merge_requests"):
                return httpx.Response(201, json=self._open(json.loads(request.content)))
            if path.endswith("/notes"):
                return httpx.Response(200, json=[])
            if "/merge_requests/" in path:
                mr = self._numbered(int(path.rsplit("/", 1)[1]))
                if mr is None:
                    return httpx.Response(404, json={"message": "404 Not found"})
                if request.method == "PUT":
                    self._close(mr)
                return httpx.Response(200, json=mr)
        return httpx.Response(404, json={"message": "404 Not found"})

    def _open(self, body: dict[str, Any]) -> dict[str, Any]:
        iid = MERGE_REQUEST_IID + len(self.merge_requests)
        mr = {
            "iid": iid,
            "state": "opened",
            "source_branch": body["source_branch"],
            "web_url": f"https://git.example.com/{CONTEXT_PROJECT}/-/merge_requests/{iid}",
        }
        self.merge_requests.append(mr)
        return mr

    def _numbered(self, iid: int) -> dict[str, Any] | None:
        return next((mr for mr in self.merge_requests if mr["iid"] == iid), None)

    def _close(self, mr: dict[str, Any]) -> None:
        mr.update(
            state="closed",
            closed_by={"username": "bob"},
            closed_at=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        )

    def close_without_comment(self, source_branch_suffix: str) -> None:
        """A person closes the stage's merge request in GitLab and writes nothing."""
        with self.lock:
            for mr in self.merge_requests:
                if mr["source_branch"].endswith(source_branch_suffix):
                    self._close(mr)


@dataclass
class Demo:
    ui_url: str
    edge_url: str
    outcome_url: str
    idp: FakeIdP
    launcher: DemoLauncher
    databases: Databases
    gitlab: DemoGitLab
    # The reconciler signs the call tokens that start a process's stages at the edge.
    run_key: SigningKey
    # A process is pinned; people start it, and not the agent.
    processes: bool


@contextmanager
def running(
    databases: Databases, board_url: str, ui_port: int = 0, *, processes: bool = False
) -> Iterator[Demo]:
    """The stack, with the board served from ``board_url`` (support.front.board_server)."""
    in_own_loop(reset(databases))
    pinned = {PROCESS: PROCESS_CATALOG} if processes else {}
    catalogs = Catalogs(agents=load_catalogs(EXAMPLES).agents, processes=pinned)
    limits = ChainLimits(max_depth=3)
    # As the edge's start derives it: with a process pinned, people may start only processes.
    registry = derive_registry(
        Registry(allowed_callers={PROCESS if processes else AGENT: frozenset({"user:*"})}),
        catalogs,
        limits,
    )
    ui_socket = bound(ui_port)
    idp_socket, edge_socket, tasks_socket, outcome_socket, read_socket, bff_socket = (
        bound() for _ in range(6)
    )
    ui_url = f"http://localhost:{port_of(ui_socket)}"
    edge_url = f"http://127.0.0.1:{port_of(edge_socket)}"
    tasks_url = f"http://127.0.0.1:{port_of(tasks_socket)}"
    idp = FakeIdP(
        issuer=f"http://localhost:{port_of(idp_socket)}",
        client_secret=CLIENT_SECRET,
        redirect_url=f"{ui_url}/callback",
        audience=EDGE_AUDIENCE,
        # Long enough that no refresh happens while someone looks around.
        expires_in=3600,
    )
    launcher = DemoLauncher()
    run_key = SigningKey.generate(kid="demo")
    catalog = CatalogRef(url=CATALOG_URL, revision="main")
    listeners = create_listeners(
        service_card(tasks_url),
        PostgresOrchestrator(
            dsn=databases.runs,
            limits=LIMITS,
            estimated_cost=Decimal("1"),
            launcher=launcher,
            template=JobTemplate(
                image="registry.example.com/golem/runtime:0.1.0",
                namespace="golem-jobs",
                secret_name="golem-run-secrets",
                active_deadline_seconds=3600,
                ttl_seconds_after_finished=600,
                cpu="1",
                memory="1Gi",
            ),
            catalogs={AGENT: catalog, STAGE_AGENT: catalog},
            signing_key=run_key,
            grants={AGENT: ("tracker.read", "wiki.read")},
            processes=pinned,
        ),
        edge_token=EDGE_TOKEN,
        task_store=tasks_store(tasks_engine(databases.tasks_url)),
        run_keys=(run_key,),
    )
    read_url = f"http://127.0.0.1:{port_of(read_socket)}"
    served = [
        serve((idp_app(idp, USER, post_logout_redirects=(f"{ui_url}/",)), idp_socket)),
        serve(
            (listeners.public, tasks_socket),
            (listeners.internal_write, outcome_socket),
            (listeners.internal_read, read_socket),
        ),
    ]
    try:
        edge_keys = SigningKeys(partial(fetch_jwks, httpx.Client(timeout=5), idp.jwks_url))
        edge_keys.refresh()
        golem_keys = SigningKeys(
            partial(fetch_jwks, httpx.Client(base_url=read_url, timeout=5), RUN_KEYS_PATH)
        )
        card_key = SigningKey.generate("demo-cards")
        edge = create_edge_app(
            authenticate=partial(
                authenticate_any,
                idp=authenticator(edge_keys, issuer=idp.issuer, audience=EDGE_AUDIENCE),
                golem=call_authenticator(golem_keys),
            ),
            registry=registry,
            limits=limits,
            audit_dsn=databases.audit,
            forward=httpx.AsyncClient(base_url=tasks_url, timeout=10),
            edge_token=EDGE_TOKEN,
            cards=public_cards(
                catalogs,
                base_url=edge_url,
                oidc_discovery_url=idp.discovery_url,
                signing_key=card_key,
            ),
            card_keys=card_keys([card_key]),
            public_base_url=edge_url,
            callers=Limiter(DEMO_CALLER_RATE),
            run_statuses=RunStatuses(
                httpx.AsyncClient(base_url=read_url, timeout=5), ttl_seconds=0
            ),
        )
        served.append(serve((edge, edge_socket)))
        ui = create_ui_app(
            oidc=OidcClient(
                http=httpx.AsyncClient(timeout=5),
                keys_http=httpx.Client(timeout=5),
                issuer=idp.issuer,
                discovery_url=idp.discovery_url,
                client_id=idp.client_id,
                client_secret=CLIENT_SECRET,
                redirect_url=idp.redirect_url,
            ),
            store=store.SessionStore(databases.ui, Fernet.generate_key().decode()),
            edge=httpx.AsyncClient(base_url=edge_url, timeout=10),
            public_base_url=ui_url,
        )
        served.append(serve((ui, bff_socket)))
        served.append(
            serve((Front(f"http://127.0.0.1:{port_of(bff_socket)}", board_url), ui_socket))
        )
        yield Demo(
            ui_url=ui_url,
            edge_url=edge_url,
            outcome_url=f"http://127.0.0.1:{port_of(outcome_socket)}",
            idp=idp,
            launcher=launcher,
            databases=databases,
            gitlab=DemoGitLab(),
            run_key=run_key,
            processes=processes,
        )
    finally:
        for each in served:
            each.stop()


def seed(demo: Demo) -> dict[str, str]:
    """alice's tasks, one per state the board shows, or with processes her two processes;
    returns each one's board path by state (a process's as ``process-<state>``)."""
    return in_own_loop(_seed_processes(demo) if demo.processes else _seed(demo))


async def _seed(demo: Demo) -> dict[str, str]:
    token = demo.idp.access_token(USER)
    async with httpx.AsyncClient(base_url=demo.edge_url, timeout=10) as edge:

        async def send(tenant: str, key: str, text: str) -> dict[str, Any]:
            message = {"messageId": f"seed-{key}", "role": "ROLE_USER", "parts": [{"text": text}]}
            result = await rpc(edge, token, "SendMessage", {"tenant": tenant, "message": message})
            return result["task"]

        tasks = {state: await send(AGENT, state, goal) for state, goal in GOALS.items()}
        await _finish(demo, tasks["completed"], JobStatus.SUCCEEDED)
        await _finish(
            demo,
            tasks["failed"],
            JobStatus.FAILED,
            report_json(
                RunReport(
                    run_id=tasks["failed"]["metadata"]["runId"],
                    agent=AGENT,
                    outcome=Outcome.INVALID,
                    role="designer",
                    target_id="H-3",
                    reasons=(VALIDATION_REASON,),
                )
            ),
        )
        await rpc(edge, token, "CancelTask", {"tenant": AGENT, "id": tasks["canceled"]["id"]})
    return {state: f"/agents/{AGENT}/tasks/{task['id']}" for state, task in tasks.items()}


async def _seed_processes(demo: Demo) -> dict[str, str]:
    token = demo.idp.access_token(USER)
    async with httpx.AsyncClient(base_url=demo.edge_url, timeout=10) as edge:
        processes = {}
        for state, goal in PROCESS_GOALS.items():
            message = {
                "messageId": f"seed-process-{state}",
                "role": "ROLE_USER",
                "parts": [{"text": goal}],
            }
            result = await rpc(edge, token, "SendMessage", {"tenant": PROCESS, "message": message})
            processes[state] = result["task"]
    # The reconciler starts each process's first stage at the edge, then shows it on the task.
    await _reconcile(demo)
    for process in processes.values():
        await _finish(
            demo, {"metadata": {"runId": await _stage_run(demo, process)}}, JobStatus.SUCCEEDED
        )
    await _reconcile(demo)
    # A person closes the second one's merge request in GitLab without saying why.
    demo.gitlab.close_without_comment(await _stage_run(demo, processes["needs_reason"]))
    await _reconcile(demo)
    return {
        f"process-{state}": f"/agents/{PROCESS}/tasks/{task['id']}"
        for state, task in processes.items()
    }


async def _stage_run(demo: Demo, process_task: dict[str, Any]) -> str:
    async with await psycopg.AsyncConnection.connect(demo.databases.runs) as conn:
        cursor = await conn.execute(
            "SELECT run_id FROM process_stages WHERE process_run_id = %s",
            (process_task["metadata"]["runId"],),
        )
        row = await cursor.fetchone()
    assert row is not None and row[0] is not None, "the process's stage has not started"
    return str(row[0])


async def _finish(
    demo: Demo, task: dict[str, Any], status: JobStatus, report: str | None = None
) -> None:
    """The Job ends; a reconcile pass records it, proposes its branch, delivers the outcome."""
    demo.launcher.finish(task["metadata"]["runId"], status, report)
    await _reconcile(demo)


async def _reconcile(demo: Demo) -> None:
    """One reconcile pass as the reconciler makes it, GitLab faked at its HTTP boundary."""
    async with (
        httpx.AsyncClient(
            transport=httpx.MockTransport(demo.gitlab.handle),
            base_url=GITLAB_API,
            headers={"PRIVATE-TOKEN": GITLAB_TOKEN},
        ) as client,
        httpx.AsyncClient(base_url=demo.outcome_url, timeout=10) as outcomes,
        httpx.AsyncClient(base_url=demo.edge_url, timeout=10) as edge,
        await psycopg.AsyncConnection.connect(demo.databases.runs, autocommit=True) as conn,
    ):
        project = GitLabProject(path=CONTEXT_PROJECT, target_branch="main")
        merge_requests = GitLabMergeRequests(client, {AGENT: project, STAGE_AGENT: project})
        notifier = TaskServiceNotifier(outcomes)

        async def propose(run: SucceededRun) -> Any:
            return await propose_merge_request(merge_requests, run)

        async def check(pending: Any) -> Any:
            return await check_merge_request(merge_requests, pending)

        await reconcile_once(
            conn,
            demo.launcher,
            notifier.notify,
            propose,
            check=check,
            notify_proposal=notifier.notify_proposal,
            poll_seconds=0,
            processes=ProcessPorts(
                start=EdgeStages(client=edge, signing_key=demo.run_key).start,
                closing_reason=partial(closing_reason, merge_requests),
                close_merge_request=partial(close_merge_request, merge_requests),
            ),
            notify_process=notifier.notify_process,
        )


def reconcile(demo: Demo) -> None:
    """One reconcile pass, from a caller that may be running an event loop already."""
    in_own_loop(_reconcile(demo))


def run_count(demo: Demo) -> int:
    with psycopg.connect(demo.databases.runs) as conn:
        row = conn.execute("SELECT count(*) FROM runs").fetchone()
    assert row is not None
    return int(row[0])


@contextmanager
def frozen(start: datetime, seed: int = 0) -> Iterator[None]:
    """Task ids, run ids and status timestamps that are the same on every run, for screenshots.

    a2a-sdk and golem_runs take ids from uuid.uuid4, and a2a-sdk and the task service take
    timestamps from datetime.now; the seed calls them in a fixed order, so a seeded sequence
    reproduces them.
    Each timestamp is a minute after the one before.
    """
    ids = Random(seed)
    lock = threading.Lock()
    ticks = iter(range(10**6))

    def uuid4() -> uuid.UUID:
        with lock:
            return uuid.UUID(int=ids.getrandbits(128), version=4)

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> datetime:  # type: ignore[override]
            with lock:
                return (start + timedelta(minutes=next(ticks))).astimezone(tz or UTC)

    with (
        mock.patch("uuid.uuid4", uuid4),
        mock.patch("a2a.server.tasks.task_updater.datetime", FrozenDatetime),
        # A proposal's or a process's change moves its task's timestamp too.
        mock.patch("golem.tasks.app.datetime", FrozenDatetime),
    ):
        yield
