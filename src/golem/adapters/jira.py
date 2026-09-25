"""The Jira channel adapter: a label on an issue starts an agent, the outcome becomes a comment.

Jira side: the Jira Cloud platform REST API v2 (also served by Jira Data Center under the same
path) and Jira's ``jira:issue_updated`` webhook signed with a secret (``X-Hub-Signature``).
Golem side: an A2A client of the edge, authenticated as a service with OAuth 2.0 client
credentials, receiving task updates as A2A push notifications.
"""

import base64
import hashlib
import hmac
import json
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from golem.adapters.common import (
    NOTIFICATION_TOKEN_HEADER,
    PUSH_PATH,
    TERMINAL_STATES,
    PushUpdate,
    push_token,
    push_update,
    rate_limited,
    send_message_request,
    start_task,
    state_word,
    subject_of_push_token,
)
from golem.metrics import Instrumented, Metrics
from golem.ratelimit import Limiter, Network, Rate

WEBHOOK_PATH = "/jira/webhook"
SIGNATURE_HEADER = "X-Hub-Signature"
SIGNATURE_METHOD = "sha256"
ISSUE_UPDATED = "jira:issue_updated"
LABELS_FIELD = "labels"
COMMENTS_PAGE_SIZE = 100
# Jira sends every update of every issue the webhook covers, from a few addresses (ADR 0012).
WEBHOOK_RATE = Rate(per_minute=300, burst=100)

log = logging.getLogger("golem.adapters.jira")


@dataclass(frozen=True)
class LabelAdded:
    issue_key: str
    summary: str
    label: str
    event: str


def signature_valid(secret: bytes, body: bytes, header: str | None) -> bool:
    method, _, signature = (header or "").partition("=")
    if method != SIGNATURE_METHOD or not signature:
        return False
    expected = hmac.new(secret, body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def labels_added(payload: Any) -> list[LabelAdded]:
    if not isinstance(payload, dict) or payload.get("webhookEvent") != ISSUE_UPDATED:
        return []
    issue = payload.get("issue")
    changelog = payload.get("changelog")
    if not isinstance(issue, dict) or not isinstance(changelog, dict):
        return []
    key = issue.get("key")
    fields = issue.get("fields") if isinstance(issue.get("fields"), dict) else {}
    summary = fields.get("summary") if isinstance(fields.get("summary"), str) else ""
    if not isinstance(key, str) or not key:
        return []
    event = str(payload.get("timestamp", ""))
    added: list[LabelAdded] = []
    for item in changelog.get("items") or []:
        if isinstance(item, dict) and item.get("field") == LABELS_FIELD:
            for label in _label_set(item.get("toString")) - _label_set(item.get("fromString")):
                added.append(LabelAdded(key, summary, label, event))
    return sorted(added, key=lambda a: a.label)


def message_id(issue_key: str, label: str, event: str) -> str:
    # Jira resends the same body on a retry, so the same id reaches the orchestrator, which
    # starts one run per (caller, message id). The event comes from the signed body, not a
    # header, so a replayed body with a new header cannot start a second run either.
    return f"jira:{issue_key}:{label}:{event}"


def goal_text(issue_key: str, summary: str) -> str:
    return f"Jira issue {issue_key}: {summary}" if summary else f"Jira issue {issue_key}"


def comment_marker(task_id: str) -> str:
    return f"golem-task:{task_id}"


def comment_body(update: PushUpdate) -> str:
    lines = [f"Golem run {state_word(update.state)}.", update.text, comment_marker(update.task_id)]
    return "\n\n".join(line for line in lines if line)


def jira_authorization(*, user: str | None, token: str) -> str:
    if user is None:
        return f"Bearer {token}"
    return "Basic " + base64.b64encode(f"{user}:{token}".encode()).decode()


async def comment_exists(jira: httpx.AsyncClient, issue_key: str, marker: str) -> bool:
    start = 0
    while True:
        response = await jira.get(
            _comments_path(issue_key),
            params={"startAt": start, "maxResults": COMMENTS_PAGE_SIZE, "orderBy": "-created"},
        )
        response.raise_for_status()
        page = response.json()
        comments = page.get("comments") or []
        if any(marker in str(c.get("body", "")) for c in comments):
            return True
        start += len(comments)
        if not comments or start >= int(page.get("total", 0)):
            return False


async def comment_once(jira: httpx.AsyncClient, issue_key: str, update: PushUpdate) -> None:
    # The marker in the comment itself is the record of delivery: it survives restarts and
    # replicas without a table, and the task service sends one task's updates one at a time.
    if await comment_exists(jira, issue_key, comment_marker(update.task_id)):
        return
    response = await jira.post(_comments_path(issue_key), json={"body": comment_body(update)})
    response.raise_for_status()


def create_jira_adapter_app(
    *,
    labels: Mapping[str, str],
    webhook_secret: bytes,
    push_secret: bytes,
    public_base_url: str,
    edge: httpx.AsyncClient,
    service_token: Callable[[], Awaitable[str]],
    jira: httpx.AsyncClient,
    inbound: Limiter | None = None,
    trusted_proxies: tuple[Network, ...] = (),
    metrics: Metrics | None = None,
) -> Instrumented:
    push_url = f"{public_base_url}{PUSH_PATH}"
    metrics = Metrics("jira-adapter") if metrics is None else metrics
    inbound = Limiter(WEBHOOK_RATE) if inbound is None else inbound

    async def start(added: LabelAdded, agent: str) -> str | None:
        message = message_id(added.issue_key, added.label, added.event)
        request = send_message_request(
            agent=agent,
            message=message,
            goal=goal_text(added.issue_key, added.summary),
            push_url=push_url,
            token=push_token(push_secret, added.issue_key, message),
        )
        return await start_task(edge, service_token, request, what=f"{agent} for {added.issue_key}")

    async def webhook(request: Request) -> Response:
        refused = rate_limited(inbound, request, trusted_proxies)
        if refused is not None:
            metrics.rate_limit_refused("webhook")
            return refused
        body = await request.body()
        if not signature_valid(webhook_secret, body, request.headers.get(SIGNATURE_HEADER)):
            metrics.authentication_failed()
            return Response(status_code=401)
        try:
            payload = json.loads(body)
        except ValueError:
            return Response(status_code=400)
        mapped = [(a, labels[a.label]) for a in labels_added(payload) if a.label in labels]
        if not mapped:
            return Response(status_code=204)
        task_ids = [await start(added, agent) for added, agent in mapped]
        if None in task_ids:
            # Jira retries on 5xx with the same body; runs already started are deduplicated.
            return Response(status_code=502)
        return JSONResponse({"tasks": task_ids}, status_code=202)

    async def pushed(request: Request) -> Response:
        issue_key = subject_of_push_token(
            push_secret, request.headers.get(NOTIFICATION_TOKEN_HEADER, "")
        )
        if issue_key is None:
            metrics.authentication_failed()
            return Response(status_code=401)
        try:
            update = push_update(await request.json())
        except ValueError:
            update = None
        if update is None:
            return Response(status_code=400)
        if update.state not in TERMINAL_STATES:
            return Response(status_code=204)
        try:
            await comment_once(jira, issue_key, update)
        except (httpx.HTTPError, ValueError) as error:
            log.warning("could not comment on %s for %s: %s", issue_key, update.task_id, error)
            return Response(status_code=502)
        return JSONResponse({"issue": issue_key, "task": update.task_id})

    app = Starlette(
        routes=[
            Route(WEBHOOK_PATH, webhook, methods=["POST"]),
            Route(PUSH_PATH, pushed, methods=["POST"]),
        ]
    )
    return Instrumented(app, routes=app.routes, metrics=metrics)


def _label_set(value: Any) -> frozenset[str]:
    # Jira labels cannot contain spaces; the changelog lists them separated by spaces.
    return frozenset(value.split()) if isinstance(value, str) else frozenset()


def _comments_path(issue_key: str) -> str:
    return f"/rest/api/2/issue/{quote(issue_key, safe='')}/comment"
