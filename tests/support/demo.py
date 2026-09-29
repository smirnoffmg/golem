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

With ``proposals=True`` three goal agents propose what the platform applies (ADR 0015), and
``alice`` reviews them: ``seed`` has ``bob`` start their runs, each run's branch carries its
golem-proposal.json in GitLab (faked with the branches and files the reconciler reads), and the
reconciler records the proposals; accepting one applies it through a real write server to
Confluence, Jira Service Management or Jira, faked at their HTTP boundary (support.atlassian).
One more run finds nothing to propose and leaves a report.
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
from urllib.parse import unquote

import httpx
import psycopg
import uvicorn
from cryptography.fernet import Fernet
from starlette.types import ASGIApp
from testcontainers.community.postgres import PostgresContainer

from golem.catalog import AgentCatalog, Catalogs, ProcessCatalog, load_catalogs
from golem.edge.__main__ import authenticator, call_authenticator, public_cards, reviewers_of
from golem.edge.app import create_edge_app
from golem.edge.auth import authenticate_any
from golem.edge.card_signing import card_keys
from golem.edge.policy import ChainLimits, Registry
from golem.edge.registry import derive_registry
from golem.jwks import SigningKeys, fetch_jwks
from golem.mcp.atlassian import Deployment
from golem.mcp.auth import proposal_token_verifier
from golem.mcp.gate import Gate
from golem.mcp.groups import GROUPS
from golem.mcp.server import create_mcp_app
from golem.orchestrator import runs
from golem.orchestrator.admission import Limits
from golem.orchestrator.jobs import CatalogRef, JobSpec, JobStatus
from golem.orchestrator.merge_requests import (
    GitLabMergeRequests,
    GitLabProject,
    check_merge_request,
    close_merge_request,
    closing_reason,
    discard_branch,
    land_record,
    propose_result,
    read_report,
)
from golem.orchestrator.notify import TaskServiceNotifier
from golem.orchestrator.process_runs import ProcessPorts
from golem.orchestrator.reconcile import Landing, Reporter, SucceededRun, reconcile_once
from golem.orchestrator.service import JobTemplate, PostgresOrchestrator
from golem.orchestrator.stages import EdgeStages
from golem.proposal_status import ProposalStates
from golem.ratelimit import Limiter, Rate
from golem.run_status import RunStatuses
from golem.run_token import SigningKey
from golem.runtime.main import Outcome, RunReport, report_json
from golem.serving import Listener
from golem.tasks.__main__ import service_card
from golem.tasks.app import RUN_KEYS_PATH, create_listeners
from golem.tasks.apply import McpApplier, WriteServer
from golem.tasks.store import tasks_engine, tasks_store
from golem.ui import store
from golem.ui.app import create_ui_app
from golem.ui.edge import rpc
from golem.ui.oidc import OidcClient
from support.atlassian import Confluence, Desk, Jira
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
# Goal agents whose proposals the platform applies, and alice reviews (ADR 0015); bob starts
# their runs, so what alice decides is not her own.
REVIEWER = f"user:{USER}"
OWNER = "bob"
REVIEWED = {
    "docs": ("wiki_edit", "Keeps the runbooks true to the running system."),
    "desk": ("desk_reply", "Answers service desk requests about the platform."),
    "triage": ("tracker_issue", "Turns what it finds in logs and metrics into tracker issues."),
}
RUNBOOK = (
    "<h1>Exporter runbook</h1>"
    "<p>The exporter writes the nightly files to the shared drive.</p>"
    "<p>Restart it with <code>systemctl restart exporter</code>.</p>"
    "<p>It keeps 30 days of files.</p>"
    "<h2>Alerts</h2>"
    "<p>ExporterDown fires when no file arrived by 06:00.</p>"
    "<p>Page the on-call engineer of the platform team.</p>"
)
RUNBOOK_EDIT = RUNBOOK.replace(
    "<p>Restart it with <code>systemctl restart exporter</code>.</p>",
    "<p>It runs in Kubernetes now: restart it with"
    " <code>kubectl rollout restart deploy/exporter</code>.</p>",
).replace("<p>It keeps 30 days of files.</p>", "<p>It keeps 14 days of files.</p>")
# One run's branch: the target, golem-proposal.json's fields, and the files they name.
PROPOSED_RUNS: dict[str, tuple[str, str, dict[str, Any], dict[str, str]]] = {
    "wiki": (
        "docs",
        "Bring the exporter runbook in line with the Kubernetes move.",
        {
            "kind": "wiki_edit",
            "page_id": "123",
            "title": "Exporter runbook",
            "version": 7,
            "body_file": "findings/runbook.html",
        },
        {"findings/runbook.html": RUNBOOK_EDIT},
    ),
    "wiki-stale": (
        "docs",
        "Mention the retention change in the exporter runbook.",
        {
            "kind": "wiki_edit",
            "page_id": "123",
            "title": "Exporter runbook",
            "version": 7,
            "body_file": "findings/retention.html",
        },
        {"findings/retention.html": RUNBOOK.replace("30 days", "30 days (14 from October)")},
    ),
    "reply": (
        "desk",
        "Answer SD-12: the export files are missing since Monday.",
        {
            "kind": "desk_reply",
            "request": "SD-12",
            "public": True,
            "text_file": "findings/sd-12.txt",
        },
        {
            "findings/sd-12.txt": "The exporter moved to Kubernetes on Monday and lost its"
            " volume. It is back, and the missing files were written again this morning."
        },
    ),
    "issue": (
        "triage",
        "Look into the exporter's disk usage.",
        {
            "kind": "tracker_issue",
            "action": "create",
            "project": "OPS",
            "issue_type": "Bug",
            "summary": "Exporter volume grows 4% a day",
            "description_file": "findings/disk.txt",
        },
        {
            "findings/disk.txt": "Since the 20th the exporter's volume grows 4% a day; at this"
            " rate it is full in nine days. Retention is not applied to the new path."
        },
    ),
}
REPORTED_RUN = (
    "triage",
    "Check yesterday's error rate of the export API.",
    "findings/error-rate.md",
    "Error rate of the export API stayed under 0.1% all day.\n\nThe spike at 03:10 was the"
    " nightly restart; no request failed.\n",
)
VALIDATION_REASON = (
    "H-3 changed status from 'validated' to 'accepted'; status changes are human decisions"
)


def reviewed_catalog(name: str, kind: str, description: str) -> AgentCatalog:
    """A goal agent of the demo: one role on a record it creates, proposing ``kind``."""
    return AgentCatalog.model_validate(
        {
            "name": name,
            "description": description,
            "version": "0.1.0",
            "context": {"url": f"https://git.example.com/{CONTEXT_PROJECT}.git", "branch": "main"},
            "skills": [{"id": name, "name": description, "description": description}],
            "kinds": [
                {"name": "finding", "initial": "open", "statuses": ["open"], "sections": ["Notes"]}
            ],
            "roles": [{"name": "investigator", "writes": "findings/", "tools": []}],
            "mode": "goal",
            "goal": {"role": "investigator", "kind": "finding"},
            "proposal": kind,
            "reviewers": [REVIEWER],
        }
    )


REVIEWED_CATALOGS = {
    name: reviewed_catalog(name, kind, description)
    for name, (kind, description) in REVIEWED.items()
}


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
    # A goal run's branch by run id: its name, its head commit's message and its files.
    pushed: dict[str, tuple[str, str, dict[str, str]]] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def push(self, run_id: str, target: str, files: dict[str, str], message: str = "") -> None:
        """What a goal run's runtime pushes: its branch, with its files at the head commit."""
        with self.lock:
            self.pushed[run_id] = (f"golem/{target}/{run_id}", message, files)

    def _branch(self, name: str) -> tuple[str, str, dict[str, str]] | None:
        return next((push for push in self.pushed.values() if push[0] == name), None)

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        # A branch name or a file path is one escaped segment: read it undecoded.
        raw = request.url.raw_path.decode().split("?")[0]
        with self.lock:
            if path.endswith("/repository/branches"):
                run_id = request.url.params["search"].strip("/$")
                branch = self.pushed.get(run_id, (f"golem/H-2/{run_id}",))[0]
                return httpx.Response(200, json=[{"name": branch}])
            if "/repository/branches/" in raw:
                return self._head(request.method, unquote(raw.rsplit("/", 1)[1]))
            if "/repository/files/" in raw and raw.endswith("/raw"):
                name = unquote(raw.split("/repository/files/", 1)[1].removesuffix("/raw"))
                return self._file(request.url.params.get("ref", ""), name)
            if "/merge_requests/" in path and path.endswith("/merge"):
                mr = self._numbered(int(path.split("/")[-2]))
                if mr is None:
                    return httpx.Response(404, json={"message": "404 Not found"})
                mr.update(state="merged", merge_user={"username": "golem"})
                return httpx.Response(200, json=mr)
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

    def _head(self, method: str, name: str) -> httpx.Response:
        found = self._branch(name)
        if method == "DELETE":
            self.pushed = {r: p for r, p in self.pushed.items() if p[0] != name}
            return httpx.Response(204)
        # A merge request's branch that no goal run pushed has an empty head, as before.
        message = found[1] if found else ""
        return httpx.Response(
            200, json={"name": name, "commit": {"id": f"sha-{name}", "message": message}}
        )

    def _file(self, ref: str, name: str) -> httpx.Response:
        found = self._branch(ref.removeprefix("sha-"))
        text = found[2].get(name) if found else None
        if text is None:
            return httpx.Response(404, json={"message": "404 File Not Found"})
        return httpx.Response(200, text=text)

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
    # Goal agents propose what write servers apply to these; alice reviews them.
    proposals: bool = False
    confluence: Confluence = field(default_factory=lambda: Confluence(Deployment.CLOUD))
    desk: Desk = field(default_factory=Desk)
    jira: Jira = field(default_factory=Jira)


@contextmanager
def running(
    databases: Databases,
    board_url: str,
    ui_port: int = 0,
    *,
    processes: bool = False,
    proposals: bool = False,
) -> Iterator[Demo]:
    """The stack, with the board served from ``board_url`` (support.front.board_server)."""
    in_own_loop(reset(databases))
    pinned = {PROCESS: PROCESS_CATALOG} if processes else {}
    reviewed = REVIEWED_CATALOGS if proposals else {}
    catalogs = Catalogs(agents=load_catalogs(EXAMPLES).agents | reviewed, processes=pinned)
    limits = ChainLimits(max_depth=3)
    callable_by_people = [PROCESS] if processes else list(reviewed) if proposals else [AGENT]
    # As the edge's start derives it: with a process pinned, people may start only processes.
    registry = derive_registry(
        Registry(allowed_callers={name: frozenset({"user:*"}) for name in callable_by_people}),
        catalogs,
        limits,
    )
    ui_socket = bound(ui_port)
    idp_socket, edge_socket, tasks_socket, outcome_socket, read_socket, bff_socket = (
        bound() for _ in range(6)
    )
    write_sockets = {group: bound() for group in ("wiki.write", "desk.write", "tracker.write")}
    confluence, desk, jira = Confluence(Deployment.CLOUD, body=RUNBOOK), Desk(), Jira()
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
            catalogs={AGENT: catalog, STAGE_AGENT: catalog} | {name: catalog for name in reviewed},
            signing_key=run_key,
            grants={AGENT: ("tracker.read", "wiki.read")},
            processes=pinned,
            proposal_kinds={name: agent.proposal for name, agent in reviewed.items()},
        ),
        edge_token=EDGE_TOKEN,
        task_store=tasks_store(tasks_engine(databases.tasks_url)),
        run_keys=(run_key,),
        applier=McpApplier(
            servers={
                group: WriteServer(
                    url=f"http://127.0.0.1:{port_of(sock)}/mcp", resource=resource_of(group)
                )
                for group, sock in write_sockets.items()
            },
            signing_key=run_key,
        ),
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
        upstreams = {"wiki.write": confluence, "desk.write": desk, "tracker.write": jira}
        for group, sock in write_sockets.items():
            served.append(serve((write_server(group, upstreams[group], read_url, databases), sock)))
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
            reviewers=reviewers_of(catalogs),
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
            proposals=proposals,
            confluence=confluence,
            desk=desk,
            jira=jira,
        )
    finally:
        for each in served:
            each.stop()


def resource_of(group: str) -> str:
    """A write server's canonical URI: the audience of its proposal tokens (ADR 0016)."""
    return f"http://mcp-{group.replace('.', '-')}.demo:8000/mcp"


def write_server(group: str, upstream: Any, read_url: str, databases: Databases) -> ASGIApp:
    """A real write server (gate, tools, audit), its Atlassian faked at the HTTP boundary."""
    keys = SigningKeys(
        partial(fetch_jwks, httpx.Client(base_url=read_url, timeout=5), RUN_KEYS_PATH)
    )
    keys.refresh()
    gate = Gate(
        group=GROUPS[group],
        target_system=f"{GROUPS[group].system}:demo",
        verify=proposal_token_verifier(keys, resource_of(group)),
        statuses=None,
        audit_dsn=databases.dsn("golem_audit", "golem_mcp"),
        proposals=ProposalStates(httpx.AsyncClient(base_url=read_url, timeout=5), ttl_seconds=0),
    )
    base = "https://acme.atlassian.net/wiki" if group == "wiki.write" else "https://jira.demo"
    return create_mcp_app(
        gate=gate,
        upstream=httpx.AsyncClient(transport=httpx.MockTransport(upstream), base_url=base),
        jira_deployment=Deployment.CLOUD,
        confluence_deployment=Deployment.CLOUD,
        allowed=frozenset({"OPS", "SD"}),
    )


def seed(demo: Demo) -> dict[str, str]:
    """alice's tasks, one per state the board shows, or with processes her two processes;
    returns each one's board path by state (a process's as ``process-<state>``)."""
    if demo.proposals:
        return in_own_loop(_seed_proposals(demo))
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


async def _seed_proposals(demo: Demo) -> dict[str, str]:
    """bob's runs of the goal agents alice reviews: four propose, one reports; returns each
    proposal's page by name (``wiki``, ``wiki-stale``, ``reply``, ``issue``) and the report's
    as ``report``."""
    token = demo.idp.access_token(OWNER)
    async with httpx.AsyncClient(base_url=demo.edge_url, timeout=10) as edge:

        async def send(agent: str, key: str, text: str) -> dict[str, Any]:
            message = {"messageId": f"seed-{key}", "role": "ROLE_USER", "parts": [{"text": text}]}
            result = await rpc(edge, token, "SendMessage", {"tenant": agent, "message": message})
            return result["task"]

        runs = {}
        for name, (agent, goal, manifest, files) in PROPOSED_RUNS.items():
            task = await send(agent, name, goal)
            run_id = task["metadata"]["runId"]
            demo.gitlab.push(
                run_id, f"{agent}-{name}", {"golem-proposal.json": json.dumps(manifest)} | files
            )
            demo.launcher.finish(run_id, JobStatus.SUCCEEDED)
            runs[name] = run_id
        # Admission allows four runs at once; the four end before the fifth starts.
        await _reconcile(demo)
        agent, goal, record, text = REPORTED_RUN
        task = await send(agent, "report", goal)
        run_id = task["metadata"]["runId"]
        demo.gitlab.push(run_id, "error-rate", {record: text})
        demo.launcher.finish(
            run_id,
            JobStatus.SUCCEEDED,
            report_json(
                RunReport(run_id=run_id, agent=agent, outcome=Outcome.REPORTED, record=record)
            ),
        )
    await _reconcile(demo)
    async with await psycopg.AsyncConnection.connect(demo.databases.runs) as conn:
        cursor = await conn.execute("SELECT run_id, id FROM proposals")
        proposal_of = {str(run): str(proposal) for run, proposal in await cursor.fetchall()}
    missing = sorted(name for name, run in runs.items() if run not in proposal_of)
    assert not missing, f"no proposal was recorded for {missing}"
    paths = {name: f"/proposals/{proposal_of[run]}" for name, run in runs.items()}
    return paths | {"report": f"/agents/{agent}/reports/{task['id']}"}


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
        projects = {name: project for name in (AGENT, STAGE_AGENT, *REVIEWED)}
        merge_requests = GitLabMergeRequests(client, projects)
        notifier = TaskServiceNotifier(outcomes)

        async def propose(run: SucceededRun) -> Any:
            return await propose_result(merge_requests, run)

        async def land(landing: Landing) -> None:
            await land_record(merge_requests, landing)

        async def read(run: SucceededRun) -> str | None:
            return await read_report(merge_requests, run)

        async def discard(run: SucceededRun) -> None:
            await discard_branch(merge_requests, run)

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
            report=Reporter(read, discard),
            land=land,
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
