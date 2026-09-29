"""Proposals of the kinds the platform applies (ADR 0015), in the orchestrator: read back from the
run's branch at its head commit, recorded without a merge request, retried while accepted, and
the record landed once the proposal is decided."""

import json
from collections.abc import AsyncIterator
from decimal import Decimal

import httpx
import pytest
from test_merge_requests import PROJECT, TOKEN, FakeGitLab
from test_proposals import ProposalInbox
from test_reconcile import LIMITS, Inbox, StatusBoard, age, connect, recorded

from golem.orchestrator.merge_requests import (
    GitLabMergeRequests,
    GitLabProject,
    land_record,
    propose_result,
)
from golem.orchestrator.reconcile import (
    LAUNCH_GRACE_SECONDS,
    Landing,
    Settlement,
    SucceededRun,
    reconcile_once,
)
from golem.orchestrator.runs import RunCreated, StartRequest, start_run
from golem.proposal_payload import payload_digest

REPLY = {
    "kind": "desk_reply",
    "request": "SD-12",
    "public": True,
    "text_file": "hypotheses/replies/sd-12.txt",
}
REPLY_TEXT = "The export works again."
PAYLOAD = {"request": "SD-12", "public": True, "text": REPLY_TEXT}


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


def pushed(gitlab: FakeGitLab, run_id: str, manifest: object = REPLY) -> str:
    """A run's branch as the runtime pushes it: golem-proposal.json and the body, at its head."""
    branch = f"golem/alert-1/{run_id}"
    gitlab.branches.append(branch)
    head = f"sha-{branch}"
    gitlab.files[(head, "golem-proposal.json")] = json.dumps(manifest)
    gitlab.files[(head, "hypotheses/replies/sd-12.txt")] = REPLY_TEXT
    return branch


async def succeeded_run(dsn: str, kind: str = "desk_reply", message_id: str = "m-1") -> str:
    async with await connect(dsn) as conn:
        outcome = await start_run(
            conn,
            StartRequest(
                caller="user:alice",
                message_id=message_id,
                task_id=f"task-{message_id}",
                agent="discovery",
                estimated_cost=Decimal("1"),
                proposal_kind=kind,
            ),
            LIMITS,
        )
        assert isinstance(outcome, RunCreated)
        await conn.execute("UPDATE runs SET status = 'succeeded' WHERE id = %s", (outcome.run_id,))
    await age(dsn, outcome.run_id, LAUNCH_GRACE_SECONDS + 1)
    return outcome.run_id


async def reconcile(
    dsn: str,
    gitlab: GitLabMergeRequests,
    *,
    proposals: ProposalInbox | None = None,
    inbox: Inbox | None = None,
) -> None:
    async def propose(run: SucceededRun) -> Settlement:
        return await propose_result(gitlab, run)

    async def land(landing: Landing) -> None:
        await land_record(gitlab, landing)

    async with await connect(dsn) as conn:
        await reconcile_once(
            conn,
            StatusBoard(),
            (inbox or Inbox()).notify,
            propose,
            notify_proposal=(proposals or ProposalInbox()).notify,
            land=land,
        )


async def row_of(dsn: str, run_id: str) -> dict:
    async with await connect(dsn) as conn:
        cursor = await conn.execute(
            "SELECT id, kind, state, payload, digest, commit, target, url, landed_at"
            " FROM proposals WHERE run_id = %s",
            (run_id,),
        )
        row = await cursor.fetchone()
        assert row is not None
        names = [column.name for column in cursor.description or ()]
    return dict(zip(names, row, strict=True))


async def test_a_run_records_the_kind_its_agent_proposes(runs_db: str) -> None:
    run_id = await succeeded_run(runs_db, kind="wiki_edit")

    async with await connect(runs_db) as conn:
        cursor = await conn.execute("SELECT proposal_kind FROM runs WHERE id = %s", (run_id,))
        assert await cursor.fetchone() == ("wiki_edit",)


async def test_a_proposal_is_read_at_the_head_commit_and_needs_no_merge_request(
    gitlab: FakeGitLab, merge_requests: GitLabMergeRequests
) -> None:
    branch = pushed(gitlab, "run-1")

    settlement = await propose_result(
        merge_requests, SucceededRun("run-1", "discovery", proposal_kind="desk_reply")
    )

    assert settlement.merge_request is None
    proposal = settlement.proposal
    assert proposal is not None
    assert (proposal.kind, proposal.payload, proposal.commit, proposal.target) == (
        "desk_reply",
        PAYLOAD,
        f"sha-{branch}",
        "alert-1",
    )
    assert proposal.digest == payload_digest(PAYLOAD)
    reads = [r for r in gitlab.requests if r.url.path.endswith("/raw")]
    assert {r.url.params["ref"] for r in reads} == {f"sha-{branch}"}
    assert gitlab.merge_requests == []


async def test_a_branch_without_golem_proposal_json_proposes_nothing(
    gitlab: FakeGitLab, merge_requests: GitLabMergeRequests
) -> None:
    gitlab.branches.append("golem/alert-1/run-1")

    settlement = await propose_result(
        merge_requests, SucceededRun("run-1", "discovery", proposal_kind="desk_reply")
    )

    assert settlement.proposal is None
    assert "golem-proposal.json" in settlement.detail


@pytest.mark.parametrize(
    "manifest",
    [
        REPLY | {"kind": "wiki_edit"},
        REPLY | {"text_file": "hypotheses/replies/missing.txt"},
        REPLY | {"public": "yes"},
        "not an object",
    ],
)
async def test_a_proposal_the_job_got_wrong_is_checked_again_and_refused(
    gitlab: FakeGitLab, merge_requests: GitLabMergeRequests, manifest: object
) -> None:
    pushed(gitlab, "run-1", manifest)

    settlement = await propose_result(
        merge_requests, SucceededRun("run-1", "discovery", proposal_kind="desk_reply")
    )

    assert settlement.proposal is None
    assert "invalid" in settlement.detail


async def test_malformed_json_on_the_branch_is_refused(
    gitlab: FakeGitLab, merge_requests: GitLabMergeRequests
) -> None:
    branch = pushed(gitlab, "run-1")
    gitlab.files[(f"sha-{branch}", "golem-proposal.json")] = "{not json"

    settlement = await propose_result(
        merge_requests, SucceededRun("run-1", "discovery", proposal_kind="desk_reply")
    )

    assert settlement.proposal is None
    assert "invalid" in settlement.detail


async def test_a_merge_request_agent_still_gets_its_merge_request(
    gitlab: FakeGitLab, merge_requests: GitLabMergeRequests
) -> None:
    gitlab.branches.append("golem/H-7/run-1")

    settlement = await propose_result(merge_requests, SucceededRun("run-1", "discovery"))

    assert settlement.merge_request is not None
    assert settlement.proposal is None


async def test_a_settled_run_has_a_pending_row_and_its_task_hears_of_it(
    runs_db: str, gitlab: FakeGitLab, merge_requests: GitLabMergeRequests
) -> None:
    run_id = await succeeded_run(runs_db)
    branch = pushed(gitlab, run_id)
    inbox = Inbox()

    await reconcile(runs_db, merge_requests, inbox=inbox)

    row = await row_of(runs_db, run_id)
    assert (row["kind"], row["state"], row["target"], row["url"]) == (
        "desk_reply",
        "pending",
        "alert-1",
        None,
    )
    assert row["payload"] == PAYLOAD
    assert row["digest"] == payload_digest(PAYLOAD)
    assert row["commit"] == f"sha-{branch}"
    assert gitlab.merge_requests == []
    assert [outcome.task_id for outcome in inbox.received] == ["task-m-1"]
    run = await recorded(runs_db, "task-m-1")
    assert run.proposal is not None
    assert (run.proposal.kind, run.proposal.state) == ("desk_reply", "pending")
    assert run.proposal_payload == PAYLOAD


async def test_an_invalid_proposal_settles_the_run_without_a_row(
    runs_db: str, gitlab: FakeGitLab, merge_requests: GitLabMergeRequests
) -> None:
    run_id = await succeeded_run(runs_db)
    pushed(gitlab, run_id, REPLY | {"public": "yes"})

    await reconcile(runs_db, merge_requests)

    async with await connect(runs_db) as conn:
        cursor = await conn.execute("SELECT count(*) FROM proposals WHERE run_id = %s", (run_id,))
        assert await cursor.fetchone() == (0,)
    run = await recorded(runs_db, "task-m-1")
    assert run.final
    assert "invalid" in (run.detail or "")


async def set_state(dsn: str, run_id: str, state: str, decided_seconds_ago: int = 0) -> None:
    async with await connect(dsn) as conn:
        await conn.execute(
            "UPDATE proposals SET state = %s, notified_state = %s, decided_by = 'user:bob',"
            " decided_at = now() - make_interval(secs => %s) WHERE run_id = %s",
            (state, state, decided_seconds_ago, run_id),
        )


async def test_an_accepted_proposal_left_by_a_lost_apply_is_retried_after_a_minute(
    runs_db: str, gitlab: FakeGitLab, merge_requests: GitLabMergeRequests
) -> None:
    run_id = await succeeded_run(runs_db)
    pushed(gitlab, run_id)
    await reconcile(runs_db, merge_requests)
    proposal_id = str((await row_of(runs_db, run_id))["id"])
    retries = ProposalInbox()

    await set_state(runs_db, run_id, "accepted", decided_seconds_ago=30)
    await reconcile(runs_db, merge_requests, proposals=retries)
    assert retries.received == []

    await set_state(runs_db, run_id, "accepted", decided_seconds_ago=61)
    await reconcile(runs_db, merge_requests, proposals=retries)
    await reconcile(runs_db, merge_requests, proposals=retries)
    # Once a minute, not once a pass: the task service is asked to apply it again.
    assert retries.received == [proposal_id]


async def test_an_applied_proposal_lands_its_record_at_the_accepted_commit(
    runs_db: str, gitlab: FakeGitLab, merge_requests: GitLabMergeRequests
) -> None:
    run_id = await succeeded_run(runs_db)
    branch = pushed(gitlab, run_id)
    await reconcile(runs_db, merge_requests)

    await set_state(runs_db, run_id, "applied")
    await reconcile(runs_db, merge_requests)

    [mr] = gitlab.merge_requests
    assert (mr["source_branch"], mr["state"]) == (branch, "merged")
    merge = next(r for r in gitlab.requests if r.url.path.endswith("/merge"))
    assert json.loads(merge.content)["sha"] == f"sha-{branch}"
    assert (await row_of(runs_db, run_id))["landed_at"] is not None


async def test_a_branch_that_moved_after_the_decision_is_left_for_a_person(
    runs_db: str, gitlab: FakeGitLab, merge_requests: GitLabMergeRequests
) -> None:
    run_id = await succeeded_run(runs_db)
    branch = pushed(gitlab, run_id)
    await reconcile(runs_db, merge_requests)
    async with await connect(runs_db) as conn:
        await conn.execute("UPDATE proposals SET commit = 'sha-older' WHERE run_id = %s", (run_id,))

    await set_state(runs_db, run_id, "applied")
    await reconcile(runs_db, merge_requests)
    await reconcile(runs_db, merge_requests)

    [mr] = gitlab.merge_requests
    assert (mr["source_branch"], mr["state"]) == (branch, "opened")
    assert (await row_of(runs_db, run_id))["landed_at"] is not None
    assert len([r for r in gitlab.requests if r.url.path.endswith("/merge")]) == 1


async def test_a_stale_proposal_deletes_its_branch(
    runs_db: str, gitlab: FakeGitLab, merge_requests: GitLabMergeRequests
) -> None:
    run_id = await succeeded_run(runs_db)
    branch = pushed(gitlab, run_id)
    await reconcile(runs_db, merge_requests)

    await set_state(runs_db, run_id, "stale")
    await reconcile(runs_db, merge_requests)

    assert branch not in gitlab.branches
    assert gitlab.merge_requests == []
    assert (await row_of(runs_db, run_id))["landed_at"] is not None


async def test_a_rejected_proposal_leaves_its_branch(
    runs_db: str, gitlab: FakeGitLab, merge_requests: GitLabMergeRequests
) -> None:
    run_id = await succeeded_run(runs_db)
    branch = pushed(gitlab, run_id)
    await reconcile(runs_db, merge_requests)

    await set_state(runs_db, run_id, "rejected")
    await reconcile(runs_db, merge_requests)

    assert branch in gitlab.branches
    assert gitlab.merge_requests == []


async def test_landing_waits_for_gitlab(
    runs_db: str, gitlab: FakeGitLab, merge_requests: GitLabMergeRequests
) -> None:
    run_id = await succeeded_run(runs_db)
    pushed(gitlab, run_id)
    await reconcile(runs_db, merge_requests)
    await set_state(runs_db, run_id, "applied")

    gitlab.down = True
    await reconcile(runs_db, merge_requests)
    assert (await row_of(runs_db, run_id))["landed_at"] is None

    gitlab.down = False
    await reconcile(runs_db, merge_requests)
    assert (await row_of(runs_db, run_id))["landed_at"] is not None
