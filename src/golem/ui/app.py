"""The web UI: a backend-for-frontend in front of the edge (ADR 0011).

The browser gets server-rendered pages and one opaque session cookie. Tokens stay here: the
user's access token goes to the edge with every A2A call, so the edge sees the user, not the UI.
"""

import hmac
import re
import secrets
from collections.abc import Awaitable, Callable
from importlib.resources import files
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
from jinja2 import Environment, PackageLoader
from starlette.applications import Starlette
from starlette.datastructures import MutableHeaders
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response
from starlette.routing import Route
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from golem.metrics import Instrumented, Metrics
from golem.ratelimit import Decision, Limiter, Network, Rate, client_address
from golem.ui.edge import TASK_NOT_FOUND, EdgeError, EdgeUnauthorized, agent_card, rpc
from golem.ui.oidc import (
    OidcClient,
    OidcError,
    Tokens,
    authorization_url,
    end_session_url,
    new_verifier,
    s256,
)
from golem.ui.store import LOGIN_LIFETIME_SECONDS, Session, SessionStore, digest
from golem.ui.views import AGENT_METADATA, task_view

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
MAX_FORM_BYTES = 16 * 1024
MAX_GOAL_CHARS = 4000
LIST_PAGE_SIZE = 50
# a2a-sdk's page token is the base64 of a task id; anything else never came from the edge.
PAGE_TOKEN = re.compile(r"^[A-Za-z0-9+/=_-]{1,256}$")
# ADR 0012: /login writes a row per request, POST /tasks starts a run.
LOGIN_RATE = Rate(per_minute=30, burst=10)
START_RATE = Rate(per_minute=10, burst=5)
UNKNOWN_ADDRESS = "unknown"
NONCE = re.compile(r"^[A-Za-z0-9_-]{22,64}$")
FORM = "application/x-www-form-urlencoded"

Handler = Callable[[Request, Session], Awaitable[Response]]


def security_headers(app: ASGIApp, *, hsts: bool) -> ASGIApp:
    """ASVS 5.0, 3.4.1 and 3.4.3 to 3.4.6 on every response, errors and redirects included."""

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
                # Pages carry the user's tasks and a CSRF token; no cache keeps them.
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


def csrf_ok(session: Session, form: dict[str, str]) -> bool:
    return hmac.compare_digest(form.get("csrf", "").encode(), session.csrf.encode())


async def form_of(request: Request) -> dict[str, str] | None:
    """An urlencoded form, bounded; None for anything else."""
    if request.headers.get("content-type", "").split(";")[0].strip() != FORM:
        return None
    body = b""
    async for chunk in request.stream():
        body += chunk
        if len(body) > MAX_FORM_BYTES:
            return None
    try:
        parsed = parse_qs(body.decode("utf-8"), max_num_fields=20)
    except (UnicodeDecodeError, ValueError):
        return None
    return {name: values[0] for name, values in parsed.items()}


def create_ui_app(
    *,
    oidc: OidcClient,
    store: SessionStore,
    edge: httpx.AsyncClient,
    agents: tuple[str, ...],
    public_base_url: str,
    page_size: int = LIST_PAGE_SIZE,
    logins: Limiter | None = None,
    starts: Limiter | None = None,
    trusted_proxies: tuple[Network, ...] = (),
    metrics: Metrics | None = None,
) -> ASGIApp:
    if not agents:
        raise ValueError("the UI needs at least one agent")
    metrics = Metrics("ui") if metrics is None else metrics
    logins = Limiter(LOGIN_RATE) if logins is None else logins
    starts = Limiter(START_RATE) if starts is None else starts
    templates = Environment(loader=PackageLoader("golem.ui", "templates"), autoescape=True)
    stylesheet = files("golem.ui").joinpath("static/golem.css").read_bytes()

    def render(name: str, status_code: int = 200, **context: Any) -> HTMLResponse:
        return HTMLResponse(templates.get_template(name).render(**context), status_code)

    def error(status_code: int, message: str, session: Session | None = None) -> HTMLResponse:
        return render("error.html", status_code, message=message, session=session)

    def too_many(decision: Decision, session: Session | None = None) -> Response:
        response = error(429, f"Too many requests. Try again in {decision.retry_after} s.", session)
        response.headers["Retry-After"] = str(decision.retry_after)
        return response

    def to_login(request: Request) -> Response:
        response = RedirectResponse("/login", status_code=303)
        if SESSION_COOKIE in request.cookies:
            response.headers.append("set-cookie", cleared(SESSION_COOKIE))
        return response

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

    def authenticated(handler: Handler) -> Callable[[Request], Awaitable[Response]]:
        async def endpoint(request: Request) -> Response:
            session = await session_of(request)
            if session is None:
                return to_login(request)
            try:
                return await handler(request, session)
            except EdgeUnauthorized:
                await store.delete(request.cookies.get(SESSION_COOKIE, ""))
                return to_login(request)

        return endpoint

    async def home(request: Request) -> Response:
        session = await session_of(request)
        if session is not None:
            return RedirectResponse("/tasks", status_code=303)
        return render("home.html", session=None)

    async def login(request: Request) -> Response:
        # Every sign-in started stores a transaction; anonymous GETs must not fill the table.
        peer = request.client.host if request.client else None
        address = client_address(peer, request.headers.getlist("x-forwarded-for"), trusted_proxies)
        decision = logins.take(address or UNKNOWN_ADDRESS)
        if not decision.allowed:
            metrics.rate_limit_refused("login")
            return too_many(decision)
        try:
            discovery = await oidc.discovery()
        except OidcError:
            return error(503, "The identity provider is unavailable. Try again later.")
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
            return error(400, "This sign-in expired or was not started here. Sign in again.")
        if "error" in request.query_params:
            metrics.authentication_failed()
            return error(400, f"The identity provider refused: {request.query_params['error']}")
        code = request.query_params.get("code", "")
        if not code:
            metrics.authentication_failed()
            return error(400, "The identity provider sent no authorization code.")
        try:
            tokens = await oidc.exchange(code, login.verifier)
            if tokens.id_token is None:
                raise OidcError("the token response has no ID token")
            claims = await oidc.verify(tokens.id_token, nonce=login.nonce)
        except OidcError:
            metrics.authentication_failed()
            return error(400, "Sign-in failed. Sign in again.")
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

    async def logout(request: Request, session: Session) -> Response:
        form = await form_of(request) or {}
        if not csrf_ok(session, form):
            return error(403, "The form expired. Reload the page and try again.", session)
        await store.delete(request.cookies.get(SESSION_COOKIE, ""))
        try:
            target = end_session_url(
                await oidc.discovery(),
                id_token=session.id_token,
                client_id=oidc.client_id,
                post_logout_redirect_uri=f"{public_base_url}/",
            )
        except OidcError:
            target = None
        # Browsers apply CSP form-action to the redirects that follow a form's POST, so this
        # POST cannot redirect to the provider; the page it returns refreshes there instead.
        response = (
            RedirectResponse("/", status_code=303)
            if target is None
            else render("signed_out.html", session=None, target=target)
        )
        response.headers.append("set-cookie", cleared(SESSION_COOKIE))
        return response

    async def agent_list(request: Request, session: Session) -> Response:
        cards = [(name, await agent_card(edge, name)) for name in agents]
        return render("agents.html", session=session, cards=cards)

    async def new_task(request: Request, session: Session) -> Response:
        chosen = request.query_params.get("agent", "")
        return render(
            "new_task.html",
            session=session,
            agents=agents,
            chosen=chosen if chosen in agents else agents[0],
            nonce=secrets.token_urlsafe(24),
            max_goal=MAX_GOAL_CHARS,
        )

    async def start(request: Request, session: Session) -> Response:
        decision = starts.take(session.id)
        if not decision.allowed:
            metrics.rate_limit_refused("start")
            return too_many(decision, session)
        form = await form_of(request) or {}
        if not csrf_ok(session, form):
            return error(403, "The form expired. Reload the page and try again.", session)
        agent, goal, nonce = (
            form.get("agent", ""),
            form.get("goal", "").strip(),
            form.get("nonce", ""),
        )
        if agent not in agents:
            return error(400, "Choose one of the listed agents.", session)
        if not goal or len(goal) > MAX_GOAL_CHARS:
            return error(400, f"The goal must be 1 to {MAX_GOAL_CHARS} characters.", session)
        if not NONCE.fullmatch(nonce):
            return error(400, "The form is malformed. Reload the page and try again.", session)
        message = {
            # One message id per rendered form: a double submit is the same message, and the
            # orchestrator starts one run per (caller, message id).
            "messageId": f"ui:{nonce}",
            "role": "ROLE_USER",
            "parts": [{"text": goal}],
            "metadata": {AGENT_METADATA: agent},
        }
        try:
            result = await rpc(
                edge, session.access_token, "SendMessage", {"tenant": agent, "message": message}
            )
        except EdgeError as failure:
            return error(502, f"The task was not started: {failure}", session)
        task = result.get("task") if isinstance(result.get("task"), dict) else {}
        if not isinstance(task.get("id"), str) or not task["id"]:
            return error(502, "The task was not started.", session)
        return RedirectResponse(f"/tasks/{agent}/{task['id']}", status_code=303)

    async def task_list(request: Request, session: Session) -> Response:
        page = request.query_params.get("page")
        if page is not None and not PAGE_TOKEN.fullmatch(page):
            return error(400, "This page of tasks does not exist.", session)
        # The task service lists the caller's own tasks whatever the tenant; the tenant is
        # there for the edge's call registry.
        params: dict[str, Any] = {"tenant": agents[0], "pageSize": page_size}
        if page is not None:
            params["pageToken"] = page
        try:
            result = await rpc(edge, session.access_token, "ListTasks", params)
        except EdgeError as failure:
            return error(502, f"Tasks could not be listed: {failure}", session)
        tasks = result.get("tasks") if isinstance(result.get("tasks"), list) else []
        views = [view for view in (task_view(t, agents) for t in tasks) if view is not None]
        next_page = result.get("nextPageToken")
        return render(
            "tasks.html",
            session=session,
            tasks=views,
            paged=page is not None,
            next_page=next_page
            if isinstance(next_page, str) and PAGE_TOKEN.fullmatch(next_page)
            else None,
        )

    async def task_detail(request: Request, session: Session) -> Response:
        agent, task_id = request.path_params["agent"], request.path_params["task_id"]
        if agent not in agents:
            return error(404, "No such task.", session)
        try:
            task = await rpc(
                edge, session.access_token, "GetTask", {"tenant": agent, "id": task_id}
            )
        except EdgeError as failure:
            if failure.code == TASK_NOT_FOUND:
                return error(404, "No such task.", session)
            return error(502, f"The task could not be read: {failure}", session)
        view = task_view(task, agents)
        if view is None:
            return error(502, "The edge returned an unreadable task.", session)
        return render("task.html", session=session, task=view, agent=agent)

    async def cancel(request: Request, session: Session) -> Response:
        agent, task_id = request.path_params["agent"], request.path_params["task_id"]
        form = await form_of(request) or {}
        if not csrf_ok(session, form):
            return error(403, "The form expired. Reload the page and try again.", session)
        if agent not in agents:
            return error(404, "No such task.", session)
        try:
            await rpc(edge, session.access_token, "CancelTask", {"tenant": agent, "id": task_id})
        except EdgeError as failure:
            if failure.code == TASK_NOT_FOUND:
                return error(404, "No such task.", session)
            return error(409, f"The task was not canceled: {failure}", session)
        return RedirectResponse(f"/tasks/{agent}/{task_id}", status_code=303)

    async def css(request: Request) -> Response:
        return Response(stylesheet, media_type="text/css")

    async def not_found(request: Request, exc: Exception) -> Response:
        return error(404, "Nothing here.")

    app = Starlette(
        routes=[
            Route("/", home, methods=["GET"]),
            Route("/login", login, methods=["GET"]),
            Route(CALLBACK_PATH, callback, methods=["GET"]),
            Route("/logout", authenticated(logout), methods=["POST"]),
            Route("/agents", authenticated(agent_list), methods=["GET"]),
            Route("/tasks", authenticated(task_list), methods=["GET"]),
            Route("/tasks", authenticated(start), methods=["POST"]),
            Route("/tasks/new", authenticated(new_task), methods=["GET"]),
            Route("/tasks/{agent}/{task_id}", authenticated(task_detail), methods=["GET"]),
            Route("/tasks/{agent}/{task_id}/cancel", authenticated(cancel), methods=["POST"]),
            Route("/static/golem.css", css, methods=["GET"]),
        ],
        exception_handlers={404: not_found},
    )
    headed = security_headers(app, hsts=urlsplit(public_base_url).scheme == "https")
    return Instrumented(headed, routes=app.routes, metrics=metrics)
