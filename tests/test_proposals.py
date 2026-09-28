"""Proposals in golem_runs (ADR 0015), the merge request kind: recorded when a succeeded run's
merge request opens, then following the merge request's state in GitLab."""

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

import httpx
import pytest
from test_merge_requests import PROJECT, TOKEN, FakeGitLab
from test_reconcile import Inbox, StatusBoard, age, connect, new_run, recorded

from golem.orchestrator.jobs import JobStatus
from golem.orchestrator.merge_requests import (
    GitLabMergeRequests,
    GitLabProject,
    check_merge_request,
    merge_request_transition,
    propose_merge_request,
)
from golem.orchestrator.proposals import (
    MAX_CHECKS_PER_PASS,
    PendingMergeRequest,
    Transition,
    proposal_record,
)
from golem.orchestrator.reconcile import (
    LAUNCH_GRACE_SECONDS,
    Settlement,
    SucceededRun,
    reconcile_once,
)
from golem.tasks.ports import ProposalView

MERGED_AT = "2026-09-28T10:00:00.000Z"


@dataclass
class ProposalInbox:
    received: list[str] = field(default_factory=list)
    accepting: bool = True

    async def notify(self, proposal_id: str) -> bool:
        if self.accepting:
            self.received.append(proposal_id)
        return self.accepting


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


async def reconcile(
    dsn: str,
    gitlab: GitLabMergeRequests,
    *,
    board: StatusBoard | None = None,
    inbox: Inbox | None = None,
    proposals: ProposalInbox | None = None,
    poll_seconds: float = 0,
) -> None:
    async def propose(run: SucceededRun) -> Settlement:
        return await propose_merge_request(gitlab, run)

    async def check(pending: PendingMergeRequest) -> Transition | None:
        return await check_merge_request(gitlab, pending)

    async with await connect(dsn) as conn:
        await reconcile_once(
            conn,
            board or StatusBoard(),
            (inbox or Inbox()).notify,
            propose,
            check=check,
            notify_proposal=(proposals or ProposalInbox()).notify,
            poll_seconds=poll_seconds,
        )


async def succeeded_with_a_branch(dsn: str, gitlab: FakeGitLab, message_id: str = "m-1") -> str:
    run_id = await new_run(dsn, message_id)
    await age(dsn, run_id, LAUNCH_GRACE_SECONDS + 1)
    gitlab.branches.append(f"golem/H-7/{run_id}")
    async with await connect(dsn) as conn:
        await conn.execute("UPDATE runs SET status = 'succeeded' WHERE id = %s", (run_id,))
    return run_id


async def proposal_row(dsn: str, run_id: str) -> dict:
    async with await connect(dsn) as conn:
        cursor = await conn.execute(
            "SELECT id, task_id, agent, owner, kind, state, target, url, payload, decided_by,"
            " decided_at FROM proposals WHERE run_id = %s",
            (run_id,),
        )
        row = await cursor.fetchone()
        assert row is not None
        names = [column.name for column in cursor.description or ()]
    return dict(zip(names, row, strict=True))


async def test_an_opened_merge_request_is_recorded_as_a_pending_proposal(
    runs_db: str, gitlab: FakeGitLab, merge_requests: GitLabMergeRequests
) -> None:
    run_id = await succeeded_with_a_branch(runs_db, gitlab)

    await reconcile(runs_db, merge_requests)

    [mr] = gitlab.merge_requests
    row = await proposal_row(runs_db, run_id)
    assert (row["task_id"], row["agent"], row["owner"]) == ("task-m-1", "discovery", "user:alice")
    assert (row["kind"], row["state"], row["target"]) == ("merge_request", "pending", "H-7")
    assert row["url"] == mr["web_url"]
    assert row["payload"] == {"iid": mr["iid"]}
    run = await recorded(runs_db, "task-m-1")
    assert run.proposal == ProposalView(str(row["id"]), "merge_request", "pending", mr["web_url"])


async def test_an_idle_run_has_no_proposal(
    runs_db: str, merge_requests: GitLabMergeRequests
) -> None:
    run_id = await new_run(runs_db, "m-1")
    board = StatusBoard(statuses={run_id: JobStatus.SUCCEEDED})

    await reconcile(runs_db, merge_requests, board=board)

    run = await recorded(runs_db, "task-m-1")
    assert run.final
    assert run.proposal is None


def merged(mr: dict, by: str = "bob") -> None:
    mr.update(state="merged", merge_user={"username": by}, merged_at=MERGED_AT)


async def test_a_merged_merge_request_applies_its_proposal_and_notifies_once(
    runs_db: str, gitlab: FakeGitLab, merge_requests: GitLabMergeRequests
) -> None:
    run_id = await succeeded_with_a_branch(runs_db, gitlab)
    proposals = ProposalInbox()
    await reconcile(runs_db, merge_requests, proposals=proposals)
    merged(gitlab.merge_requests[0])

    await reconcile(runs_db, merge_requests, proposals=proposals)
    await reconcile(runs_db, merge_requests, proposals=proposals)

    row = await proposal_row(runs_db, run_id)
    assert (row["state"], row["decided_by"]) == ("applied", "gitlab:bob")
    assert row["decided_at"].isoformat() == "2026-09-28T10:00:00+00:00"
    assert proposals.received == [str(row["id"])]


async def test_a_closed_merge_request_rejects_its_proposal(
    runs_db: str, gitlab: FakeGitLab, merge_requests: GitLabMergeRequests
) -> None:
    run_id = await succeeded_with_a_branch(runs_db, gitlab)
    await reconcile(runs_db, merge_requests)
    gitlab.merge_requests[0].update(
        state="closed", closed_by={"username": "carol"}, closed_at=MERGED_AT
    )

    await reconcile(runs_db, merge_requests)

    row = await proposal_row(runs_db, run_id)
    assert (row["state"], row["decided_by"]) == ("rejected", "gitlab:carol")


async def gitlab_reads_of_merge_requests(gitlab: FakeGitLab) -> int:
    return sum(
        1
        for request in gitlab.requests
        if request.method == "GET" and request.url.path.rsplit("/", 1)[-1].isdigit()
    )


async def test_an_open_merge_request_is_checked_at_most_once_per_poll_interval(
    runs_db: str, gitlab: FakeGitLab, merge_requests: GitLabMergeRequests
) -> None:
    run_id = await succeeded_with_a_branch(runs_db, gitlab)

    for _ in range(3):
        await reconcile(runs_db, merge_requests, poll_seconds=300)

    assert await gitlab_reads_of_merge_requests(gitlab) == 1
    assert (await proposal_row(runs_db, run_id))["state"] == "pending"


async def pending_proposals(dsn: str, count: int) -> list[str]:
    """Rows as a busy reconciler leaves them: many open merge requests, checked long ago."""
    ids = []
    async with await connect(dsn) as conn:
        for n in range(count):
            run_id, proposal_id = uuid.uuid4(), uuid.uuid4()
            await conn.execute(
                "INSERT INTO runs (id, root_run_id, caller, message_id, task_id, agent,"
                " estimated_cost, status, proposal_settled_at)"
                " VALUES (%s, %s, 'user:alice', %s, %s, 'discovery', 1, 'succeeded', now())",
                (run_id, run_id, f"bulk-{n}", f"task-bulk-{n}"),
            )
            await conn.execute(
                "INSERT INTO proposals (id, run_id, task_id, agent, owner, kind, state,"
                " notified_state, payload, url, checked_at)"
                " VALUES (%s, %s, %s, 'discovery', 'user:alice', 'merge_request', 'pending',"
                " 'pending', %s, 'u', now() - make_interval(secs => %s))",
                (proposal_id, run_id, f"task-bulk-{n}", f'{{"iid": {n + 1}}}', 1000 + n),
            )
            ids.append(str(proposal_id))
    return ids


async def test_a_pass_checks_at_most_fifty_merge_requests_the_longest_unchecked_first(
    runs_db: str, gitlab: FakeGitLab, merge_requests: GitLabMergeRequests
) -> None:
    await pending_proposals(runs_db, MAX_CHECKS_PER_PASS + 1)
    gitlab.merge_requests = [
        {"iid": n + 1, "state": "opened"} for n in range(MAX_CHECKS_PER_PASS + 1)
    ]

    await reconcile(runs_db, merge_requests)

    checked = [
        int(r.url.path.rsplit("/", 1)[-1])
        for r in gitlab.requests
        if r.url.path.rsplit("/", 1)[-1].isdigit()
    ]
    # The newest check (iid 1, checked 1000 s ago) waits for the next pass.
    assert sorted(checked) == list(range(2, MAX_CHECKS_PER_PASS + 2))


async def test_one_unreadable_merge_request_does_not_hold_back_the_others(
    runs_db: str, gitlab: FakeGitLab, merge_requests: GitLabMergeRequests
) -> None:
    first, second = await pending_proposals(runs_db, 2)
    gitlab.merge_requests = [{"iid": 1, "state": "opened"}, {"iid": 2, "state": "opened"}]
    merged(gitlab.merge_requests[0])
    merged(gitlab.merge_requests[1])
    gitlab.broken = {2}

    await reconcile(runs_db, merge_requests)

    async with await connect(runs_db) as conn:
        states = dict(
            await (await conn.execute("SELECT id::text, state FROM proposals")).fetchall()
        )
    assert (states[first], states[second]) == ("applied", "pending")


async def test_a_proposals_state_waits_until_its_tasks_heard_the_outcome(
    runs_db: str, gitlab: FakeGitLab, merge_requests: GitLabMergeRequests
) -> None:
    run_id = await succeeded_with_a_branch(runs_db, gitlab)
    await reconcile(runs_db, merge_requests, inbox=Inbox(accepting=False))
    merged(gitlab.merge_requests[0])
    proposals = ProposalInbox()

    await reconcile(runs_db, merge_requests, inbox=Inbox(accepting=False), proposals=proposals)
    assert proposals.received == []

    await reconcile(runs_db, merge_requests, proposals=proposals)
    assert proposals.received == [str((await proposal_row(runs_db, run_id))["id"])]


async def test_the_task_service_reads_a_proposal_with_every_task_of_its_run(
    runs_db: str, gitlab: FakeGitLab, merge_requests: GitLabMergeRequests
) -> None:
    run_id = await succeeded_with_a_branch(runs_db, gitlab)
    async with await connect(runs_db) as conn:
        await conn.execute(
            "INSERT INTO run_tasks (task_id, run_id) VALUES ('task-retry', %s)", (run_id,)
        )
    await reconcile(runs_db, merge_requests)
    proposal_id = str((await proposal_row(runs_db, run_id))["id"])

    async with await connect(runs_db) as conn:
        record = await proposal_record(conn, proposal_id)
        unknown = await proposal_record(conn, str(uuid.uuid4()))
        malformed = await proposal_record(conn, "not-a-uuid")

    assert record is not None
    assert (record.caller, record.agent) == ("user:alice", "discovery")
    assert sorted(record.task_ids) == ["task-m-1", "task-retry"]
    assert record.view.state == "pending"
    assert unknown is None
    assert malformed is None


@pytest.mark.parametrize(
    ("merge_request", "transition"),
    [
        ({"state": "opened"}, None),
        # Short-lived and transitional in GitLab's words: still waiting for its outcome.
        ({"state": "locked"}, None),
        (
            {"state": "merged", "merge_user": {"username": "bob"}, "merged_at": MERGED_AT},
            Transition("applied", "gitlab:bob", MERGED_AT),
        ),
        (
            {"state": "closed", "closed_by": {"username": "carol"}, "closed_at": MERGED_AT},
            Transition("rejected", "gitlab:carol", MERGED_AT),
        ),
        # Merged by a user GitLab no longer names: the decision stands, its author is unknown.
        ({"state": "merged", "merge_user": None}, Transition("applied", None, None)),
        ({"state": "something-new"}, None),
    ],
)
def test_a_merge_requests_state_maps_to_its_proposals(
    merge_request: dict, transition: Transition | None
) -> None:
    assert merge_request_transition(merge_request) == transition
