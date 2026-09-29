"""Proposals and reports on the task service's edge port (ADR 0015, ADR 0018).

The edge authenticated a person and forwards their principal with the agents they review
(``X-Golem-Reviews``); golem_runs answers only rows that person owns or reviews, so any other id
is 404. Accepting applies at once through the write server; an apply that does not answer in
15 s leaves the proposal accepted, and the reconciler asks for it again.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from golem.decisions import DECISIONS, NAME, PAGE, REVIEWS_HEADER, STATES
from golem.metrics import Metrics
from golem.resolution import MAX_REASON_CHARS
from golem.tasks.ports import (
    ALREADY_DECIDED,
    DECIDED_IN_GITLAB,
    NOT_FOUND,
    REASON_REQUIRED,
    UNNAMEABLE_DECIDER,
    Access,
    Applier,
    Orchestrator,
    ProposalDetail,
    ProposalSummary,
    Report,
    ReportSummary,
)

PRINCIPAL_HEADER = "x-golem-principal"
APPLY_SECONDS = 15.0
REFUSALS = {
    NOT_FOUND: 404,
    ALREADY_DECIDED: 409,
    DECIDED_IN_GITLAB: 409,
    REASON_REQUIRED: 400,
    UNNAMEABLE_DECIDER: 403,
}

# Shows a proposal's current state on the tasks of its run.
Show = Callable[[str], Awaitable[None]]

log = logging.getLogger(__name__)


class Malformed(ValueError):
    pass


def access_of(request: Request) -> Access | None:
    principal = request.headers.get(PRINCIPAL_HEADER, "")
    if not principal:
        return None
    reviews = request.headers.get(REVIEWS_HEADER, "")
    return Access(principal, frozenset(name for name in reviews.split(",") if NAME.fullmatch(name)))


def _name(request: Request, key: str) -> str | None:
    value = request.query_params.get(key)
    if value is not None and not NAME.fullmatch(value):
        raise Malformed(key)
    return value


def _page(request: Request) -> str | None:
    value = request.query_params.get("page")
    if value is not None and not PAGE.fullmatch(value):
        raise Malformed("page")
    return value


def _states(request: Request) -> tuple[str, ...] | None:
    value = request.query_params.get("state")
    if value is None:
        return None
    states = tuple(value.split(","))
    if not set(states) <= STATES:
        raise Malformed("state")
    return states


def summary_json(item: ProposalSummary) -> dict[str, Any]:
    return {
        "id": item.id,
        "taskId": item.task_id,
        "agent": item.agent,
        "kind": item.kind,
        "state": item.state,
        "summary": item.summary,
        "url": item.url,
        "owner": item.owner,
        "createdAt": item.created_at,
        "decidedBy": item.decided_by,
        "decidedAt": item.decided_at,
    }


def detail_json(found: ProposalDetail) -> dict[str, Any]:
    # Payloads were written in an untrusted Job: they are data, and no client renders them as
    # HTML (ADR 0015).
    return {
        **summary_json(found.summary),
        "payload": dict(found.payload),
        "target": found.target,
        "reason": found.reason,
        "detail": found.detail,
        "report": found.report,
        "stage": found.stage,
    }


def report_summary_json(item: ReportSummary) -> dict[str, Any]:
    return {
        "taskId": item.task_id,
        "agent": item.agent,
        "target": item.target,
        "completedAt": item.completed_at,
        "summary": item.summary,
    }


def report_json(report: Report) -> dict[str, Any]:
    return {
        "taskId": report.task_id,
        "agent": report.agent,
        "target": report.target,
        "completedAt": report.completed_at,
        "text": report.text,
    }


def error(code: str, status: int) -> JSONResponse:
    return JSONResponse({"error": code}, status_code=status)


async def apply_accepted(
    orchestrator: Orchestrator,
    applier: Applier,
    decided: ProposalDetail,
    metrics: Metrics | None = None,
) -> None:
    """Apply an accepted proposal and record what came of it; an apply that fails to answer
    leaves it accepted for the reconciler's retry."""
    kind = decided.summary.kind
    try:
        async with asyncio.timeout(APPLY_SECONDS):
            result = await applier.apply(decided)
    except Exception:
        log.exception("the apply of proposal %s did not answer", decided.summary.id)
        if metrics is not None:
            metrics.proposal_applied(kind, "unanswered")
        return
    if metrics is not None:
        metrics.proposal_applied(kind, result.state)
    await orchestrator.record_apply(decided.summary.id, result.state, result.detail, ("accepted",))


def proposal_routes(
    orchestrator: Orchestrator, applier: Applier, show: Show, metrics: Metrics | None = None
) -> list[Route]:
    async def listed(request: Request) -> Response:
        access = access_of(request)
        if access is None:
            return error("not_found", 404)
        try:
            page = await orchestrator.list_proposals(
                access,
                _name(request, "agent"),
                _states(request),
                _name(request, "process"),
                _page(request),
            )
        except ValueError:
            return error("malformed", 400)
        return JSONResponse(
            {"proposals": [summary_json(item) for item in page.items], "next": page.next}
        )

    async def one(request: Request) -> Response:
        access = access_of(request)
        proposal_id = request.path_params["proposal_id"]
        found = None if access is None else await orchestrator.read_proposal(access, proposal_id)
        if access is None or found is None:
            return error("not_found", 404)
        body = detail_json(found)
        if found.summary.kind == "wiki_edit" and found.summary.state in ("pending", "failed"):
            body |= await _live(found, access)
            if body.get("stale"):
                await show(proposal_id)
        return JSONResponse(body)

    async def _live(found: ProposalDetail, access: Access) -> dict[str, Any]:
        # What a person judges is the platform's reading of the page, not the Job's: the diff
        # is taken against the live body, and a moved version makes the proposal stale now.
        try:
            live = await applier.preview(found, access.principal)
        except Exception as problem:
            return {"live": None, "liveError": str(problem)[:500]}
        shown: dict[str, Any] = {
            "live": {"title": live.title, "version": live.version, "body": live.body}
        }
        if live.version != found.payload.get("version") and await orchestrator.record_apply(
            found.summary.id, "stale", None, ("pending", "failed")
        ):
            shown |= {"state": "stale", "stale": True}
        return shown

    async def decision(request: Request) -> Response:
        access = access_of(request)
        if access is None:
            return error("not_found", 404)
        try:
            body = await request.json()
        except ValueError:
            body = None
        choice = body.get("decision") if isinstance(body, dict) else None
        reason = body.get("reason") if isinstance(body, dict) else None
        if choice not in DECISIONS or not (
            reason is None or (isinstance(reason, str) and len(reason) <= MAX_REASON_CHARS)
        ):
            return error("malformed", 400)
        proposal_id = request.path_params["proposal_id"]
        decided = await orchestrator.decide_proposal(
            access, proposal_id, str(choice), reason or None
        )
        if isinstance(decided, str):
            return error(decided, REFUSALS.get(decided, 409))
        if metrics is not None:
            metrics.proposal_decided(decided.summary.kind, str(choice))
        if decided.summary.state == "accepted":
            await apply_accepted(orchestrator, applier, decided, metrics)
        await show(proposal_id)
        now = await orchestrator.read_proposal(access, proposal_id)
        return JSONResponse(detail_json(now or decided))

    async def reports(request: Request) -> Response:
        access = access_of(request)
        if access is None:
            return error("not_found", 404)
        try:
            page = await orchestrator.list_reports(access, _name(request, "agent"), _page(request))
        except ValueError:
            return error("malformed", 400)
        return JSONResponse(
            {"reports": [report_summary_json(item) for item in page.items], "next": page.next}
        )

    async def report(request: Request) -> Response:
        access = access_of(request)
        found = (
            None
            if access is None
            else await orchestrator.read_report(access, request.path_params["task_id"])
        )
        if found is None:
            return error("not_found", 404)
        return JSONResponse(report_json(found))

    return [
        Route("/proposals", listed, methods=["GET"]),
        Route("/proposals/{proposal_id}", one, methods=["GET"]),
        Route("/proposals/{proposal_id}/decision", decision, methods=["POST"]),
        Route("/reports", reports, methods=["GET"]),
        Route("/reports/{task_id}", report, methods=["GET"]),
    ]
