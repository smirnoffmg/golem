"""Merge requests for succeeded runs, against a GitLab REST API stand-in and real Postgres.

The stand-in implements only the documented shapes Golem uses:
https://docs.gitlab.com/api/branches/#list-repository-branches
https://docs.gitlab.com/api/merge_requests/#list-project-merge-requests
https://docs.gitlab.com/api/merge_requests/#create-a-merge-request
"""

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest
from test_reconcile import Inbox, StatusBoard, connect, new_run, recorded, run_status

from golem.orchestrator.jobs import JobStatus
from golem.orchestrator.merge_requests import (
    GitLabError,
    GitLabMergeRequests,
    GitLabProject,
    propose_merge_request,
    run_branch,
)
from golem.orchestrator.reconcile import SucceededRun, reconcile_once

PROJECT = "product/discovery-context"
ENCODED_PROJECT = "product%2Fdiscovery-context"
TOKEN = "glpat-test"


@dataclass
class FakeGitLab:
    branches: list[str] = field(default_factory=list)
    merge_requests: list[dict[str, Any]] = field(default_factory=list)
    requests: list[httpx.Request] = field(default_factory=list)
    down: bool = False

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.down:
            return httpx.Response(503, json={"message": "503 Service Unavailable"})
        if request.headers.get("PRIVATE-TOKEN") != TOKEN:
            return httpx.Response(401, json={"message": "401 Unauthorized"})
        prefix = f"/api/v4/projects/{ENCODED_PROJECT}"
        path = request.url.raw_path.decode().split("?")[0]
        if path == f"{prefix}/repository/branches" and request.method == "GET":
            return httpx.Response(200, json=self._branches(request.url.params.get("search", "")))
        if path == f"{prefix}/merge_requests" and request.method == "GET":
            return httpx.Response(200, json=self._merge_requests(request.url.params))
        if path == f"{prefix}/merge_requests" and request.method == "POST":
            return self._create(json.loads(request.content))
        return httpx.Response(404, json={"message": "404 Not Found"})

    def _branches(self, search: str) -> list[dict[str, Any]]:
        begins, ends = search.startswith("^"), search.endswith("$")
        term = search.removeprefix("^").removesuffix("$")

        def matches(name: str) -> bool:
            if begins and ends:
                return name == term
            if begins:
                return name.startswith(term)
            if ends:
                return name.endswith(term)
            return term in name

        return [{"name": name, "merged": False} for name in self.branches if matches(name)]

    def _merge_requests(self, params: httpx.QueryParams) -> list[dict[str, Any]]:
        state = params.get("state", "all")
        return [
            mr
            for mr in self.merge_requests
            if mr["source_branch"] == params.get("source_branch", mr["source_branch"])
            and state in ("all", mr["state"])
        ]

    def _create(self, body: dict[str, Any]) -> httpx.Response:
        missing = [k for k in ("source_branch", "target_branch", "title") if not body.get(k)]
        if missing:
            return httpx.Response(400, json={"error": f"{missing[0]} is missing"})
        iid = len(self.merge_requests) + 1
        mr = {
            "iid": iid,
            "state": "opened",
            "web_url": f"https://gitlab.example.test/{PROJECT}/-/merge_requests/{iid}",
            **body,
        }
        self.merge_requests.append(mr)
        return httpx.Response(201, json=mr)


@pytest.fixture
def gitlab() -> FakeGitLab:
    return FakeGitLab()


@pytest.fixture
async def merge_requests(gitlab: FakeGitLab) -> AsyncIterator[GitLabMergeRequests]:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(gitlab.handle),
        base_url="https://gitlab.example.test/api/v4",
        headers={"PRIVATE-TOKEN": TOKEN},
    ) as client:
        yield GitLabMergeRequests(
            client, {"discovery": GitLabProject(path=PROJECT, target_branch="main")}
        )


def test_the_run_branch_is_golem_target_run() -> None:
    names = ["golem/H-1/r2", "golem/H-7/r1", "golem/H-7/xr1", "feature/r1", "golem/a/b/r1"]

    assert run_branch(names, "r1") == "golem/H-7/r1"
    assert run_branch(names, "r3") is None


async def test_the_branch_of_a_run_is_found_by_its_run_id_suffix(
    gitlab: FakeGitLab, merge_requests: GitLabMergeRequests
) -> None:
    gitlab.branches = ["main", "golem/H-7/run-1", "golem/H-8/run-11"]

    assert await merge_requests.find_branch("discovery", "run-1") == "golem/H-7/run-1"
    [request] = gitlab.requests
    assert request.url.params["search"] == "/run-1$"


async def test_no_branch_means_none(merge_requests: GitLabMergeRequests) -> None:
    assert await merge_requests.find_branch("discovery", "run-1") is None


async def test_open_creates_a_merge_request_into_the_target_branch(
    gitlab: FakeGitLab, merge_requests: GitLabMergeRequests
) -> None:
    url = await merge_requests.open(
        "discovery", "run-1", "golem/H-7/run-1", "Proposal for H-7", "From run run-1."
    )

    [mr] = gitlab.merge_requests
    assert url == mr["web_url"]
    assert (mr["source_branch"], mr["target_branch"]) == ("golem/H-7/run-1", "main")
    assert (mr["title"], mr["description"]) == ("Proposal for H-7", "From run run-1.")


async def test_open_is_idempotent_per_source_branch(
    gitlab: FakeGitLab, merge_requests: GitLabMergeRequests
) -> None:
    first = await merge_requests.open("discovery", "run-1", "golem/H-7/run-1", "t", "d")
    second = await merge_requests.open("discovery", "run-1", "golem/H-7/run-1", "t", "d")

    assert first == second
    assert len(gitlab.merge_requests) == 1
    lookup = gitlab.requests[-1]
    assert lookup.method == "GET"
    assert lookup.url.params["source_branch"] == "golem/H-7/run-1"
    assert lookup.url.params["state"] == "opened"


async def test_a_closed_merge_request_does_not_count_as_open(
    gitlab: FakeGitLab, merge_requests: GitLabMergeRequests
) -> None:
    gitlab.merge_requests.append(
        {"iid": 9, "state": "closed", "source_branch": "golem/H-7/run-1", "web_url": "old"}
    )

    url = await merge_requests.open("discovery", "run-1", "golem/H-7/run-1", "t", "d")

    assert url != "old"


async def test_gitlab_errors_become_gitlab_error(
    gitlab: FakeGitLab, merge_requests: GitLabMergeRequests
) -> None:
    gitlab.down = True

    with pytest.raises(GitLabError, match="503"):
        await merge_requests.find_branch("discovery", "run-1")


async def test_an_unreachable_gitlab_becomes_gitlab_error() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route", request=request)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(refuse), base_url="https://gitlab.example.test/api/v4"
    ) as client:
        gitlab = GitLabMergeRequests(client, {"discovery": GitLabProject(PROJECT, "main")})
        with pytest.raises(GitLabError):
            await gitlab.find_branch("discovery", "run-1")


async def test_an_agent_without_a_project_is_a_gitlab_error(
    merge_requests: GitLabMergeRequests,
) -> None:
    with pytest.raises(GitLabError, match="reviewer"):
        await merge_requests.find_branch("reviewer", "run-1")


async def test_a_run_of_an_agent_without_a_project_settles_instead_of_retrying_forever(
    merge_requests: GitLabMergeRequests,
) -> None:
    detail = await propose_merge_request(merge_requests, SucceededRun("run-1", "reviewer"))

    assert "no GitLab project is configured for agent 'reviewer'" in detail
    assert "not proposed" in detail


async def test_a_run_without_a_branch_proposed_nothing(
    merge_requests: GitLabMergeRequests,
) -> None:
    detail = await propose_merge_request(merge_requests, SucceededRun("run-1", "discovery"))

    assert detail == "Run run-1 succeeded and proposed no changes."


async def test_a_run_with_a_branch_proposes_a_merge_request(
    gitlab: FakeGitLab, merge_requests: GitLabMergeRequests
) -> None:
    gitlab.branches = ["golem/H-7/run-1"]

    detail = await propose_merge_request(merge_requests, SucceededRun("run-1", "discovery"))

    [mr] = gitlab.merge_requests
    assert mr["web_url"] in detail
    assert "H-7" in mr["title"]
    assert "run-1" in mr["description"]


# The outbox: a succeeded run's tasks are notified only after its merge request is settled.


async def reconcile(dsn: str, board: StatusBoard, inbox: Inbox, gitlab: GitLabMergeRequests):
    async def propose(run: SucceededRun) -> str:
        return await propose_merge_request(gitlab, run)

    async with await connect(dsn) as conn:
        await reconcile_once(conn, board, inbox.notify, propose)


async def test_a_succeeded_run_notifies_its_task_with_the_merge_request_url(
    runs_db: str, gitlab: FakeGitLab, merge_requests: GitLabMergeRequests
) -> None:
    board, inbox = StatusBoard(), Inbox()
    run_id = await new_run(runs_db, "m-1")
    gitlab.branches = [f"golem/H-7/{run_id}"]
    board.statuses[run_id] = JobStatus.SUCCEEDED

    await reconcile(runs_db, board, inbox, merge_requests)
    await reconcile(runs_db, board, inbox, merge_requests)

    [mr] = gitlab.merge_requests
    [outcome] = inbox.received
    run = await recorded(runs_db, outcome.task_id)
    assert run.status == "succeeded"
    assert mr["web_url"] in run.detail


async def test_an_idle_run_notifies_that_it_proposed_nothing(
    runs_db: str, merge_requests: GitLabMergeRequests
) -> None:
    board, inbox = StatusBoard(), Inbox()
    run_id = await new_run(runs_db, "m-1")
    board.statuses[run_id] = JobStatus.SUCCEEDED

    await reconcile(runs_db, board, inbox, merge_requests)

    [outcome] = inbox.received
    run = await recorded(runs_db, outcome.task_id)
    assert run.detail == f"Run {run_id} succeeded and proposed no changes."


async def test_a_gitlab_outage_holds_the_notification_until_the_merge_request_opens(
    runs_db: str, gitlab: FakeGitLab, merge_requests: GitLabMergeRequests
) -> None:
    board, inbox = StatusBoard(), Inbox()
    run_id = await new_run(runs_db, "m-1")
    gitlab.branches = [f"golem/H-7/{run_id}"]
    board.statuses[run_id] = JobStatus.SUCCEEDED
    gitlab.down = True

    await reconcile(runs_db, board, inbox, merge_requests)
    await reconcile(runs_db, board, inbox, merge_requests)

    assert await run_status(runs_db, run_id) == "succeeded"
    assert inbox.received == []

    gitlab.down = False
    await reconcile(runs_db, board, inbox, merge_requests)

    [mr] = gitlab.merge_requests
    [outcome] = inbox.received
    assert mr["web_url"] in (await recorded(runs_db, outcome.task_id)).detail


async def test_one_run_failing_to_propose_does_not_hold_back_the_others(
    runs_db: str, gitlab: FakeGitLab, merge_requests: GitLabMergeRequests
) -> None:
    board, inbox = StatusBoard(), Inbox()
    stuck = await new_run(runs_db, "m-1")
    fine = await new_run(runs_db, "m-2")
    failed = await new_run(runs_db, "m-3")
    board.statuses |= {stuck: JobStatus.SUCCEEDED, fine: JobStatus.SUCCEEDED}
    board.statuses[failed] = JobStatus.FAILED

    async def propose(run: SucceededRun) -> str:
        if run.run_id == stuck:
            raise GitLabError("merge request refused")
        return await propose_merge_request(merge_requests, run)

    async with await connect(runs_db) as conn:
        await reconcile_once(conn, board, inbox.notify, propose)

    assert sorted(o.run_id for o in inbox.received) == sorted([fine, failed])


async def test_a_merge_request_opened_before_a_crash_is_not_opened_twice(
    runs_db: str, gitlab: FakeGitLab, merge_requests: GitLabMergeRequests
) -> None:
    board, inbox = StatusBoard(), Inbox()
    run_id = await new_run(runs_db, "m-1")
    branch = f"golem/H-7/{run_id}"
    gitlab.branches = [branch]
    board.statuses[run_id] = JobStatus.SUCCEEDED
    await merge_requests.open("discovery", run_id, branch, "t", "d")

    await reconcile(runs_db, board, inbox, merge_requests)

    assert len(gitlab.merge_requests) == 1
    [outcome] = inbox.received
    assert gitlab.merge_requests[0]["web_url"] in (await recorded(runs_db, outcome.task_id)).detail


async def test_an_idle_run_keeps_the_reasons_from_its_report(
    runs_db: str, merge_requests: GitLabMergeRequests
) -> None:
    board, inbox = StatusBoard(), Inbox()
    run_id = await new_run(runs_db, "m-1")
    board.statuses[run_id] = JobStatus.SUCCEEDED
    board.messages[run_id] = '{"outcome": "idle", "reasons": ["researcher: all pending"]}'

    await reconcile(runs_db, board, inbox, merge_requests)

    [outcome] = inbox.received
    assert (await recorded(runs_db, outcome.task_id)).detail == (
        f"Run {run_id} succeeded and proposed no changes: researcher: all pending."
    )
