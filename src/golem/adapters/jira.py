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
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx
from a2a.utils.constants import VERSION_HEADER
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

WEBHOOK_PATH = "/jira/webhook"
PUSH_PATH = "/a2a/push"
EDGE_RPC_PATH = "/a2a"
A2A_VERSION = "1.0"
SIGNATURE_HEADER = "X-Hub-Signature"
SIGNATURE_METHOD = "sha256"
# The header a2a-sdk's push sender puts the configured per-task token in.
NOTIFICATION_TOKEN_HEADER = "X-A2A-Notification-Token"
ISSUE_UPDATED = "jira:issue_updated"
LABELS_FIELD = "labels"
TERMINAL_STATES = frozenset(
    {
        "TASK_STATE_COMPLETED",
        "TASK_STATE_FAILED",
        "TASK_STATE_CANCELED",
        "TASK_STATE_REJECTED",
    }
)
COMMENTS_PAGE_SIZE = 100
TOKEN_REFRESH_MARGIN_SECONDS = 30.0

log = logging.getLogger("golem.adapters.jira")


@dataclass(frozen=True)
class LabelAdded:
    issue_key: str
    summary: str
    label: str
    event: str


@dataclass(frozen=True)
class PushUpdate:
    task_id: str
    state: str
    text: str


class ServiceTokenUnavailable(Exception):
    pass


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


def push_token(secret: bytes, issue_key: str, message: str) -> str:
    nonce = hashlib.sha256(message.encode()).hexdigest()[:16]
    return f"{issue_key}.{nonce}.{_push_mac(secret, issue_key, nonce)}"


def issue_of_push_token(secret: bytes, token: str) -> str | None:
    parts = token.rsplit(".", 2)
    if len(parts) != 3 or not all(parts):
        return None
    issue_key, nonce, mac = parts
    return issue_key if hmac.compare_digest(_push_mac(secret, issue_key, nonce), mac) else None


def send_message_request(
    *, agent: str, message: str, goal: str, push_url: str, token: str
) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": message,
        "method": "SendMessage",
        "params": {
            "tenant": agent,
            "message": {"messageId": message, "role": "ROLE_USER", "parts": [{"text": goal}]},
            "configuration": {"taskPushNotificationConfig": {"url": push_url, "token": token}},
        },
    }


def task_id_of(response: httpx.Response) -> str | None:
    if response.status_code != 200:
        return None
    try:
        body = response.json()
    except ValueError:
        return None
    result = body.get("result") if isinstance(body, dict) else None
    task = result.get("task") if isinstance(result, dict) else None
    task_id = task.get("id") if isinstance(task, dict) else None
    return task_id if isinstance(task_id, str) and task_id else None


def push_update(payload: Any) -> PushUpdate | None:
    if not isinstance(payload, dict):
        return None
    if isinstance(payload.get("statusUpdate"), dict):
        event = payload["statusUpdate"]
        task_id = event.get("taskId")
    elif isinstance(payload.get("task"), dict):
        event = payload["task"]
        task_id = event.get("id")
    else:
        return None
    status = event.get("status") if isinstance(event.get("status"), dict) else {}
    state = status.get("state")
    if not isinstance(task_id, str) or not task_id or not isinstance(state, str):
        return None
    return PushUpdate(task_id, state, _message_text(status.get("message")))


def comment_marker(task_id: str) -> str:
    return f"golem-task:{task_id}"


def comment_body(update: PushUpdate) -> str:
    status = update.state.removeprefix("TASK_STATE_").lower()
    lines = [f"Golem run {status}.", update.text, comment_marker(update.task_id)]
    return "\n\n".join(line for line in lines if line)


def jira_authorization(*, user: str | None, token: str) -> str:
    if user is None:
        return f"Bearer {token}"
    return "Basic " + base64.b64encode(f"{user}:{token}".encode()).decode()


class ClientCredentials:
    """An access token from the OAuth 2.0 client credentials grant, reused until near expiry."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        token_url: str,
        client_id: str,
        client_secret: str,
        clock: Callable[[], float] = time.monotonic,
        refresh_margin_seconds: float = TOKEN_REFRESH_MARGIN_SECONDS,
    ) -> None:
        self._client = client
        self._token_url = token_url
        self._form = {
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
        }
        self._clock = clock
        self._margin = refresh_margin_seconds
        self._token: str | None = None
        self._expires_at = 0.0

    async def token(self) -> str:
        if self._token is not None and self._clock() < self._expires_at - self._margin:
            return self._token
        requested_at = self._clock()
        try:
            response = await self._client.post(self._token_url, data=self._form)
            response.raise_for_status()
            body = response.json()
            token, expires_in = body["access_token"], float(body["expires_in"])
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as error:
            raise ServiceTokenUnavailable(str(error)) from error
        if not isinstance(token, str) or not token:
            raise ServiceTokenUnavailable("token response has no access token")
        self._token, self._expires_at = token, requested_at + expires_in
        return token


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
) -> Starlette:
    push_url = f"{public_base_url}{PUSH_PATH}"

    async def start(added: LabelAdded, agent: str) -> str | None:
        message = message_id(added.issue_key, added.label, added.event)
        request = send_message_request(
            agent=agent,
            message=message,
            goal=goal_text(added.issue_key, added.summary),
            push_url=push_url,
            token=push_token(push_secret, added.issue_key, message),
        )
        try:
            headers = {
                "Authorization": f"Bearer {await service_token()}",
                VERSION_HEADER: A2A_VERSION,
            }
            response = await edge.post(EDGE_RPC_PATH, json=request, headers=headers)
        except (ServiceTokenUnavailable, httpx.HTTPError) as error:
            log.warning("could not start %s for %s: %s", agent, added.issue_key, error)
            return None
        task_id = task_id_of(response)
        if task_id is None:
            log.warning(
                "edge did not start %s for %s: %s %s",
                agent,
                added.issue_key,
                response.status_code,
                response.text[:500],
            )
        return task_id

    async def webhook(request: Request) -> Response:
        body = await request.body()
        if not signature_valid(webhook_secret, body, request.headers.get(SIGNATURE_HEADER)):
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
        issue_key = issue_of_push_token(
            push_secret, request.headers.get(NOTIFICATION_TOKEN_HEADER, "")
        )
        if issue_key is None:
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

    return Starlette(
        routes=[
            Route(WEBHOOK_PATH, webhook, methods=["POST"]),
            Route(PUSH_PATH, pushed, methods=["POST"]),
        ]
    )


def _label_set(value: Any) -> frozenset[str]:
    # Jira labels cannot contain spaces; the changelog lists them separated by spaces.
    return frozenset(value.split()) if isinstance(value, str) else frozenset()


def _push_mac(secret: bytes, issue_key: str, nonce: str) -> str:
    return hmac.new(secret, f"push:{issue_key}.{nonce}".encode(), hashlib.sha256).hexdigest()


def _message_text(message: Any) -> str:
    if not isinstance(message, dict):
        return ""
    parts = message.get("parts") or []
    return "\n".join(
        p["text"] for p in parts if isinstance(p, dict) and isinstance(p.get("text"), str)
    )


def _comments_path(issue_key: str) -> str:
    return f"/rest/api/2/issue/{quote(issue_key, safe='')}/comment"
