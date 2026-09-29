"""The web UI's backend-for-frontend (ADRs 0011 and 0018).

The board's static files come from a container of their own; this process signs people in, keeps
their tokens and answers JSON. The browser gets one opaque session cookie. Every call to the edge
carries the user's own access token, so the edge sees the user, not the UI.
"""

import hmac
import json
import re
import secrets
import time
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

import httpx
from starlette.applications import Starlette
from starlette.datastructures import MutableHeaders
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response
from starlette.routing import Route
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from golem.adapters.common import TERMINAL_STATES
from golem.decisions import DECISIONS, PAGE, TASK_ID
from golem.metrics import Instrumented, Metrics
from golem.ratelimit import (
    LOGIN_RATE,
    START_RATE,
    Decision,
    Limiter,
    Network,
    address_key,
    client_address,
)
from golem.resolution import ACTIONS as RESOLUTION_ACTIONS
from golem.resolution import MAX_REASON_CHARS
from golem.ui.board import CURSOR_OVERLAP, card, cursor_after, detail, format_cursor, parse_cursor
from golem.ui.edge import (
    INVALID_PARAMS,
    TASK_NOT_CANCELABLE,
    TASK_NOT_FOUND,
    EdgeError,
    EdgeLimited,
    EdgeRefused,
    EdgeUnauthorized,
    directory,
    for_person,
    resolve,
    rpc,
)
from golem.ui.oidc import (
    OidcClient,
    OidcError,
    Tokens,
    authorization_url,
    end_session_url,
    new_verifier,
    s256,
)
from golem.ui.proposals import (
    proposal_cards,
    proposal_detail,
    report_cards,
    report_detail,
    review_counts,
)
from golem.ui.store import (
    LOGIN_LIFETIME_SECONDS,
    SESSION_LIFETIME_SECONDS,
    Session,
    SessionStore,
    digest,
)

# The __Host- prefix makes the browser refuse the cookie unless it is Secure, has Path=/ and
# no Domain, so no sibling host can set or shadow it (ASVS 5.0, 3.3.1).
SESSION_COOKIE = "__Host-golem-session"
LOGIN_COOKIE = "__Host-golem-login"
COOKIE_ATTRIBUTES = "Path=/; Secure; HttpOnly; SameSite=Lax"
CALLBACK_PATH = "/callback"
# ASVS 5.0, 3.4.3 asks for object-src 'none' besides the rest.
CONTENT_SECURITY_POLICY = (
    "default-src 'self'; frame-ancestors 'none'; form-action 'self'; base-uri 'none';"
    " object-src 'none'"
)
HSTS = "max-age=31536000; includeSubDomains"
# Not a CORS-safelisted header, so a cross-site page cannot send it without a preflight, which
# this BFF never permits (ASVS 5.0, 3.5.1).
CSRF_HEADER = "X-Golem-CSRF"
JSON_TYPE = "application/json"
MAX_BODY_BYTES = 16 * 1024
MAX_TEXT_CHARS = 4000
BOARD_SIZE = 100
PAGE_SIZE = 50
# The edge's max-age for the directory.
DIRECTORY_SECONDS = 60
# a2a-sdk's page token is the base64 of a task id; anything else never came from the edge.
PAGE_TOKEN = re.compile(r"^[A-Za-z0-9+/=_-]{1,256}$")
NONCE = re.compile(r"^[A-Za-z0-9_-]{22,64}$")
# The edge's refusals of a resolution, in words the board shows as they are.
RESOLUTION_MESSAGES = {
    "reason_required": "Say why the stage should run again.",
    "not_waiting": "The process no longer waits for a reason. Reload the board.",
    "malformed": "The answer is not one the process takes.",
}
DECISION_MESSAGES = {
    "reason_required": "Say why you reject it: the stage runs again with your reason.",
    "already_decided": "Someone decided on this proposal already. Reload it.",
    "decided_in_gitlab": "A merge request is decided in GitLab: merge or close it there.",
    "agents_do_not_decide": "Only a person decides on a proposal.",
    "unnameable_decider": (
        "Your sign-in name has a space, a colon or a '*', which a decision cannot carry."
        " Ask an operator to decide it."
    ),
    "malformed": "The decision is not one the proposal takes.",
}
# Open: waiting for a person, being applied, or refused by the target (ADR 0015).
OPEN_STATES = "pending,accepted,failed"
# What a person can act on now: counted in the left column.
WAITING_STATES = "pending,failed"

Read = Callable[[Request, Session], Awaitable[Response]]
Write = Callable[[Request, Session, dict[str, Any]], Awaitable[Response]]
Endpoint = Callable[[Request], Awaitable[Response]]


class Refusal(Exception):
    """An answer other than success, as ``{"error": <code>, "message": <text>}``."""

    def __init__(self, status: int, error: str, message: str, **headers: str) -> None:
        super().__init__(message)
        self.status = status
        self.error = error
        self.message = message
        self.headers = headers


def refused(refusal: Refusal) -> JSONResponse:
    return JSONResponse(
        {"error": refusal.error, "message": refusal.message},
        status_code=refusal.status,
        headers=refusal.headers,
    )


def malformed(message: str) -> Refusal:
    return Refusal(400, "malformed", message)


def not_found() -> Refusal:
    return Refusal(404, "not_found", "No such agent or task.")


def edge_failed(error: EdgeError) -> Refusal:
    return Refusal(502, "edge_failed", f"The edge failed: {error}")


def limited(decision: Decision) -> Refusal:
    return Refusal(
        429,
        "rate_limited",
        f"Too many requests. Try again in {decision.retry_after} s.",
        **{"Retry-After": str(decision.retry_after)},
    )


def security_headers(app: ASGIApp, *, hsts: bool) -> ASGIApp:
    """ASVS 5.0, 3.4.1 and 3.4.3 to 3.4.8 on every response, errors and redirects included."""

    async def wrapped(scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers["Content-Security-Policy"] = CONTENT_SECURITY_POLICY
                headers["X-Content-Type-Options"] = "nosniff"
                headers["Referrer-Policy"] = "same-origin"
                headers["Cross-Origin-Opener-Policy"] = "same-origin"
                # Answers carry the user's tasks and a CSRF token; no cache keeps them.
                headers["Cache-Control"] = "no-store"
                if hsts:
                    headers["Strict-Transport-Security"] = HSTS
            await send(message)

        await app(scope, receive, send_with_headers)

    return wrapped


def cookie(name: str, value: str, max_age: int | None = None) -> str:
    return f"{name}={value}; {COOKIE_ATTRIBUTES}" + (
        f"; Max-Age={max_age}" if max_age is not None else ""
    )


def cleared(name: str) -> str:
    return cookie(name, "", max_age=0)


def signin_failed(code: str) -> Response:
    """The board shows a fixed message per code; the identity provider's text never reaches
    the page."""
    return RedirectResponse(f"/?signin={code}", status_code=303)


def utc(seconds: float) -> str:
    return datetime.fromtimestamp(seconds, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


async def json_body(request: Request) -> dict[str, Any]:
    """A JSON object of at most 16 KiB; the content type is checked before anything is read."""
    if request.headers.get("content-type", "").split(";")[0].strip().lower() != JSON_TYPE:
        raise Refusal(415, "unsupported_media_type", f"The body must be {JSON_TYPE}.")
    body = b""
    async for chunk in request.stream():
        body += chunk
        if len(body) > MAX_BODY_BYTES:
            raise Refusal(413, "too_large", "The body is larger than 16 KiB.")
    if not body:
        return {}
    try:
        parsed = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as error:
        raise malformed("The body is not JSON.") from error
    if not isinstance(parsed, dict):
        raise malformed("The body must be a JSON object.")
    return parsed


def text_field(body: dict[str, Any], name: str) -> str:
    value = body.get(name)
    if not isinstance(value, str) or not value.strip() or len(value) > MAX_TEXT_CHARS:
        raise malformed(f"{name} must be 1 to {MAX_TEXT_CHARS} characters.")
    return value


def nonce_field(body: dict[str, Any]) -> str:
    # Drawn by the board when it opens a form: a double submit is one message id, and the
    # orchestrator starts one run per (caller, message id).
    value = body.get("nonce")
    if not isinstance(value, str) or not NONCE.fullmatch(value):
        raise malformed("nonce must be 22 to 64 URL-safe characters.")
    return value


def task_of(result: dict[str, Any]) -> dict[str, Any]:
    shown = card(result.get("task"))
    if shown is None:
        raise Refusal(502, "edge_failed", "The edge returned no task.")
    return shown


def cards_of(result: dict[str, Any]) -> list[dict[str, Any]]:
    tasks = result.get("tasks")
    shown = (card(task) for task in (tasks if isinstance(tasks, list) else []))
    return [each for each in shown if each is not None]


def create_ui_app(
    *,
    oidc: OidcClient,
    store: SessionStore,
    edge: httpx.AsyncClient,
    public_base_url: str,
    board_size: int = BOARD_SIZE,
    page_size: int = PAGE_SIZE,
    logins: Limiter | None = None,
    starts: Limiter | None = None,
    trusted_proxies: tuple[Network, ...] = (),
    metrics: Metrics | None = None,
    clock: Callable[[], float] = time.time,
) -> ASGIApp:
    metrics = Metrics("ui") if metrics is None else metrics
    logins = Limiter(LOGIN_RATE) if logins is None else logins
    starts = Limiter(START_RATE) if starts is None else starts
    # Per session: when it was read and what it listed. A name outside it costs no edge call.
    directories: dict[str, tuple[float, list[dict[str, Any]]]] = {}

    async def renew(session: Session) -> Tokens | None:
        if session.refresh_token is None:
            return None
        try:
            tokens = await oidc.refresh(session.refresh_token)
            if tokens.id_token is not None:
                # Core 12.2: a refreshed ID token names the same subject.
                claims = await oidc.verify(tokens.id_token, nonce=None)
                if claims["sub"] != session.subject:
                    return None
        except OidcError:
            return None
        return tokens

    async def session_of(request: Request) -> Session | None:
        value = request.cookies.get(SESSION_COOKIE)
        if not value:
            return None
        session = await store.get(value)
        if session is not None and store.needs_refresh(session):
            session = await store.renew(value, renew)
        return session

    def unauthenticated(request: Request) -> Response:
        # A fetch must not follow a redirect to the identity provider: the board navigates to
        # /login itself on a 401.
        response = refused(Refusal(401, "unauthenticated", "Sign in again."))
        if SESSION_COOKIE in request.cookies:
            response.headers.append("set-cookie", cleared(SESSION_COOKIE))
        return response

    def reading(handler: Read) -> Endpoint:
        async def endpoint(request: Request) -> Response:
            session = await session_of(request)
            if session is None:
                return unauthenticated(request)
            try:
                return await handler(request, session)
            except Refusal as refusal:
                return refused(refusal)
            except EdgeLimited as limit:
                return refused(
                    Refusal(
                        429,
                        "rate_limited",
                        str(limit),
                        **{"Retry-After": str(limit.retry_after)},
                    )
                )
            except EdgeUnauthorized:
                await store.delete(request.cookies.get(SESSION_COOKIE, ""))
                directories.pop(session.id, None)
                return unauthenticated(request)
            except EdgeError as error:
                return refused(edge_failed(error))

        return endpoint

    def writing(handler: Write) -> Endpoint:
        async def checked(request: Request, session: Session) -> Response:
            sent = request.headers.get(CSRF_HEADER, "")
            # Before anything else, as in ADR 0011; compared in constant time.
            if not hmac.compare_digest(sent.encode(), session.csrf.encode()):
                raise Refusal(403, "csrf", "The CSRF token is missing or wrong. Reload.")
            return await handler(request, session, await json_body(request))

        return reading(checked)

    async def agents_of(session: Session) -> list[dict[str, Any]]:
        now = clock()
        cached = directories.get(session.id)
        if cached is not None and now - cached[0] < DIRECTORY_SECONDS:
            return cached[1]
        listed = await directory(edge, session.access_token)
        for stale in [k for k, (at, _) in directories.items() if now - at >= DIRECTORY_SECONDS]:
            del directories[stale]
        directories[session.id] = (now, listed)
        return listed

    async def agent_named(request: Request, session: Session) -> str:
        agent = str(request.path_params["agent"])
        if agent not in {entry["name"] for entry in await agents_of(session)}:
            raise not_found()
        return agent

    async def call(session: Session, method: str, params: dict[str, Any]) -> dict[str, Any]:
        try:
            return await rpc(edge, session.access_token, method, params)
        except EdgeError as error:
            if error.code == TASK_NOT_FOUND:
                raise not_found() from error
            raise

    async def login(request: Request) -> Response:
        # Every sign-in started stores a transaction; anonymous GETs must not fill the table.
        peer = request.client.host if request.client else None
        address = client_address(peer, request.headers.getlist("x-forwarded-for"), trusted_proxies)
        decision = logins.take(address_key(address))
        if not decision.allowed:
            metrics.rate_limit_refused("login")
            return refused(limited(decision))
        try:
            discovery = await oidc.discovery()
        except OidcError:
            return signin_failed("failed")
        state, nonce, binding = (secrets.token_urlsafe(32) for _ in range(3))
        verifier = new_verifier()
        await store.begin_login(state=state, binding=binding, nonce=nonce, verifier=verifier)
        response = RedirectResponse(
            authorization_url(
                discovery,
                client_id=oidc.client_id,
                redirect_uri=oidc.redirect_url,
                state=state,
                nonce=nonce,
                code_challenge=s256(verifier),
            ),
            status_code=303,
        )
        # Binds the state to this browser (ASVS 5.0, 10.1.2): a code and state lured into
        # another browser do not sign that browser in.
        response.headers.append(
            "set-cookie", cookie(LOGIN_COOKIE, binding, max_age=LOGIN_LIFETIME_SECONDS)
        )
        return response

    async def callback(request: Request) -> Response:
        state = request.query_params.get("state", "")
        login = await store.take_login(state) if state else None
        binding = request.cookies.get(LOGIN_COOKIE, "")
        if login is None or not hmac.compare_digest(digest(binding), login.binding):
            metrics.authentication_failed()
            return signin_failed("expired")
        if "error" in request.query_params:
            metrics.authentication_failed()
            return signin_failed("refused")
        code = request.query_params.get("code", "")
        if not code:
            metrics.authentication_failed()
            return signin_failed("failed")
        try:
            tokens = await oidc.exchange(code, login.verifier)
            if tokens.id_token is None:
                raise OidcError("the token response has no ID token")
            claims = await oidc.verify(tokens.id_token, nonce=login.nonce)
        except OidcError:
            metrics.authentication_failed()
            return signin_failed("failed")
        name = claims.get("preferred_username")
        session_cookie, _ = await store.create(
            subject=claims["sub"],
            name=name if isinstance(name, str) and name else claims["sub"],
            tokens=tokens,
            replacing=request.cookies.get(SESSION_COOKIE),
        )
        response = RedirectResponse("/", status_code=303)
        response.headers.append("set-cookie", cookie(SESSION_COOKIE, session_cookie))
        response.headers.append("set-cookie", cleared(LOGIN_COOKIE))
        return response

    async def logout(request: Request, session: Session, body: dict[str, Any]) -> Response:
        await store.delete(request.cookies.get(SESSION_COOKIE, ""))
        directories.pop(session.id, None)
        try:
            target = end_session_url(
                await oidc.discovery(),
                id_token=session.id_token,
                client_id=oidc.client_id,
                post_logout_redirect_uri=f"{public_base_url}/",
            )
        except OidcError:
            target = None
        # The board navigates there itself: a script's navigation is not a form submission,
        # so CSP form-action does not block the provider (ADR 0018).
        response = JSONResponse({"redirect": target or "/"})
        response.headers.append("set-cookie", cleared(SESSION_COOKIE))
        return response

    async def health(request: Request) -> Response:
        return JSONResponse({"status": "ok"})

    async def session_info(request: Request, session: Session) -> Response:
        return JSONResponse(
            {
                "name": session.name,
                "csrf": session.csrf,
                "expiresAt": utc(session.created_at + SESSION_LIFETIME_SECONDS),
            }
        )

    async def agent_list(request: Request, session: Session) -> Response:
        return JSONResponse({"agents": await agents_of(session)})

    async def list_tasks(session: Session, params: dict[str, Any]) -> dict[str, Any]:
        try:
            return await call(session, "ListTasks", params)
        except EdgeError as error:
            if error.code == INVALID_PARAMS:
                raise malformed("This page of tasks does not exist.") from error
            raise edge_failed(error) from error

    async def agent_board(request: Request, session: Session) -> Response:
        since = request.query_params.get("since")
        if since is not None and parse_cursor(since) is None:
            raise malformed("since must be a cursor the board was given.")
        agent = await agent_named(request, session)
        snapshot = {"tenant": agent, "pageSize": board_size}
        complete = since is None
        result = await list_tasks(
            session, snapshot if since is None else snapshot | {"statusTimestampAfter": since}
        )
        cards = cards_of(result)
        # A full delta may have missed tasks past its page: start over from a snapshot.
        if not complete and len(cards) >= board_size:
            complete = True
            cards = cards_of(await list_tasks(session, snapshot))
        cursor = cursor_after(cards, None if complete else since) or format_cursor(
            datetime.fromtimestamp(clock(), UTC) - CURSOR_OVERLAP
        )
        # The open set is small and a proposal's change can come without its task's, so it
        # is sent whole with every answer (ADR 0018).
        listed = await person(
            session, "GET", "/proposals", params={"agent": agent, "state": OPEN_STATES}
        )
        return JSONResponse(
            {
                "agent": agent,
                "complete": complete,
                "cursor": cursor,
                "tasks": cards,
                "proposals": proposal_cards(listed.get("proposals")),
            }
        )

    async def archive(request: Request, session: Session) -> Response:
        page = request.query_params.get("page")
        if page is not None and not PAGE_TOKEN.fullmatch(page):
            raise malformed("This page of tasks does not exist.")
        agent = await agent_named(request, session)
        params: dict[str, Any] = {"tenant": agent, "pageSize": page_size}
        if page is not None:
            params["pageToken"] = page
        result = await list_tasks(session, params)
        following = result.get("nextPageToken")
        return JSONResponse(
            {
                "tasks": cards_of(result),
                "next": following
                if isinstance(following, str) and PAGE_TOKEN.fullmatch(following)
                else None,
            }
        )

    async def read_task(session: Session, agent: str, task_id: str) -> dict[str, Any]:
        try:
            return await call(session, "GetTask", {"tenant": agent, "id": task_id})
        except EdgeError as error:
            raise edge_failed(error) from error

    async def task_detail(request: Request, session: Session) -> Response:
        agent = await agent_named(request, session)
        shown = detail(await read_task(session, agent, str(request.path_params["task_id"])))
        if shown is None:
            raise Refusal(502, "edge_failed", "The edge returned an unreadable task.")
        return JSONResponse(shown)

    def take_start(session: Session) -> None:
        decision = starts.take(session.id)
        if not decision.allowed:
            metrics.rate_limit_refused("start")
            raise limited(decision)

    async def start(request: Request, session: Session, body: dict[str, Any]) -> Response:
        take_start(session)
        goal, nonce = text_field(body, "goal").strip(), nonce_field(body)
        agent = await agent_named(request, session)
        message = {"messageId": f"ui:{nonce}", "role": "ROLE_USER", "parts": [{"text": goal}]}
        try:
            result = await call(session, "SendMessage", {"tenant": agent, "message": message})
        except EdgeError as error:
            raise edge_failed(error) from error
        return JSONResponse({"task": task_of(result)}, status_code=201)

    async def reply(request: Request, session: Session, body: dict[str, Any]) -> Response:
        take_start(session)
        text, nonce = text_field(body, "text"), nonce_field(body)
        agent = await agent_named(request, session)
        task_id = str(request.path_params["task_id"])
        task = await read_task(session, agent, task_id)
        status = task.get("status")
        if isinstance(status, dict) and status.get("state") in TERMINAL_STATES:
            raise Refusal(409, "conflict", "The task has ended.")
        message = {
            "messageId": f"ui:{nonce}",
            "taskId": task_id,
            # The task service may hold no live task to take the context from.
            "contextId": task.get("contextId", ""),
            "role": "ROLE_USER",
            "parts": [{"text": text}],
        }
        try:
            result = await call(session, "SendMessage", {"tenant": agent, "message": message})
        except EdgeError as error:
            if error.code == INVALID_PARAMS:
                raise Refusal(409, "conflict", "The task has ended.") from error
            raise edge_failed(error) from error
        shown = card(result.get("task")) or card(task)
        return JSONResponse({"task": shown})

    async def cancel(request: Request, session: Session, body: dict[str, Any]) -> Response:
        agent = await agent_named(request, session)
        task_id = str(request.path_params["task_id"])
        try:
            result = await call(session, "CancelTask", {"tenant": agent, "id": task_id})
        except EdgeError as error:
            if error.code == TASK_NOT_CANCELABLE:
                raise Refusal(409, "conflict", "The task has ended already.") from error
            raise edge_failed(error) from error
        return JSONResponse({"task": task_of({"task": result})})

    async def person(
        session: Session,
        method: str,
        path: str,
        messages: dict[str, str] | None = None,
        **options: Any,
    ) -> dict[str, Any]:
        try:
            return await for_person(edge, session.access_token, method, path, **options)
        except EdgeRefused as refusal:
            if refusal.status == 404:
                raise not_found() from refusal
            text = (messages or {}).get(refusal.error, "The edge refused the request.")
            raise Refusal(refusal.status, refusal.error, text) from refusal

    def page_of(request: Request) -> dict[str, str]:
        page = request.query_params.get("page")
        if page is None:
            return {}
        if not PAGE.fullmatch(page):
            raise malformed("This page does not exist.")
        return {"page": page}

    def proposal_path(request: Request, suffix: str = "") -> str:
        proposal_id = str(request.path_params["proposal_id"])
        try:
            uuid.UUID(proposal_id)
        except ValueError as error:
            raise not_found() from error
        return f"/proposals/{proposal_id}{suffix}"

    async def counts(request: Request, session: Session) -> Response:
        listed = await person(session, "GET", "/proposals", params={"state": WAITING_STATES})
        return JSONResponse(
            {
                "agents": review_counts(listed.get("proposals") or []),
                "more": isinstance(listed.get("next"), str),
            }
        )

    async def queue(request: Request, session: Session) -> Response:
        params = {"state": OPEN_STATES} | page_of(request)
        listed = await person(session, "GET", "/proposals", params=params)
        following = listed.get("next")
        return JSONResponse(
            {
                "proposals": proposal_cards(listed.get("proposals")),
                "next": following if isinstance(following, str) else None,
            }
        )

    async def proposal(request: Request, session: Session) -> Response:
        shown = proposal_detail(await person(session, "GET", proposal_path(request)))
        if shown is None:
            raise Refusal(502, "edge_failed", "The edge returned an unreadable proposal.")
        return JSONResponse(shown)

    async def decision(request: Request, session: Session, body: dict[str, Any]) -> Response:
        choice, reason = body.get("decision"), body.get("reason")
        if choice not in DECISIONS or not (
            reason is None or (isinstance(reason, str) and len(reason) <= MAX_REASON_CHARS)
        ):
            raise malformed("decision must be accept or reject, and reason text.")
        sent = {"decision": choice} | ({"reason": reason.strip()} if reason else {})
        path = proposal_path(request, "/decision")
        shown = proposal_detail(await person(session, "POST", path, DECISION_MESSAGES, json=sent))
        if shown is None:
            raise Refusal(502, "edge_failed", "The edge returned an unreadable proposal.")
        return JSONResponse(shown)

    async def reports(request: Request, session: Session) -> Response:
        agent = await agent_named(request, session)
        params = {"agent": agent} | page_of(request)
        listed = await person(session, "GET", "/reports", params=params)
        following = listed.get("next")
        return JSONResponse(
            {
                "reports": report_cards(listed.get("reports")),
                "next": following if isinstance(following, str) else None,
            }
        )

    async def report(request: Request, session: Session) -> Response:
        task_id = str(request.path_params["task_id"])
        if not TASK_ID.fullmatch(task_id):
            raise not_found()
        shown = report_detail(await person(session, "GET", f"/reports/{task_id}"))
        if shown is None:
            raise Refusal(502, "edge_failed", "The edge returned an unreadable report.")
        return JSONResponse(shown)

    async def resolution(request: Request, session: Session, body: dict[str, Any]) -> Response:
        # A rerun starts a run, so it counts against the same limit as starting one.
        take_start(session)
        action, reason = body.get("action"), body.get("reason")
        if action not in RESOLUTION_ACTIONS or not isinstance(reason, str | None):
            raise malformed("action must be rerun or end, and reason text.")
        task_id = str(request.path_params["task_id"])
        try:
            await resolve(edge, session.access_token, task_id, str(action), reason)
        except EdgeRefused as refusal:
            if refusal.status == 404:
                raise not_found() from refusal
            message = RESOLUTION_MESSAGES.get(refusal.error, "The process refused the answer.")
            raise Refusal(refusal.status, refusal.error, message) from refusal
        return JSONResponse({"taskId": task_id, "action": action})

    async def unmatched(request: Request, exc: Exception) -> Response:
        return refused(not_found())

    async def wrong_method(request: Request, exc: Exception) -> Response:
        return refused(Refusal(405, "method_not_allowed", "Not with this method."))

    tasks = "/api/agents/{agent}/tasks"
    app = Starlette(
        routes=[
            Route("/login", login, methods=["GET"]),
            Route(CALLBACK_PATH, callback, methods=["GET"]),
            Route("/logout", writing(logout), methods=["POST"]),
            Route("/healthz", health, methods=["GET"]),
            Route("/api/session", reading(session_info), methods=["GET"]),
            Route("/api/agents", reading(agent_list), methods=["GET"]),
            Route("/api/agents/{agent}/board", reading(agent_board), methods=["GET"]),
            Route(tasks, reading(archive), methods=["GET"]),
            Route(tasks, writing(start), methods=["POST"]),
            Route(f"{tasks}/{{task_id}}", reading(task_detail), methods=["GET"]),
            Route(f"{tasks}/{{task_id}}/messages", writing(reply), methods=["POST"]),
            Route(f"{tasks}/{{task_id}}/cancel", writing(cancel), methods=["POST"]),
            Route("/api/processes/{task_id}/resolution", writing(resolution), methods=["POST"]),
            Route("/api/review-counts", reading(counts), methods=["GET"]),
            Route("/api/proposals", reading(queue), methods=["GET"]),
            Route("/api/proposals/{proposal_id}", reading(proposal), methods=["GET"]),
            Route("/api/proposals/{proposal_id}/decision", writing(decision), methods=["POST"]),
            Route("/api/agents/{agent}/reports", reading(reports), methods=["GET"]),
            Route("/api/reports/{task_id}", reading(report), methods=["GET"]),
        ],
        exception_handlers={404: unmatched, 405: wrong_method},
    )
    headed = security_headers(app, hsts=urlsplit(public_base_url).scheme == "https")
    return Instrumented(headed, routes=app.routes, metrics=metrics)
