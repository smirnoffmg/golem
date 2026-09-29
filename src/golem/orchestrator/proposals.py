"""Proposals in golem_runs (ADR 0015): the result of a succeeded run that a person decides on.

A merge request is decided in GitLab; the reconciler reads the decision there and tells the task
service, which shows it on the run's tasks. The other kinds are decided in Golem and applied by
the task service; the reconciler retries an apply whose answer was lost, and lands the run's
record once the proposal is decided.
"""

import base64
import json
import uuid
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

from psycopg import AsyncConnection
from psycopg.types.json import Jsonb

from golem.catalog import REVIEWER
from golem.proposal_payload import MERGE_REQUEST, summary_of
from golem.tasks.ports import (
    ALREADY_DECIDED,
    DECIDED_IN_GITLAB,
    NOT_FOUND,
    REASON_REQUIRED,
    UNNAMEABLE_DECIDER,
    Access,
    ProposalDetail,
    ProposalGate,
    ProposalPage,
    ProposalRecord,
    ProposalSummary,
    ProposalView,
    Report,
    ReportPage,
    ReportSummary,
)

MR_POLL_SECONDS = 300.0
# How long one apply holds an accepted proposal: longer than the task service waits for it (15 s)
# and than a write server spends on it (WRITE_DEADLINE_SECONDS, 30 s), so a retry starts only
# once the apply before it is over (ADR 0015).
APPLY_LEASE_SECONDS = 60.0
# An accepted proposal whose lease ran out is asked for again at most this often.
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
        " AND (apply_lease_until IS NULL OR apply_lease_until <= now())"
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


PAGE_SIZE = 50
REPORTS_PAGE_SIZE = 20
MAX_REPORT_SUMMARY = 280
# A proposal leaves these by a decision; a failed apply can be accepted again or rejected.
DECIDABLE = ("pending", "failed")
PERSON = "user:"

# Who may see a row: its owner, or a reviewer of its agent (ADR 0015).
VISIBLE = "(p.owner = %(principal)s OR p.agent = ANY(%(reviews)s))"
SELECT_PROPOSAL = (
    "SELECT p.id, p.task_id, p.agent, p.kind, p.state, p.payload, p.url, p.owner, p.created_at,"
    " p.decided_by, p.decided_at, p.digest, p.target, p.reason, p.detail,"
    # A goal run that proposed leaves its record too, and that record is its report.
    " r.report,"
    " root.kind = 'process' AND root.id <> r.id"
    " FROM proposals p JOIN runs r ON r.id = p.run_id JOIN runs root ON root.id = r.root_run_id"
)


def _access(access: Access) -> dict[str, Any]:
    return {"principal": access.principal, "reviews": sorted(access.reviews)}


def _summary(row: tuple[Any, ...]) -> ProposalSummary:
    proposal_id, task_id, agent, kind, state, payload, url, owner, created, by, at = row[:11]
    return ProposalSummary(
        id=str(proposal_id),
        task_id=task_id,
        agent=agent,
        kind=kind,
        state=state,
        summary=_summary_text(kind, payload, row[12]),
        url=url,
        owner=owner,
        created_at=created.isoformat(),
        decided_by=by,
        decided_at=None if at is None else at.isoformat(),
    )


def _summary_text(kind: str, payload: Any, target: str | None) -> str:
    if kind == MERGE_REQUEST:
        return f"Merge request for {target}" if target else "Merge request"
    return summary_of(kind, payload)


def _detail(row: tuple[Any, ...]) -> ProposalDetail:
    return ProposalDetail(
        summary=_summary(row),
        payload=row[5],
        digest=row[11],
        target=row[12],
        reason=row[13],
        detail=row[14],
        report=row[15],
        stage=bool(row[16]),
    )


def page_token(created_at: str, item_id: str) -> str:
    return base64.urlsafe_b64encode(json.dumps([created_at, item_id]).encode()).decode()


def _after(page: str | None) -> tuple[str | None, str | None]:
    if not page:
        return None, None
    try:
        created_at, item_id = json.loads(base64.urlsafe_b64decode(page.encode()))
        uuid.UUID(str(item_id))
    except (ValueError, TypeError):
        raise ValueError("malformed page token") from None
    return str(created_at), str(item_id)


async def list_proposals(
    conn: AsyncConnection,
    access: Access,
    *,
    agent: str | None = None,
    states: tuple[str, ...] | None = None,
    process: str | None = None,
    page: str | None = None,
    page_size: int = PAGE_SIZE,
) -> ProposalPage:
    """The proposals ``access`` may see, newest first; ValueError on a malformed page token."""
    after, after_id = _after(page)
    cursor = await conn.execute(
        SELECT_PROPOSAL
        + " WHERE "
        + VISIBLE
        + " AND (%(agent)s::text IS NULL OR p.agent = %(agent)s)"
        " AND (%(states)s::text[] IS NULL OR p.state = ANY(%(states)s))"
        " AND (%(process)s::text IS NULL OR (root.kind = 'process' AND root.agent = %(process)s"
        "  AND root.id <> r.id))"
        " AND (%(after)s::timestamptz IS NULL OR (p.created_at, p.id) < (%(after)s, %(after_id)s))"
        " ORDER BY p.created_at DESC, p.id DESC LIMIT %(limit)s",
        {
            **_access(access),
            "agent": agent,
            "states": list(states) if states else None,
            "process": process,
            "after": after,
            "after_id": after_id,
            "limit": page_size + 1,
        },
    )
    rows = await cursor.fetchall()
    items = tuple(_summary(row) for row in rows[:page_size])
    more = len(rows) > page_size
    return ProposalPage(
        items, page_token(rows[page_size - 1][8].isoformat(), items[-1].id) if more else None
    )


async def read_proposal(
    conn: AsyncConnection, access: Access, proposal_id: str
) -> ProposalDetail | None:
    try:
        proposal_uuid = uuid.UUID(proposal_id)
    except ValueError:
        return None
    cursor = await conn.execute(
        SELECT_PROPOSAL + " WHERE p.id = %(id)s AND " + VISIBLE,
        {**_access(access), "id": proposal_uuid},
    )
    row = await cursor.fetchone()
    if row is None:
        return None
    return _detail(row)


async def claim_apply(conn: AsyncConnection, proposal_id: str) -> ProposalDetail | None:
    """An accepted proposal no apply holds, now held by the caller: what the task service
    applies again when the reconciler says the apply before went unanswered. None while
    another apply holds it, or when it is no longer accepted: one apply at a time."""
    try:
        proposal_uuid = uuid.UUID(proposal_id)
    except ValueError:
        return None
    claimed = await conn.execute(
        "UPDATE proposals SET apply_lease_until = now() + make_interval(secs => %s)"
        " WHERE id = %s AND state = 'accepted'"
        " AND (apply_lease_until IS NULL OR apply_lease_until <= now())",
        (APPLY_LEASE_SECONDS, proposal_uuid),
    )
    if not claimed.rowcount:
        return None
    cursor = await conn.execute(SELECT_PROPOSAL + " WHERE p.id = %s", (proposal_uuid,))
    row = await cursor.fetchone()
    return None if row is None else _detail(row)


async def proposal_gate(conn: AsyncConnection, proposal_id: str) -> ProposalGate | None:
    try:
        proposal_uuid = uuid.UUID(proposal_id)
    except ValueError:
        return None
    cursor = await conn.execute(
        "SELECT id, state, digest, kind FROM proposals WHERE id = %s", (proposal_uuid,)
    )
    row = await cursor.fetchone()
    return None if row is None else ProposalGate(str(row[0]), row[1], row[2], row[3])


async def decide_proposal(
    conn: AsyncConnection, access: Access, proposal_id: str, decision: str, reason: str | None
) -> ProposalDetail | str:
    """A person's decision, one compare-and-set on the row: the proposal as it now is, or why
    it was refused (``not_found``, ``unnameable_decider``, ``decided_in_gitlab``,
    ``reason_required``, ``already_decided``). Accepting moves it to ``accepted`` and holds its
    apply for the caller, who applies it next."""
    found = await read_proposal(conn, access, proposal_id)
    # A service or an agent never decides (ADR 0015); neither may learn the row exists.
    if found is None or not access.principal.startswith(PERSON):
        return NOT_FOUND
    # The write server takes the decider from a proposal token, which names only a principal
    # the catalog's reviewers could: anyone else would leave an accept no apply can carry out.
    if not REVIEWER.fullmatch(access.principal):
        return UNNAMEABLE_DECIDER
    if found.summary.kind == MERGE_REQUEST:
        return DECIDED_IN_GITLAB
    if decision == "reject" and found.stage and not reason:
        return REASON_REQUIRED
    state = "accepted" if decision == "accept" else "rejected"
    moved = await conn.execute(
        "UPDATE proposals SET state = %s, decided_by = %s, decided_at = now(), reason = %s,"
        " detail = NULL, checked_at = NULL,"
        " apply_lease_until = CASE WHEN %s THEN now() + make_interval(secs => %s) END"
        " WHERE id = %s AND state = ANY(%s)",
        (
            state,
            access.principal,
            reason if state == "rejected" else None,
            state == "accepted",
            APPLY_LEASE_SECONDS,
            uuid.UUID(proposal_id),
            list(DECIDABLE),
        ),
    )
    if not moved.rowcount:
        return ALREADY_DECIDED
    decided = await read_proposal(conn, access, proposal_id)
    assert decided is not None
    return decided


async def record_apply(
    conn: AsyncConnection,
    proposal_id: str,
    state: str,
    detail: str | None,
    *,
    from_states: tuple[str, ...] = ("accepted",),
) -> bool:
    """What the write server made of an accepted proposal, or a preview that found it stale;
    False when the row had moved on already (another apply, a decision)."""
    moved = await conn.execute(
        "UPDATE proposals SET state = %s, detail = %s, apply_lease_until = NULL"
        " WHERE id = %s AND state = ANY(%s)",
        (state, detail, uuid.UUID(proposal_id), list(from_states)),
    )
    return bool(moved.rowcount)


async def list_reports(
    conn: AsyncConnection,
    access: Access,
    *,
    agent: str | None = None,
    page: str | None = None,
    page_size: int = REPORTS_PAGE_SIZE,
) -> ReportPage:
    """The reports of goal runs that proposed nothing (ADR 0018), newest first."""
    after, after_id = _after(page)
    cursor = await conn.execute(
        "SELECT r.task_id, r.agent, r.record, r.proposal_settled_at, r.report, r.id FROM runs r"
        " WHERE r.outcome = 'reported' AND r.report IS NOT NULL"
        # The report is stored before its branch is deleted; it is a report once both are done.
        " AND r.proposal_settled_at IS NOT NULL"
        " AND (r.caller = %(principal)s OR r.agent = ANY(%(reviews)s))"
        " AND (%(agent)s::text IS NULL OR r.agent = %(agent)s)"
        " AND (%(after)s::timestamptz IS NULL"
        "  OR (r.proposal_settled_at, r.id) < (%(after)s, %(after_id)s))"
        " ORDER BY r.proposal_settled_at DESC, r.id DESC LIMIT %(limit)s",
        {
            **_access(access),
            "agent": agent,
            "after": after,
            "after_id": after_id,
            "limit": page_size + 1,
        },
    )
    rows = await cursor.fetchall()
    items = tuple(
        ReportSummary(
            task_id=task_id,
            agent=agent_name,
            target=_target(record),
            completed_at=settled.isoformat(),
            summary=_first_line(report),
        )
        for task_id, agent_name, record, settled, report, _ in rows[:page_size]
    )
    more = len(rows) > page_size
    last = rows[page_size - 1] if more else None
    return ReportPage(
        items, None if last is None else page_token(last[3].isoformat(), str(last[5]))
    )


async def read_report(conn: AsyncConnection, access: Access, task_id: str) -> Report | None:
    cursor = await conn.execute(
        "SELECT t.task_id, r.agent, r.record, r.proposal_settled_at, r.report"
        " FROM run_tasks t JOIN runs r ON r.id = t.run_id"
        " WHERE t.task_id = %(task)s AND r.outcome = 'reported' AND r.report IS NOT NULL"
        " AND r.proposal_settled_at IS NOT NULL"
        " AND (r.caller = %(principal)s OR r.agent = ANY(%(reviews)s))",
        {**_access(access), "task": task_id},
    )
    row = await cursor.fetchone()
    if row is None:
        return None
    _, agent, record, settled, text = row
    return Report(task_id, agent, _target(record), settled.isoformat(), text)


def _target(record: str | None) -> str | None:
    # A goal run's record is `<writes>/<target>.md` (ADR 0017).
    return None if not record else PurePosixPath(record).stem


def _first_line(text: str) -> str:
    line = next((line.strip() for line in text.splitlines() if line.strip()), "")
    return line[:MAX_REPORT_SUMMARY]
