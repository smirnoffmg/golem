"""Proposals in golem_runs (ADR 0015): the result of a succeeded run that a person decides on.

A merge request is decided in GitLab; the reconciler reads the decision there and tells the task
service, which shows it on the run's tasks. The other kinds are decided in Golem and applied by
the task service; the reconciler retries an apply whose answer was lost, and lands the run's
record once the proposal is decided.
"""

import uuid
from dataclasses import dataclass
from typing import Any

from psycopg import AsyncConnection
from psycopg.types.json import Jsonb

from golem.tasks.ports import ProposalRecord, ProposalView

MR_POLL_SECONDS = 300.0
# An accepted proposal the task service did not finish applying is asked for again this late,
# and at most this often: past the apply's own 15 s timeout, with room to spare (ADR 0015).
RETRY_SECONDS = 60.0
# Keeps a pass short however many merge requests are open; the rest wait for the next one.
MAX_CHECKS_PER_PASS = 50


@dataclass(frozen=True)
class OpenedMergeRequest:
    url: str
    iid: int
    # The record the run worked on, from its branch name.
    target: str


@dataclass(frozen=True)
class PendingMergeRequest:
    proposal_id: str
    agent: str
    iid: int


@dataclass(frozen=True)
class Transition:
    """A decision taken outside Golem: the state it moves a pending proposal to."""

    state: str
    decided_by: str | None
    # ISO 8601 from the upstream; when absent, the time the reconciler saw it.
    decided_at: str | None


async def record_merge_request(
    conn: AsyncConnection, run_id: str, opened: OpenedMergeRequest
) -> None:
    # notified_state starts equal to state: the run's outcome carries the first state to its
    # tasks, so only later changes go through the outbox.
    await conn.execute(
        "INSERT INTO proposals (id, run_id, task_id, agent, owner, kind, state, notified_state,"
        " payload, target, url)"
        " SELECT %s, r.id, r.task_id, r.agent, r.caller, 'merge_request', 'pending', 'pending',"
        " %s, %s, %s FROM runs r WHERE r.id = %s"
        " ON CONFLICT (run_id) DO NOTHING",
        (uuid.uuid4(), Jsonb({"iid": opened.iid}), opened.target, opened.url, run_id),
    )


@dataclass(frozen=True)
class NewProposal:
    """A proposal read back from a run's branch, checked again: what the row records."""

    kind: str
    payload: dict[str, Any]
    digest: str
    # The head commit the payload was read at, where the record lands if it is applied.
    commit: str
    target: str


@dataclass(frozen=True)
class Landing:
    """A decided proposal whose run's branch is still to be merged or deleted."""

    proposal_id: str
    run_id: str
    agent: str
    state: str
    commit: str


async def record_proposal(conn: AsyncConnection, run_id: str, proposal: NewProposal) -> None:
    await conn.execute(
        "INSERT INTO proposals (id, run_id, task_id, agent, owner, kind, state, notified_state,"
        " payload, digest, commit, target)"
        " SELECT %s, r.id, r.task_id, r.agent, r.caller, %s, 'pending', 'pending', %s, %s, %s, %s"
        " FROM runs r WHERE r.id = %s ON CONFLICT (run_id) DO NOTHING",
        (
            uuid.uuid4(),
            proposal.kind,
            Jsonb(proposal.payload),
            proposal.digest,
            proposal.commit,
            proposal.target,
            run_id,
        ),
    )


async def due_retries(conn: AsyncConnection) -> list[str]:
    # checked_at doubles as the time of the last retry: a merge request is never accepted.
    cursor = await conn.execute(
        "SELECT id FROM proposals WHERE state = 'accepted'"
        " AND decided_at <= now() - make_interval(secs => %(after)s)"
        " AND (checked_at IS NULL OR checked_at <= now() - make_interval(secs => %(after)s))"
        " ORDER BY decided_at LIMIT %(limit)s",
        {"after": RETRY_SECONDS, "limit": MAX_CHECKS_PER_PASS},
    )
    return [str(proposal_id) for (proposal_id,) in await cursor.fetchall()]


async def mark_retried(conn: AsyncConnection, proposal_id: str) -> None:
    await conn.execute("UPDATE proposals SET checked_at = now() WHERE id = %s", (proposal_id,))


async def unlanded(conn: AsyncConnection) -> list[Landing]:
    cursor = await conn.execute(
        "SELECT p.id, p.run_id, p.agent, p.state, p.commit FROM proposals p"
        " WHERE p.kind <> 'merge_request' AND p.state IN ('applied', 'stale')"
        " AND p.landed_at IS NULL ORDER BY p.decided_at LIMIT %s",
        (MAX_CHECKS_PER_PASS,),
    )
    return [
        Landing(str(proposal_id), str(run_id), agent, state, commit or "")
        for proposal_id, run_id, agent, state, commit in await cursor.fetchall()
    ]


async def mark_landed(conn: AsyncConnection, proposal_id: str) -> None:
    await conn.execute("UPDATE proposals SET landed_at = now() WHERE id = %s", (proposal_id,))


async def due_merge_requests(
    conn: AsyncConnection, poll_seconds: float
) -> list[PendingMergeRequest]:
    cursor = await conn.execute(
        "SELECT id, agent, (payload->>'iid')::bigint FROM proposals"
        " WHERE kind = 'merge_request' AND state = 'pending'"
        " AND (checked_at IS NULL OR checked_at <= now() - make_interval(secs => %s))"
        " ORDER BY checked_at NULLS FIRST, created_at LIMIT %s",
        (poll_seconds, MAX_CHECKS_PER_PASS),
    )
    return [PendingMergeRequest(str(i), agent, iid) for i, agent, iid in await cursor.fetchall()]


async def record_check(
    conn: AsyncConnection, proposal_id: str, transition: Transition | None
) -> None:
    if transition is None:
        await conn.execute("UPDATE proposals SET checked_at = now() WHERE id = %s", (proposal_id,))
        return
    # Compare-and-set: whatever moved the row first keeps it.
    await conn.execute(
        "UPDATE proposals SET state = %s, decided_by = %s,"
        " decided_at = coalesce(%s::timestamptz, now()), checked_at = now()"
        " WHERE id = %s AND state = 'pending'",
        (transition.state, transition.decided_by, transition.decided_at, proposal_id),
    )


async def undelivered_states(conn: AsyncConnection) -> list[tuple[str, str]]:
    # A task that has not heard its run's outcome yet will hear the current state with it.
    cursor = await conn.execute(
        "SELECT p.id, p.state FROM proposals p"
        " WHERE p.notified_state IS DISTINCT FROM p.state AND NOT EXISTS"
        " (SELECT 1 FROM run_tasks t WHERE t.run_id = p.run_id AND t.notified_at IS NULL)"
    )
    return [(str(proposal_id), state) for proposal_id, state in await cursor.fetchall()]


async def mark_delivered(conn: AsyncConnection, proposal_id: str, state: str) -> None:
    await conn.execute(
        "UPDATE proposals SET notified_state = %s WHERE id = %s", (state, proposal_id)
    )


async def proposal_record(conn: AsyncConnection, proposal_id: str) -> ProposalRecord | None:
    try:
        proposal_uuid = uuid.UUID(proposal_id)
    except ValueError:
        return None
    cursor = await conn.execute(
        "SELECT p.id, p.kind, p.state, p.url, p.owner, p.agent,"
        " array(SELECT t.task_id FROM run_tasks t WHERE t.run_id = p.run_id ORDER BY t.task_id)"
        " FROM proposals p WHERE p.id = %s",
        (proposal_uuid,),
    )
    row = await cursor.fetchone()
    if row is None:
        return None
    found, kind, state, url, owner, agent, task_ids = row
    return ProposalRecord(
        view=ProposalView(str(found), kind, state, url or ""),
        caller=owner,
        agent=agent,
        task_ids=tuple(task_ids),
    )
