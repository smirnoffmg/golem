"""What every channel adapter shares: its service identity, the A2A request, the push tokens.

An adapter is an A2A client of the edge, authenticated as a service with OAuth 2.0 client
credentials, that learns a task's outcome from A2A push notifications carrying a per-run token.
"""

import hashlib
import hmac
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import httpx
from a2a.utils.constants import VERSION_HEADER
from starlette.requests import Request
from starlette.responses import Response

from golem.ratelimit import Limiter, Network, client_address

PUSH_PATH = "/a2a/push"
EDGE_RPC_PATH = "/a2a"
A2A_VERSION = "1.0"
# The header a2a-sdk's push sender puts the configured per-task token in.
NOTIFICATION_TOKEN_HEADER = "X-A2A-Notification-Token"
TERMINAL_STATES = frozenset(
    {
        "TASK_STATE_COMPLETED",
        "TASK_STATE_FAILED",
        "TASK_STATE_CANCELED",
        "TASK_STATE_REJECTED",
    }
)
TOKEN_REFRESH_MARGIN_SECONDS = 30.0
UNKNOWN_ADDRESS = "unknown"

log = logging.getLogger("golem.adapters")


@dataclass(frozen=True)
class PushUpdate:
    task_id: str
    state: str
    text: str


class ServiceTokenUnavailable(Exception):
    pass


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


def rate_limited(
    limiter: Limiter, request: Request, trusted_proxies: tuple[Network, ...]
) -> Response | None:
    """429 when the client's address is over its rate (ADR 0012), before any secret is checked,
    so a flood of forged requests costs no HMAC and no log line past the limit."""
    peer = request.client.host if request.client else None
    address = client_address(peer, request.headers.getlist("x-forwarded-for"), trusted_proxies)
    decision = limiter.take(address or UNKNOWN_ADDRESS)
    if decision.allowed:
        return None
    return Response(status_code=429, headers={"Retry-After": str(decision.retry_after)})


def push_token(secret: bytes, subject: str, message: str) -> str:
    """A token naming where a run's outcome goes; only the adapter holding the secret can mint
    one, so a push that names a subject the adapter never started is refused."""
    nonce = hashlib.sha256(message.encode()).hexdigest()[:16]
    return f"{subject}.{nonce}.{_push_mac(secret, subject, nonce)}"


def subject_of_push_token(secret: bytes, token: str) -> str | None:
    parts = token.rsplit(".", 2)
    if len(parts) != 3 or not all(parts):
        return None
    subject, nonce, mac = parts
    return subject if hmac.compare_digest(_push_mac(secret, subject, nonce), mac) else None


def send_message_request(
    *,
    agent: str,
    message: str,
    goal: str,
    push_url: str,
    token: str,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {"messageId": message, "role": "ROLE_USER", "parts": [{"text": goal}]}
    if metadata:
        body["metadata"] = metadata
    return {
        "jsonrpc": "2.0",
        "id": message,
        "method": "SendMessage",
        "params": {
            "tenant": agent,
            "message": body,
            "configuration": {"taskPushNotificationConfig": {"url": push_url, "token": token}},
        },
    }


async def start_task(
    edge: httpx.AsyncClient,
    service_token: Callable[[], Awaitable[str]],
    request: dict[str, Any],
    *,
    what: str,
) -> str | None:
    """Send the request to the edge as the service; the new task's id, or None, logged."""
    try:
        headers = {"Authorization": f"Bearer {await service_token()}", VERSION_HEADER: A2A_VERSION}
        response = await edge.post(EDGE_RPC_PATH, json=request, headers=headers)
    except (ServiceTokenUnavailable, httpx.HTTPError) as error:
        log.warning("could not start %s: %s", what, error)
        return None
    task_id = task_id_of(response)
    if task_id is None:
        log.warning("edge did not start %s: %s %s", what, response.status_code, response.text[:500])
    return task_id


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


def state_word(state: str) -> str:
    return state.removeprefix("TASK_STATE_").lower()


def _push_mac(secret: bytes, subject: str, nonce: str) -> str:
    return hmac.new(secret, f"push:{subject}.{nonce}".encode(), hashlib.sha256).hexdigest()


def _message_text(message: Any) -> str:
    if not isinstance(message, dict):
        return ""
    parts = message.get("parts") or []
    return "\n".join(
        p["text"] for p in parts if isinstance(p, dict) and isinstance(p.get("text"), str)
    )
