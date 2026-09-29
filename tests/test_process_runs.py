"""Processes end to end, in process (ADR 0019): a person starts a process, the reconciler runs
its stages through the edge, and each stage waits for a person's decision in GitLab.

Everything on the path is real: the edge (call tokens the reconciler signs, the registry,
revocation by the process run's status, audit), the task service, the orchestrator and the
reconciler on Postgres. Fakes only at the boundaries: Kubernetes (the cluster below), GitLab
(the documented API shapes) and the identity provider's keys.
"""

import time
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
from decimal import Decimal
from functools import partial
from pathlib import Path
from typing import Any

import httpx
import jwt
import psycopg
import pytest
from starlette.testclient import TestClient
from support.idp import jwk
from test_delegation import AUDIENCE, IDP_KEY, ISSUER, idp_token
from test_merge_requests import PROJECT, TOKEN, FakeGitLab
from test_processes import process
from test_settings import RECONCILER_ENV
from test_tasks_service import make_card
from test_tasks_to_runs import CATALOG, TEMPLATE

from golem import call_token
from golem.call_token import CallClaims
from golem.catalog import ProcessCatalog
from golem.edge.__main__ import authenticator, call_authenticator
from golem.edge.app import create_edge_app
from golem.edge.auth import authenticate_any
from golem.edge.policy import ChainLimits, Registry
from golem.jwks import SigningKeys, fetch_jwks
from golem.orchestrator.admission import Limits
from golem.orchestrator.jobs import JobSpec, JobStatus
from golem.orchestrator.merge_requests import (
    GitLabMergeRequests,
    GitLabProject,
    check_merge_request,
    close_merge_request,
    closing_reason,
    propose_merge_request,
    pushed_branch,
)
from golem.orchestrator.notify import TaskServiceNotifier
from golem.orchestrator.processes import stage_target
from golem.orchestrator.reconcile import LAUNCH_GRACE_SECONDS, ProcessPorts, reconcile_once
from golem.orchestrator.reconciler import stages_for
from golem.orchestrator.service import PostgresOrchestrator
from golem.orchestrator.stages import EdgeStages
from golem.run_status import RunStatuses
from golem.run_token import SigningKey
from golem.settings import reconciler_settings
from golem.tasks.app import RUN_KEYS_PATH, create_listeners

EDGE_TOKEN = "edge-shared-secret"
RUN_KEY = SigningKey.generate(kid="golem-1")
FEATURE = "corsar-feature"
STAGE_AGENTS = ("analyst", "designer", "developer")
REGISTRY = Registry(
    allowed_callers={
        FEATURE: frozenset({"user:*"}),
        **{agent: frozenset({f"agent:{FEATURE}"}) for agent in STAGE_AGENTS},
    }
)
LIMITS = Limits(max_runs_per_caller=1, max_runs_per_root=5, budget_per_root=Decimal("10"))


@dataclass
class Cluster:
    """Kubernetes as the reconciler sees it: a Job it never launched is missing."""

    launched: list[JobSpec] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    statuses: dict[str, JobStatus] = field(default_factory=dict)

    def launch(self, spec: JobSpec) -> None:
        self.launched.append(spec)

    def status(self, run_id: str) -> JobStatus:
        if run_id not in {spec.run_id for spec in self.launched}:
            return JobStatus.MISSING
        return self.statuses.get(run_id, JobStatus.RUNNING)

    def delete(self, run_id: str) -> None:
        self.deleted.append(run_id)

    def termination_message(self, run_id: str) -> str | None:
        return None


@dataclass
class Golem:
    edge: httpx.AsyncClient
    cluster: Cluster
    gitlab: FakeGitLab
    merge_requests: GitLabMergeRequests
    notifier: TaskServiceNotifier
    stages: EdgeStages
    runs_dsn: str

    async def rpc(self, token: str, method: str, params: dict[str, Any]) -> dict[str, Any]:
        response = await self.edge.post(
            "/a2a",
            json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
            headers={"Authorization": f"Bearer {token}", "A2A-Version": "1.0"},
        )
        return response.json()

    async def start(self, user: str, text: str, message_id: str = "m-1") -> dict[str, Any]:
        message = {"role": "ROLE_USER", "messageId": message_id, "parts": [{"text": text}]}
        answer = await self.rpc(
            idp_token(user), "SendMessage", {"tenant": FEATURE, "message": message}
        )
        return answer["result"]["task"]

    async def task(self, user: str, task_id: str) -> dict[str, Any]:
        answer = await self.rpc(idp_token(user), "GetTask", {"tenant": FEATURE, "id": task_id})
        return answer["result"]

    async def cancel(self, user: str, task_id: str) -> dict[str, Any]:
        return await self.rpc(idp_token(user), "CancelTask", {"tenant": FEATURE, "id": task_id})

    async def resolve(self, token: str, task_id: str, body: dict[str, Any]) -> httpx.Response:
        return await self.edge.post(
            f"/processes/{task_id}/resolution",
            json=body,
            headers={"Authorization": f"Bearer {token}"},
        )

    async def reconcile(self, passes: int = 3) -> None:
        async def propose(run: Any) -> Any:
            return await propose_merge_request(self.merge_requests, run)

        async def proposed(run: Any) -> Any:
            return await pushed_branch(self.merge_requests, run)

        async def check(pending: Any) -> Any:
            return await check_merge_request(self.merge_requests, pending)

        ports = ProcessPorts(
            start=self.stages.start,
            closing_reason=partial(closing_reason, self.merge_requests),
            close_merge_request=partial(close_merge_request, self.merge_requests),
        )
        for _ in range(passes):
            async with await psycopg.AsyncConnection.connect(self.runs_dsn, autocommit=True) as c:
                await reconcile_once(
                    c,
                    self.cluster,
                    self.notifier.notify,
                    propose,
                    proposed=proposed,
                    check=check,
                    notify_proposal=self.notifier.notify_proposal,
                    poll_seconds=0,
                    processes=ports,
                    notify_process=self.notifier.notify_process,
                )

    def stage(self, agent: str, attempt: int = -1) -> JobSpec:
        return [spec for spec in self.cluster.launched if spec.agent == agent][attempt]

    def succeed(self, spec: JobSpec) -> None:
        """The stage's Job pushed its branch and ended well."""
        self.gitlab.branches.append(f"golem/{spec.target}/{spec.run_id}")
        self.cluster.statuses[spec.run_id] = JobStatus.SUCCEEDED

    def merge_request_of(self, spec: JobSpec) -> dict[str, Any]:
        [mr] = [
            mr for mr in self.gitlab.merge_requests if mr["source_branch"].endswith(spec.run_id)
        ]
        return mr

    def merge(self, spec: JobSpec) -> None:
        self.merge_request_of(spec).update(
            state="merged", merge_user={"username": "bob"}, merged_at="2026-09-28T10:00:00Z"
        )

    def close(self, spec: JobSpec, comment: str | None = None) -> None:
        mr = self.merge_request_of(spec)
        mr.update(state="closed", closed_by={"username": "bob"}, closed_at="2026-09-28T10:00:00Z")
        if comment is not None:
            self.gitlab.notes[mr["iid"]] = [
                {
                    "author": {"username": "bob"},
                    "created_at": "2026-09-28T09:59:00Z",
                    "body": comment,
                    "system": False,
                }
            ]

    async def through(self, agent: str) -> JobSpec:
        """Run the stage of `agent` to a merged merge request."""
        spec = self.stage(agent)
        self.succeed(spec)
        await self.reconcile()
        self.merge(spec)
        await self.reconcile()
        return spec


@pytest.fixture
def cluster() -> Cluster:
    return Cluster()


@pytest.fixture
def listeners_and_read(runs_db: str, cluster: Cluster) -> Iterator[tuple[Any, TestClient]]:
    orchestrator = PostgresOrchestrator(
        dsn=runs_db,
        limits=LIMITS,
        estimated_cost=Decimal("1"),
        launcher=cluster,
        template=TEMPLATE,
        catalogs={agent: CATALOG for agent in STAGE_AGENTS},
        signing_key=RUN_KEY,
        grants={},
        processes={FEATURE: process()},
    )
    listeners = create_listeners(
        make_card(), orchestrator, edge_token=EDGE_TOKEN, run_keys=(RUN_KEY,)
    )
    with TestClient(listeners.internal_read) as read_port:
        yield listeners, read_port


@pytest.fixture
async def golem(
    listeners_and_read: tuple[Any, TestClient],
    cluster: Cluster,
    runs_db: str,
    audit_dsn: str,
    audit_admin_dsn: str,
) -> AsyncIterator[Golem]:
    listeners, read_port = listeners_and_read
    idp_keys = SigningKeys(lambda: jwt.PyJWKSet.from_dict({"keys": [jwk(IDP_KEY, "idp-1")]}))
    golem_keys = SigningKeys(partial(fetch_jwks, read_port, RUN_KEYS_PATH))
    edge = create_edge_app(
        authenticate=partial(
            authenticate_any,
            idp=authenticator(idp_keys, issuer=ISSUER, audience=AUDIENCE),
            golem=call_authenticator(golem_keys),
        ),
        registry=REGISTRY,
        limits=ChainLimits(max_depth=3),
        audit_dsn=audit_dsn,
        forward=httpx.AsyncClient(
            transport=httpx.ASGITransport(app=listeners.public), base_url="http://tasks"
        ),
        edge_token=EDGE_TOKEN,
        cards={},
        run_statuses=RunStatuses(
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=listeners.internal_read),
                base_url="http://tasks-read",
            ),
            # Revocation must see a canceled process at once.
            ttl_seconds=0,
        ),
    )
    gitlab = FakeGitLab()
    project = GitLabProject(path=PROJECT, target_branch="main")
    async with (
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=edge, client=("10.0.0.9", 40000)),
            base_url="http://edge",
        ) as edge_client,
        httpx.AsyncClient(
            transport=httpx.MockTransport(gitlab.handle),
            base_url="https://gitlab.example.test/api/v4",
            headers={"PRIVATE-TOKEN": TOKEN},
        ) as gitlab_client,
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=listeners.internal_write),
            base_url="http://tasks-write",
        ) as write_client,
    ):
        yield Golem(
            edge=edge_client,
            cluster=cluster,
            gitlab=gitlab,
            merge_requests=GitLabMergeRequests(
                gitlab_client, {agent: project for agent in STAGE_AGENTS}
            ),
            notifier=TaskServiceNotifier(write_client),
            stages=EdgeStages(client=edge_client, signing_key=RUN_KEY),
            runs_dsn=runs_db,
        )


def process_of(task: dict[str, Any]) -> dict[str, Any]:
    return task["metadata"]["golemProcess"]


# A process is a task of its own and a run with no Job.


async def test_a_process_starts_as_a_task_whose_run_launches_no_job(golem: Golem) -> None:
    task = await golem.start("alice", "Add a CSV export.")

    assert task["status"]["state"] == "TASK_STATE_WORKING"
    assert task["metadata"]["golemAgent"] == FEATURE
    assert "runId" in task["metadata"]
    assert golem.cluster.launched == []


async def test_the_reconciler_does_not_fail_a_process_run_for_having_no_job(
    golem: Golem, runs_db: str
) -> None:
    task = await golem.start("alice", "Add a CSV export.")
    async with await psycopg.AsyncConnection.connect(runs_db, autocommit=True) as conn:
        await conn.execute(
            "UPDATE runs SET created_at = now() - make_interval(secs => %s) WHERE id = %s",
            (LAUNCH_GRACE_SECONDS + 1, task["metadata"]["runId"]),
        )

    await golem.reconcile()

    process_task = await golem.task("alice", task["id"])
    assert process_task["status"]["state"] == "TASK_STATE_WORKING"


# Stages start through the edge, one after another, as each proposal is merged.


async def test_the_first_stage_starts_with_the_persons_input_on_its_own_target(
    golem: Golem, audit_admin_dsn: str
) -> None:
    task = await golem.start("alice", "Add a CSV export.")
    process_run = task["metadata"]["runId"]

    await golem.reconcile()

    spec = golem.stage("analyst")
    assert spec.goal.startswith("Analyse: Add a CSV export.")
    assert "stage 1 of 3, analysis" in spec.goal
    assert spec.target == stage_target(process_run, "analysis")
    shown = process_of(await golem.task("alice", task["id"]))
    stage_task = shown.pop("stageTaskId")
    assert shown == {
        "state": "running",
        "stage": "analysis",
        "index": 0,
        "count": 3,
        "attempt": 0,
        "maxAttempts": 3,
        "staleReruns": 0,
        "proposal": None,
    }
    # The stage's task is the person's, under the process's chain. People may not call a
    # stage agent, so the person reads it through the process, the tenant they may call.
    stage = await golem.task("alice", stage_task)
    assert stage["metadata"]["chain"] == [FEATURE]
    async with await psycopg.AsyncConnection.connect(audit_admin_dsn) as conn:
        rows = await (
            await conn.execute(
                "SELECT account, target_system, result, chain FROM audit_log"
                " WHERE target_system = 'agent:analyst'"
            )
        ).fetchall()
    assert rows == [("user:alice", "agent:analyst", "allow", [FEATURE])]


async def test_a_process_whose_goal_would_be_too_long_is_refused_at_its_start(
    golem: Golem,
) -> None:
    # "Analyse: " plus 4000 characters is over what a stage's goal may be.
    task = await golem.start("alice", "x" * 4000)

    assert task["status"]["state"] == "TASK_STATE_REJECTED"
    await golem.reconcile()
    assert golem.cluster.launched == []


async def test_a_stage_goal_that_cannot_be_written_fails_the_process(
    golem: Golem, runs_db: str
) -> None:
    task = await golem.start("alice", "Add a CSV export.")
    async with await psycopg.AsyncConnection.connect(runs_db, autocommit=True) as conn:
        await conn.execute(
            "UPDATE process_stages SET input = %s WHERE process_run_id = %s",
            ("x" * 4000, task["metadata"]["runId"]),
        )

    await golem.reconcile()

    process_task = await golem.task("alice", task["id"])
    assert process_task["status"]["state"] == "TASK_STATE_FAILED"
    assert process_of(process_task)["reason"] == "invalid_goal"
    assert golem.cluster.launched == []


async def test_a_stage_held_back_by_its_owners_other_runs_starts_once_they_end(
    golem: Golem,
) -> None:
    # LIMITS allow alice one running run: the first process's stage holds it.
    first = await golem.start("alice", "Add a CSV export.", message_id="m-1")
    second = await golem.start("alice", "Add a PDF export.", message_id="m-2")
    await golem.reconcile()
    [held] = golem.cluster.launched

    process_task = await golem.task("alice", second["id"])
    assert process_task["status"]["state"] == "TASK_STATE_WORKING"
    golem.succeed(held)
    await golem.reconcile()

    assert len(golem.cluster.launched) == 2
    assert process_of(await golem.task("alice", first["id"]))["state"] == "running"
    assert process_of(await golem.task("alice", second["id"]))["state"] == "running"


async def test_a_stage_takes_no_second_job_slot_for_its_process(golem: Golem) -> None:
    # LIMITS allow alice one running run: the process run must not be it.
    await golem.start("alice", "Add a CSV export.")

    await golem.reconcile()

    assert [spec.agent for spec in golem.cluster.launched] == ["analyst"]


async def test_the_stage_proposal_shows_on_the_process_task(golem: Golem) -> None:
    task = await golem.start("alice", "Add a CSV export.")
    await golem.reconcile()
    golem.succeed(golem.stage("analyst"))

    await golem.reconcile()

    process_task = await golem.task("alice", task["id"])
    proposal = process_of(process_task)["proposal"]
    assert (proposal["kind"], proposal["state"]) == ("merge_request", "pending")
    assert process_task["metadata"]["golemProposal"] == proposal


async def test_a_merged_stage_starts_the_next_with_the_accepted_records_named(
    golem: Golem,
) -> None:
    task = await golem.start("alice", "Add a CSV export.")
    process_run = task["metadata"]["runId"]
    await golem.reconcile()

    await golem.through("analyst")

    design = golem.stage("designer")
    assert design.goal.startswith("Design the accepted analysis.")
    assert stage_target(process_run, "analysis") in design.goal
    shown = process_of(await golem.task("alice", task["id"]))
    assert (shown["stage"], shown["index"], shown["proposal"]) == ("design", 1, None)


async def test_the_last_merged_stage_completes_the_process(golem: Golem) -> None:
    task = await golem.start("alice", "Add a CSV export.")
    await golem.reconcile()

    for agent in STAGE_AGENTS:
        await golem.through(agent)

    process_task = await golem.task("alice", task["id"])
    assert process_task["status"]["state"] == "TASK_STATE_COMPLETED"
    shown = process_of(process_task)
    assert (shown["state"], shown["stage"]) == ("completed", "implementation")
    assert shown["proposal"]["state"] == "applied"
    assert [spec.agent for spec in golem.cluster.launched] == list(STAGE_AGENTS)


# A rejection reruns the stage with the person's reason, within the return limit.


async def test_a_merge_request_closed_with_a_comment_reruns_the_stage_with_it(
    golem: Golem,
) -> None:
    task = await golem.start("alice", "Add a CSV export.")
    await golem.reconcile()
    first = golem.stage("analyst")
    golem.succeed(first)
    await golem.reconcile()

    golem.close(first, "The analysis misses the Kafka contract.")
    await golem.reconcile()

    second = golem.stage("analyst")
    assert second.run_id != first.run_id
    assert second.target == first.target
    assert "A person rejected the previous attempt (gitlab:bob):" in second.goal
    assert "The analysis misses the Kafka contract." in second.goal
    shown = process_of(await golem.task("alice", task["id"]))
    assert (shown["state"], shown["attempt"], shown["proposal"]) == ("running", 1, None)


async def test_rejections_past_the_return_limit_fail_the_process(golem: Golem) -> None:
    task = await golem.start("alice", "Add a CSV export.")
    await golem.reconcile()

    for _ in range(3):
        spec = golem.stage("analyst")
        golem.succeed(spec)
        await golem.reconcile()
        golem.close(spec, "Still wrong.")
        await golem.reconcile()

    process_task = await golem.task("alice", task["id"])
    assert len(golem.cluster.launched) == 3
    assert process_task["status"]["state"] == "TASK_STATE_FAILED"
    shown = process_of(process_task)
    assert (shown["state"], shown["reason"]) == ("failed", "return_limit")


async def test_a_failed_stage_run_fails_the_process(golem: Golem) -> None:
    task = await golem.start("alice", "Add a CSV export.")
    await golem.reconcile()
    golem.cluster.statuses[golem.stage("analyst").run_id] = JobStatus.FAILED

    await golem.reconcile()

    process_task = await golem.task("alice", task["id"])
    assert process_task["status"]["state"] == "TASK_STATE_FAILED"
    assert process_of(process_task)["state"] == "failed"


async def test_a_stale_proposal_reruns_the_stage_without_spending_an_attempt(
    golem: Golem, runs_db: str
) -> None:
    task = await golem.start("alice", "Add a CSV export.")
    await golem.reconcile()
    first = golem.stage("analyst")
    golem.succeed(first)
    await golem.reconcile()
    async with await psycopg.AsyncConnection.connect(runs_db, autocommit=True) as conn:
        await conn.execute(
            "UPDATE proposals SET state = 'stale' WHERE run_id = %s", (first.run_id,)
        )

    await golem.reconcile()

    second = golem.stage("analyst")
    assert second.run_id != first.run_id
    assert "changed since you read it" in second.goal
    shown = process_of(await golem.task("alice", task["id"]))
    assert (shown["attempt"], shown["staleReruns"]) == (0, 1)


# A merge request closed without a word waits for its owner's reason on the board.


async def closed_without_a_word(golem: Golem) -> dict[str, Any]:
    task = await golem.start("alice", "Add a CSV export.")
    await golem.reconcile()
    spec = golem.stage("analyst")
    golem.succeed(spec)
    await golem.reconcile()
    golem.close(spec)
    await golem.reconcile()
    return task


async def test_a_merge_request_closed_without_a_comment_waits_for_a_reason(
    golem: Golem,
) -> None:
    task = await closed_without_a_word(golem)

    process_task = await golem.task("alice", task["id"])
    assert process_task["status"]["state"] == "TASK_STATE_WORKING"
    assert process_of(process_task)["state"] == "needs_reason"
    assert len(golem.cluster.launched) == 1


async def test_the_owner_reruns_a_waiting_stage_with_a_reason(
    golem: Golem, audit_admin_dsn: str
) -> None:
    task = await closed_without_a_word(golem)

    response = await golem.resolve(
        idp_token("alice"), task["id"], {"action": "rerun", "reason": "Cover the export API."}
    )
    await golem.reconcile()

    assert response.status_code == 200
    second = golem.stage("analyst")
    assert "Cover the export API." in second.goal
    assert "(user:alice)" in second.goal
    async with await psycopg.AsyncConnection.connect(audit_admin_dsn) as conn:
        row = await (
            await conn.execute(
                "SELECT account, target_system, operation, result, request FROM audit_log"
                " WHERE operation = 'ResolveProcess'"
            )
        ).fetchone()
    assert row == (
        "user:alice",
        "processes",
        "ResolveProcess",
        "allow",
        f"ResolveProcess task={task['id']} action=rerun",
    )


async def test_the_owner_ends_a_waiting_process(golem: Golem) -> None:
    task = await closed_without_a_word(golem)

    response = await golem.resolve(idp_token("alice"), task["id"], {"action": "end"})
    await golem.reconcile()

    assert response.status_code == 200
    process_task = await golem.task("alice", task["id"])
    assert process_task["status"]["state"] == "TASK_STATE_FAILED"
    assert process_of(process_task)["reason"] == "ended_by_owner"


async def test_a_rerun_needs_a_reason(golem: Golem) -> None:
    task = await closed_without_a_word(golem)

    response = await golem.resolve(idp_token("alice"), task["id"], {"action": "rerun"})

    assert (response.status_code, response.json()["error"]) == (400, "reason_required")


async def test_only_the_owner_resolves_a_process(golem: Golem) -> None:
    task = await closed_without_a_word(golem)

    response = await golem.resolve(idp_token("bob"), task["id"], {"action": "end"})

    assert response.status_code == 404
    assert process_of(await golem.task("alice", task["id"]))["state"] == "needs_reason"


async def test_a_process_not_waiting_is_not_resolved(golem: Golem) -> None:
    task = await golem.start("alice", "Add a CSV export.")
    await golem.reconcile()

    response = await golem.resolve(idp_token("alice"), task["id"], {"action": "end"})

    assert (response.status_code, response.json()["error"]) == (409, "not_waiting")


async def test_an_agent_does_not_resolve_a_process(golem: Golem) -> None:
    task = await closed_without_a_word(golem)
    now = int(time.time())
    token = call_token.issue(
        CallClaims(
            subject="user:alice",
            agent=FEATURE,
            chain=(FEATURE,),
            root_run_id=task["metadata"]["runId"],
            run_id=task["metadata"]["runId"],
            expires_at=now + 60,
        ),
        RUN_KEY,
        now,
    )

    response = await golem.resolve(token, task["id"], {"action": "end"})

    assert (response.status_code, response.json()["error"]) == (403, "agents_do_not_decide")


# Canceling a process withdraws its current stage.


async def test_canceling_a_process_cancels_its_running_stage(golem: Golem) -> None:
    task = await golem.start("alice", "Add a CSV export.")
    await golem.reconcile()
    spec = golem.stage("analyst")
    stage_task = process_of(await golem.task("alice", task["id"]))["stageTaskId"]

    await golem.cancel("alice", task["id"])
    await golem.reconcile()

    assert golem.cluster.deleted == [spec.run_id]
    stage = await golem.task("alice", stage_task)
    assert stage["status"]["state"] == "TASK_STATE_CANCELED"
    assert process_of(await golem.task("alice", task["id"]))["state"] == "canceled"


async def test_canceling_a_process_closes_the_merge_request_waiting_for_review(
    golem: Golem, runs_db: str
) -> None:
    task = await golem.start("alice", "Add a CSV export.")
    await golem.reconcile()
    spec = golem.stage("analyst")
    golem.succeed(spec)
    await golem.reconcile()

    await golem.cancel("alice", task["id"])
    await golem.reconcile()

    assert golem.merge_request_of(spec)["state"] == "closed"
    async with await psycopg.AsyncConnection.connect(runs_db, autocommit=True) as conn:
        row = await (
            await conn.execute(
                "SELECT state, decided_by, detail FROM proposals WHERE run_id = %s",
                (spec.run_id,),
            )
        ).fetchone()
    assert row == ("rejected", "user:alice", "process_canceled")
    # Nothing more starts under a canceled process.
    assert len(golem.cluster.launched) == 1


async def test_a_canceled_process_proposes_nothing_its_stage_left_unsettled(
    golem: Golem,
) -> None:
    # The stage succeeded while GitLab was down, so no merge request was opened yet.
    task = await golem.start("alice", "Add a CSV export.")
    await golem.reconcile()
    spec = golem.stage("analyst")
    golem.succeed(spec)
    golem.gitlab.down = True
    await golem.reconcile()

    await golem.cancel("alice", task["id"])
    await golem.reconcile()
    golem.gitlab.down = False
    await golem.reconcile()

    assert golem.gitlab.merge_requests == []
    assert process_of(await golem.task("alice", task["id"]))["state"] == "canceled"


async def test_canceling_a_process_cancels_a_stage_run_not_yet_linked_to_it(
    golem: Golem, runs_db: str
) -> None:
    task = await golem.start("alice", "Add a CSV export.")
    await golem.reconcile()
    spec = golem.stage("analyst")
    # The stage's task was recorded, but the pass that would link its run did not get there.
    async with await psycopg.AsyncConnection.connect(runs_db, autocommit=True) as conn:
        await conn.execute(
            "UPDATE process_stages SET run_id = NULL WHERE process_run_id = %s",
            (task["metadata"]["runId"],),
        )

    await golem.cancel("alice", task["id"])
    await golem.reconcile()

    assert golem.cluster.deleted == [spec.run_id]


def test_a_process_definition_survives_as_json() -> None:
    # What the orchestrator pins at start is what the reconciler runs from, days later.
    definition = process().model_dump(mode="json")

    assert ProcessCatalog.model_validate(definition) == process()


async def test_the_reconciler_starts_stages_only_when_given_the_edge_and_the_key(
    tmp_path: Path,
) -> None:
    key_file = tmp_path / "key.pem"
    key_file.write_text(RUN_KEY.private_pem)
    env = {
        **RECONCILER_ENV,
        "GOLEM_EDGE_URL": "http://edge:8000",
        "GOLEM_RUN_TOKEN_KEY_FILE": str(key_file),
        "GOLEM_RUN_TOKEN_KID": "golem-1",
    }
    async with httpx.AsyncClient(base_url="http://edge:8000") as client:
        stages = stages_for(reconciler_settings(env), client)

        assert stages is not None
        assert stages.signing_key.kid == "golem-1"
        assert stages_for(reconciler_settings(RECONCILER_ENV), client) is None
