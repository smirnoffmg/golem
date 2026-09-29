"""Merge requests for succeeded runs, against a GitLab REST API stand-in and real Postgres.

The stand-in implements only the documented shapes Golem uses:
https://docs.gitlab.com/api/branches/#list-repository-branches
https://docs.gitlab.com/api/merge_requests/#list-project-merge-requests
https://docs.gitlab.com/api/merge_requests/#create-a-merge-request
https://docs.gitlab.com/api/merge_requests/#get-single-mr
https://docs.gitlab.com/api/branches/#get-single-repository-branch
https://docs.gitlab.com/api/branches/#delete-repository-branch
https://docs.gitlab.com/api/repository_files/#get-raw-file-from-repository
https://docs.gitlab.com/api/notes/#list-all-merge-request-notes
https://docs.gitlab.com/api/merge_requests/#update-mr
"""

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import unquote

import httpx
import pytest
from test_reconcile import Inbox, StatusBoard, age, connect, new_run, recorded, run_status

from golem.orchestrator.jobs import JobStatus
from golem.orchestrator.merge_requests import (
    GitLabError,
    GitLabMergeRequests,
    GitLabProject,
    close_merge_request,
    closing_reason,
    discard_branch,
    propose_merge_request,
    pushed_branch,
    read_report,
    run_branch,
)
from golem.orchestrator.reconcile import (
    LAUNCH_GRACE_SECONDS,
    Pushed,
    Reporter,
    Settlement,
    SucceededRun,
    reconcile_once,
    report_record,
    run_outcome,
)

PROJECT = "product/discovery-context"
ENCODED_PROJECT = "product%2Fdiscovery-context"
TOKEN = "glpat-test"


@dataclass
class FakeGitLab:
    branches: list[str] = field(default_factory=list)
    merge_requests: list[dict[str, Any]] = field(default_factory=list)
    # (commit sha, path) -> text; a branch's head commit is `sha-<branch>`.
    files: dict[tuple[str, str], str] = field(default_factory=dict)
    requests: list[httpx.Request] = field(default_factory=list)
    down: bool = False
    # Merge requests GitLab fails to read (a 500), while it answers for the others.
    broken: set[int] = field(default_factory=set)
    # iid -> the merge request's notes, oldest first.
    notes: dict[int, list[dict[str, Any]]] = field(default_factory=dict)
    # branch -> its head commit's message.
    messages: dict[str, str] = field(default_factory=dict)

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
        if path.startswith(f"{prefix}/merge_requests/") and path.endswith("/notes"):
            return self._notes(int(path.split("/")[-2]), request.url.params)
        if path.startswith(f"{prefix}/merge_requests/") and request.method == "GET":
            return self._single(int(path.rsplit("/", 1)[1]))
        if path.startswith(f"{prefix}/merge_requests/") and request.method == "PUT":
            return self._update(int(path.rsplit("/", 1)[1]), json.loads(request.content))
        if path.startswith(f"{prefix}/repository/branches/"):
            return self._branch(request.method, unquote(path.rsplit("/", 1)[1]))
        if path.startswith(f"{prefix}/repository/files/") and path.endswith("/raw"):
            name = unquote(path.removeprefix(f"{prefix}/repository/files/").removesuffix("/raw"))
            text = self.files.get((request.url.params.get("ref", ""), name))
            if text is None:
                return httpx.Response(404, json={"message": "404 File Not Found"})
            return httpx.Response(200, text=text)
        return httpx.Response(404, json={"message": "404 Not Found"})

    def _branch(self, method: str, name: str) -> httpx.Response:
        if name not in self.branches:
            return httpx.Response(404, json={"message": "404 Branch Not Found"})
        if method == "DELETE":
            self.branches.remove(name)
            return httpx.Response(204)
        commit = {"id": f"sha-{name}", "message": self.messages.get(name, "golem: a change\n")}
        return httpx.Response(200, json={"name": name, "commit": commit})

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

    def _single(self, iid: int) -> httpx.Response:
        if iid in self.broken:
            return httpx.Response(500, json={"message": "500 Internal Server Error"})
        for mr in self.merge_requests:
            if mr["iid"] == iid:
                return httpx.Response(200, json=mr)
        return httpx.Response(404, json={"message": "404 Not found"})

    def _notes(self, iid: int, params: httpx.QueryParams) -> httpx.Response:
        notes = sorted(
            self.notes.get(iid, []),
            key=lambda note: note[params.get("order_by", "created_at")],
            reverse=params.get("sort", "desc") == "desc",
        )
        return httpx.Response(200, json=notes[: int(params.get("per_page", "20"))])

    def _update(self, iid: int, body: dict[str, Any]) -> httpx.Response:
        for mr in self.merge_requests:
            if mr["iid"] == iid:
                if body.get("state_event") == "close" and mr["state"] == "opened":
                    mr.update(state="closed", closed_by={"username": "golem"})
                return httpx.Response(200, json=mr)
        return httpx.Response(404, json={"message": "404 Not found"})

    def _create(self, body: dict[str, Any]) -> httpx.Response:
        missing = [k for k in ("source_branch", "target_branch", "title") if not body.get(k)]
        if missing:
            return httpx.Response(400, json={"error": f"{missing[0]} is missing"})
        iid = len(self.merge_requests) + 1
        mr = {
            "iid": iid,
            "state": "opened",
            "merge_user": None,
            "closed_by": None,
            "merged_at": None,
            "closed_at": None,
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
    opened = await merge_requests.open(
        "discovery", "run-1", "golem/H-7/run-1", "Proposal for H-7", "From run run-1."
    )

    [mr] = gitlab.merge_requests
    assert (opened.url, opened.iid, opened.target) == (mr["web_url"], mr["iid"], "H-7")
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

    opened = await merge_requests.open("discovery", "run-1", "golem/H-7/run-1", "t", "d")

    assert opened.url != "old"


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
    settled = await propose_merge_request(merge_requests, SucceededRun("run-1", "reviewer"))

    assert "no GitLab project is configured for agent 'reviewer'" in settled.detail
    assert "not proposed" in settled.detail
    assert settled.merge_request is None


async def test_a_run_without_a_branch_proposed_nothing(
    merge_requests: GitLabMergeRequests,
) -> None:
    settled = await propose_merge_request(merge_requests, SucceededRun("run-1", "discovery"))

    assert settled.detail == "Run run-1 succeeded and proposed no changes."
    assert settled.merge_request is None


async def test_a_run_with_a_branch_proposes_a_merge_request(
    gitlab: FakeGitLab, merge_requests: GitLabMergeRequests
) -> None:
    gitlab.branches = ["golem/H-7/run-1"]

    settled = await propose_merge_request(merge_requests, SucceededRun("run-1", "discovery"))

    [mr] = gitlab.merge_requests
    assert mr["web_url"] in settled.detail
    assert settled.merge_request is not None
    assert settled.merge_request.url == mr["web_url"]
    assert "H-7" in mr["title"]
    assert "run-1" in mr["description"]


# The outbox: a succeeded run's tasks are notified only after its merge request is settled.


async def reconcile(dsn: str, board: StatusBoard, inbox: Inbox, gitlab: GitLabMergeRequests):
    async def propose(run: SucceededRun) -> Settlement:
        return await propose_merge_request(gitlab, run)

    async def proposed(run: SucceededRun) -> Pushed | None:
        return await pushed_branch(gitlab, run)

    async with await connect(dsn) as conn:
        await reconcile_once(conn, board, inbox.notify, propose, proposed=proposed)


# A Job is deleted by its TTL after it finishes; a reconciler that was down longer than that
# finds it gone. The branch the run pushed is the durable proof that it succeeded.


async def test_a_job_gone_after_pushing_its_branch_is_a_succeeded_run(
    runs_db: str, gitlab: FakeGitLab, merge_requests: GitLabMergeRequests
) -> None:
    board, inbox = StatusBoard(), Inbox()
    run_id = await new_run(runs_db, "m-1")
    await age(runs_db, run_id, LAUNCH_GRACE_SECONDS + 1)
    gitlab.branches = [f"golem/H-7/{run_id}"]
    board.statuses[run_id] = JobStatus.MISSING

    await reconcile(runs_db, board, inbox, merge_requests)

    [mr] = gitlab.merge_requests
    [outcome] = inbox.received
    run = await recorded(runs_db, outcome.task_id)
    assert run.status == "succeeded"
    assert mr["web_url"] in run.detail


async def test_a_job_gone_without_a_branch_is_a_failed_run(
    runs_db: str, gitlab: FakeGitLab, merge_requests: GitLabMergeRequests
) -> None:
    board, inbox = StatusBoard(), Inbox()
    run_id = await new_run(runs_db, "m-1")
    await age(runs_db, run_id, LAUNCH_GRACE_SECONDS + 1)
    board.statuses[run_id] = JobStatus.MISSING

    await reconcile(runs_db, board, inbox, merge_requests)

    assert await run_status(runs_db, run_id) == "failed"
    assert gitlab.merge_requests == []


async def test_a_job_gone_while_gitlab_is_down_stays_running_until_gitlab_answers(
    runs_db: str, gitlab: FakeGitLab, merge_requests: GitLabMergeRequests
) -> None:
    board, inbox = StatusBoard(), Inbox()
    run_id = await new_run(runs_db, "m-1")
    await age(runs_db, run_id, LAUNCH_GRACE_SECONDS + 1)
    gitlab.branches = [f"golem/H-7/{run_id}"]
    board.statuses[run_id] = JobStatus.MISSING
    gitlab.down = True

    await reconcile(runs_db, board, inbox, merge_requests)
    assert await run_status(runs_db, run_id) == "running"

    gitlab.down = False
    await reconcile(runs_db, board, inbox, merge_requests)
    assert await run_status(runs_db, run_id) == "succeeded"


async def test_an_agent_without_a_project_has_no_proposal(
    merge_requests: GitLabMergeRequests,
) -> None:
    assert await pushed_branch(merge_requests, SucceededRun("run-1", "unconfigured")) is None


# A goal run's report is lost with its Job; its branch's head commit says what it was.


def goal_commit(outcome: str, record: str | None = None) -> str:
    trailers = f"Run: r\nRole: investigator\nTarget: t-1\nOutcome: {outcome}\n"
    return f"golem: investigator on t-1\n\nFound it.\n\n{trailers}" + (
        f"Record: {record}\n" if record else ""
    )


async def test_a_vanished_goal_run_that_reported_is_reported_not_proposed(
    runs_db: str, gitlab: FakeGitLab, merge_requests: GitLabMergeRequests
) -> None:
    board, inbox = StatusBoard(), Inbox()
    run_id = await new_run(runs_db, "m-1")
    await age(runs_db, run_id, LAUNCH_GRACE_SECONDS + 1)
    branch = f"golem/t-1/{run_id}"
    gitlab.branches = [branch]
    gitlab.messages[branch] = goal_commit("reported", "findings/t-1.md")
    board.statuses[run_id] = JobStatus.MISSING

    await reconcile(runs_db, board, inbox, merge_requests)

    assert gitlab.merge_requests == []
    async with await connect(runs_db) as conn:
        row = await (
            await conn.execute("SELECT status, outcome, record FROM runs WHERE id = %s", (run_id,))
        ).fetchone()
    assert row == ("succeeded", "reported", "findings/t-1.md")


async def test_a_vanished_goal_run_that_proposed_gets_its_merge_request(
    runs_db: str, gitlab: FakeGitLab, merge_requests: GitLabMergeRequests
) -> None:
    board, inbox = StatusBoard(), Inbox()
    run_id = await new_run(runs_db, "m-1")
    await age(runs_db, run_id, LAUNCH_GRACE_SECONDS + 1)
    branch = f"golem/t-1/{run_id}"
    gitlab.branches = [branch]
    gitlab.messages[branch] = goal_commit("proposed")
    board.statuses[run_id] = JobStatus.MISSING

    await reconcile(runs_db, board, inbox, merge_requests)

    [mr] = gitlab.merge_requests
    assert mr["source_branch"] == branch


async def test_a_vanished_run_claiming_a_record_outside_the_repository_is_not_trusted(
    merge_requests: GitLabMergeRequests, gitlab: FakeGitLab
) -> None:
    gitlab.branches = ["golem/t-1/run-1"]
    gitlab.messages["golem/t-1/run-1"] = goal_commit("reported", "../../etc/passwd.md")

    pushed = await pushed_branch(merge_requests, SucceededRun("run-1", "discovery"))

    # Still a report, as when its Job is there to say so: only the record is not trusted.
    assert pushed is not None
    assert run_outcome(JobStatus.SUCCEEDED, pushed.report) == "reported"
    assert report_record(pushed.report) is None


async def test_a_vanished_run_that_reported_a_bad_record_opens_no_merge_request(
    runs_db: str, gitlab: FakeGitLab, merge_requests: GitLabMergeRequests
) -> None:
    board, inbox = StatusBoard(), Inbox()
    run_id = await new_run(runs_db, "m-1")
    await age(runs_db, run_id, LAUNCH_GRACE_SECONDS + 1)
    branch = f"golem/t-1/{run_id}"
    gitlab.branches = [branch]
    gitlab.messages[branch] = goal_commit("reported", "../../etc/passwd.md")
    board.statuses[run_id] = JobStatus.MISSING

    await reconcile(runs_db, board, inbox, merge_requests)

    assert gitlab.merge_requests == []
    async with await connect(runs_db) as conn:
        row = await (
            await conn.execute("SELECT outcome, record FROM runs WHERE id = %s", (run_id,))
        ).fetchone()
    assert row == ("reported", None)


# A merge request closed without a word: the reason is the closer's own comment (ADR 0019).


def closed(iid: int = 1, closer: str = "bob", at: str = "2026-09-28T10:00:00Z") -> dict[str, Any]:
    return {
        "iid": iid,
        "state": "closed",
        "closed_by": {"username": closer},
        "closed_at": at,
        "source_branch": "golem/t/r",
        "web_url": f"https://gitlab.example.test/{PROJECT}/-/merge_requests/{iid}",
    }


def note(author: str, at: str, body: str, system: bool = False) -> dict[str, Any]:
    return {"author": {"username": author}, "created_at": at, "body": body, "system": system}


async def test_the_reason_is_the_closers_newest_comment_by_the_time_of_closing(
    gitlab: FakeGitLab, merge_requests: GitLabMergeRequests
) -> None:
    gitlab.merge_requests = [closed()]
    gitlab.notes[1] = [
        note("bob", "2026-09-28T09:50:00Z", "First thought."),
        note("carol", "2026-09-28T09:59:00Z", "Not the closer."),
        note("bob", "2026-09-28T09:59:30Z", "The design ignores the topic contract."),
        note("bob", "2026-09-28T10:00:00Z", "closed", system=True),
        note("bob", "2026-09-28T10:05:00Z", "Much later."),
    ]

    reason = await closing_reason(merge_requests, "discovery", 1)

    assert reason == "The design ignores the topic contract."
    [asked] = [r for r in gitlab.requests if r.url.path.endswith("/notes")]
    assert (asked.url.params["sort"], asked.url.params["order_by"]) == ("desc", "created_at")


async def test_a_comment_within_a_minute_after_closing_counts(
    gitlab: FakeGitLab, merge_requests: GitLabMergeRequests
) -> None:
    gitlab.merge_requests = [closed()]
    gitlab.notes[1] = [note("bob", "2026-09-28T10:00:45Z", "Wrong approach.")]

    assert await closing_reason(merge_requests, "discovery", 1) == "Wrong approach."


async def test_a_comment_long_before_closing_is_not_the_reason(
    gitlab: FakeGitLab, merge_requests: GitLabMergeRequests
) -> None:
    # Written in review days earlier, it says nothing about why the merge request was closed.
    gitlab.merge_requests = [closed()]
    gitlab.notes[1] = [note("bob", "2026-09-25T10:00:00Z", "LGTM once CI passes.")]

    assert await closing_reason(merge_requests, "discovery", 1) is None


async def test_no_comment_by_the_closer_means_no_reason(
    gitlab: FakeGitLab, merge_requests: GitLabMergeRequests
) -> None:
    gitlab.merge_requests = [closed()]
    gitlab.notes[1] = [note("carol", "2026-09-28T09:59:00Z", "Someone else's word.")]

    assert await closing_reason(merge_requests, "discovery", 1) is None


async def test_a_reason_is_cut_at_4000_characters(
    gitlab: FakeGitLab, merge_requests: GitLabMergeRequests
) -> None:
    gitlab.merge_requests = [closed()]
    gitlab.notes[1] = [note("bob", "2026-09-28T09:59:00Z", "x" * 5000)]

    assert await closing_reason(merge_requests, "discovery", 1) == "x" * 4000


async def test_closing_a_merge_request_sets_its_state_event(
    gitlab: FakeGitLab, merge_requests: GitLabMergeRequests
) -> None:
    await merge_requests.open("discovery", "run-1", "golem/H-7/run-1", "t", "d")

    await close_merge_request(merge_requests, "discovery", 1)

    [mr] = gitlab.merge_requests
    assert mr["state"] == "closed"
    [put] = [r for r in gitlab.requests if r.method == "PUT"]
    assert json.loads(put.content) == {"state_event": "close"}


async def test_a_succeeded_run_notifies_its_task_with_the_merge_request_url(
    runs_db: str, gitlab: FakeGitLab, merge_requests: GitLabMergeRequests
) -> None:
    board, inbox = StatusBoard(), Inbox()
    run_id = await new_run(runs_db, "m-1")
    await age(runs_db, run_id, LAUNCH_GRACE_SECONDS + 1)
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
    await age(runs_db, run_id, LAUNCH_GRACE_SECONDS + 1)
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
    await age(runs_db, run_id, LAUNCH_GRACE_SECONDS + 1)
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

    async def propose(run: SucceededRun) -> Settlement:
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
    await age(runs_db, run_id, LAUNCH_GRACE_SECONDS + 1)
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
    await age(runs_db, run_id, LAUNCH_GRACE_SECONDS + 1)
    board.statuses[run_id] = JobStatus.SUCCEEDED
    board.messages[run_id] = '{"outcome": "idle", "reasons": ["researcher: all pending"]}'

    await reconcile(runs_db, board, inbox, merge_requests)

    [outcome] = inbox.received
    assert (await recorded(runs_db, outcome.task_id)).detail == (
        f"Run {run_id} succeeded and proposed no changes: researcher: all pending."
    )


# A goal run that found nothing to propose (ADR 0017): its record is the report, its branch goes

BRANCH = "golem/alert-0a1b2c3d4e5f/run-1"
RECORD = "hypotheses/alert-0a1b2c3d4e5f.md"
REPORTED_RUN = SucceededRun("run-1", "discovery", record=RECORD)


async def test_the_report_is_the_record_at_the_branch_head_commit(
    gitlab: FakeGitLab, merge_requests: GitLabMergeRequests
) -> None:
    gitlab.branches = [BRANCH]
    gitlab.files[(f"sha-{BRANCH}", RECORD)] = "# Seen\n\nA deploy."
    gitlab.files[(BRANCH, RECORD)] = "read by the branch name"

    assert await read_report(merge_requests, REPORTED_RUN) == "# Seen\n\nA deploy."


async def test_no_report_when_the_branch_or_the_record_is_gone(
    gitlab: FakeGitLab, merge_requests: GitLabMergeRequests
) -> None:
    assert await read_report(merge_requests, REPORTED_RUN) is None
    gitlab.branches = [BRANCH]
    assert await read_report(merge_requests, REPORTED_RUN) is None


async def test_discarding_deletes_the_branch_and_tolerates_it_gone(
    gitlab: FakeGitLab, merge_requests: GitLabMergeRequests
) -> None:
    gitlab.branches = [BRANCH, "golem/H-2/run-7"]

    await discard_branch(merge_requests, REPORTED_RUN)
    await discard_branch(merge_requests, REPORTED_RUN)

    assert gitlab.branches == ["golem/H-2/run-7"]


async def test_an_agent_without_a_project_has_no_report_and_no_branch_to_discard(
    merge_requests: GitLabMergeRequests,
) -> None:
    run = SucceededRun("run-1", "other", record=RECORD)

    assert await read_report(merge_requests, run) is None
    await discard_branch(merge_requests, run)


async def test_discarding_fails_loudly_when_gitlab_is_down(
    gitlab: FakeGitLab, merge_requests: GitLabMergeRequests
) -> None:
    gitlab.branches = [BRANCH]
    gitlab.down = True

    with pytest.raises(GitLabError):
        await discard_branch(merge_requests, REPORTED_RUN)


async def test_a_reported_run_opens_no_merge_request_and_leaves_no_branch(
    runs_db: str, gitlab: FakeGitLab, merge_requests: GitLabMergeRequests
) -> None:
    run_id = await new_run(runs_db, "m-1")
    branch = f"golem/alert-0a1b2c3d4e5f/{run_id}"
    gitlab.branches = [branch]
    gitlab.files[(f"sha-{branch}", RECORD)] = "# Seen\n\nA deploy."
    board, inbox = StatusBoard(), Inbox()
    board.statuses[run_id] = JobStatus.SUCCEEDED
    board.messages[run_id] = json.dumps({"outcome": "reported", "record": RECORD})

    async def propose(run: SucceededRun) -> Settlement:
        return await propose_merge_request(merge_requests, run)

    async def read(run: SucceededRun) -> str | None:
        return await read_report(merge_requests, run)

    async def discard(run: SucceededRun) -> None:
        await discard_branch(merge_requests, run)

    async with await connect(runs_db) as conn:
        await reconcile_once(conn, board, inbox.notify, propose, report=Reporter(read, discard))

    assert gitlab.merge_requests == []
    assert gitlab.branches == []
    assert (await recorded(runs_db, "task-m-1")).report == "# Seen\n\nA deploy."
