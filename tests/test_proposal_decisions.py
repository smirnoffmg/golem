"""Reading and deciding proposals in golem_runs (ADR 0015), and reading reports (ADR 0018): what
the task service answers for a person the edge authenticated, and the reviewers it names."""

import uuid
from decimal import Decimal

from psycopg.types.json import Jsonb
from test_reconcile import LIMITS, connect

from golem.orchestrator.process_runs import _stage_run as stage_run
from golem.orchestrator.proposals import (
    decide_proposal,
    list_proposals,
    list_reports,
    read_proposal,
    read_report,
    record_apply,
)
from golem.orchestrator.runs import RunCreated, StartRequest, start_process, start_run
from golem.proposal_payload import payload_digest
from golem.tasks.ports import (
    ALREADY_DECIDED,
    DECIDED_IN_GITLAB,
    NOT_FOUND,
    REASON_REQUIRED,
    Access,
    ProposalDetail,
)

REPLY = {"request": "SD-12", "public": True, "text": "The export works again."}
ALICE = Access("user:alice", frozenset())
BOB_REVIEWS = Access("user:bob", frozenset({"desk"}))
CAROL = Access("user:carol", frozenset({"other"}))


async def run_of(
    dsn: str, message_id: str, owner: str = "user:alice", agent: str = "desk", root: str = ""
) -> str:
    async with await connect(dsn) as conn:
        outcome = await start_run(
            conn,
            StartRequest(
                caller=owner,
                message_id=message_id,
                task_id=f"task-{message_id}",
                agent=agent,
                estimated_cost=Decimal("1"),
                root_run_id=root or None,
            ),
            LIMITS,
        )
        assert isinstance(outcome, RunCreated)
        await conn.execute(
            "UPDATE runs SET status = 'succeeded', proposal_settled_at = now() WHERE id = %s",
            (outcome.run_id,),
        )
    return outcome.run_id


async def proposal(
    dsn: str,
    message_id: str,
    *,
    kind: str = "desk_reply",
    owner: str = "user:alice",
    agent: str = "desk",
    root: str = "",
    payload: dict | None = None,
) -> str:
    run_id = await run_of(dsn, message_id, owner, agent, root)
    proposal_id = str(uuid.uuid4())
    body = payload if payload is not None else REPLY
    async with await connect(dsn) as conn:
        await conn.execute(
            "INSERT INTO proposals (id, run_id, task_id, agent, owner, kind, state,"
            " notified_state, payload, digest, commit, target, url)"
            " VALUES (%s, %s, %s, %s, %s, %s, 'pending', 'pending', %s, %s, 'sha-1', 'alert-1',"
            " %s)",
            (
                proposal_id,
                run_id,
                f"task-{message_id}",
                agent,
                owner,
                kind,
                Jsonb(body),
                payload_digest(body),
                "https://gitlab.example.test/mr/1" if kind == "merge_request" else None,
            ),
        )
    return proposal_id


async def test_a_person_lists_their_own_proposals_and_those_they_review(runs_db: str) -> None:
    own = await proposal(runs_db, "m-1")
    reviewed = await proposal(runs_db, "m-2", owner="service:jira")
    await proposal(runs_db, "m-3", owner="service:jira", agent="other")

    async with await connect(runs_db) as conn:
        alice = await list_proposals(conn, ALICE)
        bob = await list_proposals(conn, BOB_REVIEWS)

    assert [p.id for p in alice.items] == [own]
    # Newest first.
    assert [p.id for p in bob.items] == [reviewed, own]
    item = bob.items[0]
    assert (item.task_id, item.agent, item.kind, item.state, item.owner) == (
        "task-m-2",
        "desk",
        "desk_reply",
        "pending",
        "service:jira",
    )
    assert item.summary == "Reply to SD-12"


async def test_the_list_filters_by_agent_state_and_page(runs_db: str) -> None:
    ids = [await proposal(runs_db, f"m-{n}") for n in range(3)]
    await proposal(runs_db, "m-x", agent="wiki")
    async with await connect(runs_db) as conn:
        await decide_proposal(conn, ALICE, ids[0], "accept", None)
        await record_apply(conn, ids[0], "failed", "Jira answered 500")

        failed = await list_proposals(conn, ALICE, states=("failed",))
        waiting = await list_proposals(conn, ALICE, agent="desk", states=("pending", "failed"))
        first = await list_proposals(conn, ALICE, agent="desk", page_size=2)
        second = await list_proposals(conn, ALICE, agent="desk", page_size=2, page=first.next)

    assert [p.id for p in failed.items] == [ids[0]]
    assert {p.id for p in waiting.items} == set(ids)
    assert [p.id for p in first.items] == ids[:0:-1]
    assert first.next is not None
    assert [p.id for p in second.items] == [ids[0]]
    assert second.next is None


async def test_a_process_stages_proposals_are_listed_by_the_process(runs_db: str) -> None:
    async with await connect(runs_db) as conn:
        process = await start_process(
            conn,
            StartRequest("user:alice", "p-1", "task-p-1", "corsar-feature", Decimal(0)),
            {"stages": []},
            "input",
        )
    stage = await proposal(runs_db, "m-1", root=process.run_id)
    await proposal(runs_db, "m-2")

    async with await connect(runs_db) as conn:
        listed = await list_proposals(conn, ALICE, process="corsar-feature")

    assert [p.id for p in listed.items] == [stage]


async def test_one_proposal_reads_with_its_payload_and_nobody_elses(runs_db: str) -> None:
    proposal_id = await proposal(runs_db, "m-1")

    async with await connect(runs_db) as conn:
        found = await read_proposal(conn, ALICE, proposal_id)
        hidden = await read_proposal(conn, CAROL, proposal_id)
        unknown = await read_proposal(conn, ALICE, str(uuid.uuid4()))
        malformed = await read_proposal(conn, ALICE, "not-a-uuid")

    assert isinstance(found, ProposalDetail)
    assert found.payload == REPLY
    assert found.digest == payload_digest(REPLY)
    assert found.target == "alert-1"
    assert (hidden, unknown, malformed) == (None, None, None)


async def test_accepting_moves_a_pending_proposal_to_accepted_once(runs_db: str) -> None:
    proposal_id = await proposal(runs_db, "m-1", owner="service:jira")

    async with await connect(runs_db) as conn:
        decided = await decide_proposal(conn, BOB_REVIEWS, proposal_id, "accept", None)
        again = await decide_proposal(conn, BOB_REVIEWS, proposal_id, "reject", "no")

    assert isinstance(decided, ProposalDetail)
    assert (decided.summary.state, decided.summary.decided_by) == ("accepted", "user:bob")
    assert again == ALREADY_DECIDED


async def test_rejecting_records_the_reason(runs_db: str) -> None:
    proposal_id = await proposal(runs_db, "m-1")

    async with await connect(runs_db) as conn:
        decided = await decide_proposal(conn, ALICE, proposal_id, "reject", "Wrong customer.")

    assert isinstance(decided, ProposalDetail)
    assert (decided.summary.state, decided.reason) == ("rejected", "Wrong customer.")


async def test_a_process_stages_proposal_is_rejected_only_with_a_reason(runs_db: str) -> None:
    async with await connect(runs_db) as conn:
        process = await start_process(
            conn,
            StartRequest("user:alice", "p-1", "task-p-1", "corsar-feature", Decimal(0)),
            {"stages": []},
            "input",
        )
    stage = await proposal(runs_db, "m-1", root=process.run_id)

    async with await connect(runs_db) as conn:
        refused = await decide_proposal(conn, ALICE, stage, "reject", None)
        decided = await decide_proposal(conn, ALICE, stage, "reject", "Too vague.")

    assert refused == REASON_REQUIRED
    assert isinstance(decided, ProposalDetail)


async def test_a_merge_request_is_decided_in_gitlab(runs_db: str) -> None:
    proposal_id = await proposal(runs_db, "m-1", kind="merge_request", payload={"iid": 1})

    async with await connect(runs_db) as conn:
        assert await decide_proposal(conn, ALICE, proposal_id, "accept", None) == (
            DECIDED_IN_GITLAB
        )


async def test_nobody_else_and_no_service_decides(runs_db: str) -> None:
    proposal_id = await proposal(runs_db, "m-1", owner="service:jira")

    async with await connect(runs_db) as conn:
        by_stranger = await decide_proposal(conn, CAROL, proposal_id, "accept", None)
        by_service = await decide_proposal(
            conn, Access("service:jira", frozenset()), proposal_id, "accept", None
        )

    assert (by_stranger, by_service) == (NOT_FOUND, NOT_FOUND)


async def test_a_failed_proposal_may_be_accepted_again(runs_db: str) -> None:
    proposal_id = await proposal(runs_db, "m-1")
    async with await connect(runs_db) as conn:
        await decide_proposal(conn, ALICE, proposal_id, "accept", None)
        assert await record_apply(conn, proposal_id, "failed", "Jira answered 500")

        again = await decide_proposal(conn, ALICE, proposal_id, "accept", None)

    assert isinstance(again, ProposalDetail)
    assert (again.summary.state, again.detail) == ("accepted", None)


async def test_an_apply_result_is_recorded_only_on_an_accepted_proposal(runs_db: str) -> None:
    proposal_id = await proposal(runs_db, "m-1")
    async with await connect(runs_db) as conn:
        assert not await record_apply(conn, proposal_id, "applied", None)
        await decide_proposal(conn, ALICE, proposal_id, "accept", None)
        assert await record_apply(conn, proposal_id, "applied", None)
        assert not await record_apply(conn, proposal_id, "failed", "late")

        found = await read_proposal(conn, ALICE, proposal_id)

    assert isinstance(found, ProposalDetail)
    assert found.summary.state == "applied"


async def test_a_stale_preview_moves_a_pending_proposal_to_stale(runs_db: str) -> None:
    page = {"page_id": "123", "title": "Home", "version": 7, "body": "<p>New</p>"}
    proposal_id = await proposal(runs_db, "m-1", kind="wiki_edit", payload=page)
    async with await connect(runs_db) as conn:
        assert await record_apply(conn, proposal_id, "stale", None, from_states=("pending",))

        found = await read_proposal(conn, ALICE, proposal_id)

    assert isinstance(found, ProposalDetail)
    assert found.summary.state == "stale"


async def reported(dsn: str, message_id: str, owner: str = "service:alertmanager") -> str:
    run_id = await run_of(dsn, message_id, owner, agent="investigator")
    async with await connect(dsn) as conn:
        await conn.execute(
            "UPDATE runs SET outcome = 'reported', record = 'reports/alert-1.md',"
            " report = %s WHERE id = %s",
            ("Disk grows 4% a day.\n\nNothing to act on yet.", run_id),
        )
    return f"task-{message_id}"


async def test_reviewers_read_the_reports_of_the_agents_they_review(runs_db: str) -> None:
    task_id = await reported(runs_db, "m-1")
    reviewer = Access("user:bob", frozenset({"investigator"}))

    async with await connect(runs_db) as conn:
        page = await list_reports(conn, reviewer, agent="investigator")
        one = await read_report(conn, reviewer, task_id)
        hidden = await list_reports(conn, ALICE)
        not_theirs = await read_report(conn, ALICE, task_id)

    [item] = page.items
    assert (item.task_id, item.agent, item.target, item.summary) == (
        task_id,
        "investigator",
        "alert-1",
        "Disk grows 4% a day.",
    )
    assert one is not None
    assert one.text == "Disk grows 4% a day.\n\nNothing to act on yet."
    assert (hidden.items, not_theirs) == ((), None)


async def test_a_stages_rejection_reason_is_what_its_process_reruns_with(runs_db: str) -> None:
    async with await connect(runs_db) as conn:
        process = await start_process(
            conn,
            StartRequest("user:alice", "p-1", "task-p-1", "corsar-feature", Decimal(0)),
            {"stages": []},
            "input",
        )
    stage = await proposal(runs_db, "m-1", root=process.run_id)
    async with await connect(runs_db) as conn:
        await decide_proposal(conn, ALICE, stage, "reject", "Too vague.")
        cursor = await conn.execute("SELECT run_id FROM proposals WHERE id = %s", (stage,))
        row = await cursor.fetchone()
        assert row is not None
        seen = await stage_run(conn, str(row[0]))

    assert seen is not None
    assert (seen.proposal_state, seen.proposal_detail) == ("rejected", "Too vague.")
