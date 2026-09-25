"""The web UI as a backend-for-frontend, end to end: browser -> UI -> real edge -> real task
service, with the identity provider faked at its HTTP boundary.

The fake identity provider implements what the UI calls, with the shapes of:

- OpenID Connect Discovery 1.0, section 3 (provider metadata: issuer, authorization_endpoint,
  token_endpoint, jwks_uri) and 4.3 (the issuer must equal the one discovery was asked for);
  OpenID Connect RP-Initiated Logout 1.0, section 2 (end_session_endpoint, id_token_hint,
  client_id, post_logout_redirect_uri).
- OpenID Connect Core 1.0, 3.1.2.1 (authorization request: scope with openid, response_type
  code, client_id, redirect_uri, state, nonce), 3.1.2.5 (code and state back to redirect_uri),
  3.1.2.6 (error and state back), 3.1.3.1 (token request), 3.1.3.3 (token response: id_token,
  access_token, token_type Bearer, expires_in), 3.1.3.7 (ID token validation), 12.1 and 12.2
  (refresh: iss, sub and aud of a new ID token are those of the first).
- RFC 6749, 2.3.1 (client_secret_basic, the id and secret form-urlencoded first), 4.1.3 (token
  request with the authorization_code grant), 5.1 (token response, no-store), 5.2 (error
  invalid_grant with status 400), 6 (refresh_token grant; a new refresh token replaces the old).
- RFC 7636, 4.1 (verifier: 43 to 128 unreserved characters), 4.2 (S256: BASE64URL(SHA256(
  ASCII(verifier)))), 4.3 (code_challenge, code_challenge_method), 4.5 and 4.6 (code_verifier in
  the token request, checked against the challenge).
"""

import base64
import hashlib
import re
import secrets
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from decimal import Decimal
from functools import partial
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

import httpx
import jwt
import psycopg
import pytest
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives.asymmetric import rsa
from test_edge_app import EDGE_TOKEN, discovery_card
from test_edge_jwks import jwk
from test_tasks_service import FakeOrchestrator, make_card
from test_tasks_to_runs import CATALOG, SIGNING_KEY, TEMPLATE, FakeLauncher
from testcontainers.community.postgres import PostgresContainer

from golem.edge.__main__ import authenticator
from golem.edge.app import create_edge_app
from golem.edge.policy import ChainLimits, Registry
from golem.jwks import SigningKeys, fetch_jwks
from golem.metrics import Metrics
from golem.orchestrator.admission import Limits
from golem.orchestrator.service import PostgresOrchestrator
from golem.ratelimit import Limiter, Rate, parse_networks
from golem.settings import SettingsError, ui_settings
from golem.tasks.app import create_listeners
from golem.tasks.ports import Orchestrator
from golem.ui.app import SESSION_COOKIE, create_ui_app
from golem.ui.oidc import CLOCK_SKEW_SECONDS, OidcClient, s256
from golem.ui.store import SessionStore, apply_schema
from golem.ui.views import merge_request_link

ISSUER = "https://idp.example.test/realms/golem"
DISCOVERY_URL = f"{ISSUER}/.well-known/openid-configuration"
AUTHORIZE_URL = f"{ISSUER}/protocol/openid-connect/auth"
TOKEN_URL = f"{ISSUER}/protocol/openid-connect/token"
JWKS_URL = f"{ISSUER}/protocol/openid-connect/certs"
END_SESSION_URL = f"{ISSUER}/protocol/openid-connect/logout"
CLIENT_ID = "golem-ui"
# Characters that client_secret_basic must form-urlencode before Base64 (RFC 6749, 2.3.1).
CLIENT_SECRET = "s3cret:with/odd+chars"
EDGE_AUDIENCE = "golem-edge"
PUBLIC_URL = "https://golem-ui.example.test"
REDIRECT_URL = f"{PUBLIC_URL}/callback"
AGENTS = ("discovery", "reviewer")
IDP_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
ROGUE_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
SESSION_KEY = Fernet.generate_key().decode()
CSP = "default-src 'self'; frame-ancestors 'none'; form-action 'self'; base-uri 'none'"
PKCE_ALPHABET = re.compile(r"^[A-Za-z0-9._~-]{43,128}$")


class Clock:
    def __init__(self) -> None:
        self.now = time.time()

    def __call__(self) -> float:
        return self.now


@dataclass
class Grant:
    user: str
    client_id: str
    redirect_uri: str
    nonce: str
    challenge: str


@dataclass
class FakeIdP:
    clock: Clock
    expires_in: int = 300
    codes: dict[str, Grant] = field(default_factory=dict)
    refresh_tokens: dict[str, Grant] = field(default_factory=dict)
    token_requests: list[dict[str, list[str]]] = field(default_factory=list)
    refuse_refresh: bool = False
    end_session: bool = True
    # Overrides of the ID token's claims, or another signing key, to test the UI's checks.
    id_token_claims: dict[str, Any] = field(default_factory=dict)
    id_token_key: Any = IDP_KEY

    def handle(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == DISCOVERY_URL and request.method == "GET":
            return httpx.Response(200, json=self.metadata())
        if url == JWKS_URL and request.method == "GET":
            return httpx.Response(200, json={"keys": [jwk(IDP_KEY, "idp-key")]})
        if url == TOKEN_URL and request.method == "POST":
            return self.token(request)
        return httpx.Response(404)

    def metadata(self) -> dict[str, Any]:
        metadata = {
            "issuer": ISSUER,
            "authorization_endpoint": AUTHORIZE_URL,
            "token_endpoint": TOKEN_URL,
            "jwks_uri": JWKS_URL,
            "response_types_supported": ["code"],
            "subject_types_supported": ["public"],
            "id_token_signing_alg_values_supported": ["RS256"],
            "code_challenge_methods_supported": ["S256"],
        }
        if self.end_session:
            metadata["end_session_endpoint"] = END_SESSION_URL
        return metadata

    def authorize(self, location: str, user: str) -> str:
        """The user agent at the authorization endpoint: the user signs in, a code is issued."""
        assert location.startswith(f"{AUTHORIZE_URL}?")
        params = {k: v for k, [v] in parse_qs(urlsplit(location).query).items()}
        assert params["response_type"] == "code"
        assert "openid" in params["scope"].split()
        assert params["client_id"] == CLIENT_ID
        assert params["redirect_uri"] == REDIRECT_URL
        assert params["code_challenge_method"] == "S256"
        assert params["state"] and params["nonce"]
        code = secrets.token_urlsafe(16)
        self.codes[code] = Grant(
            user, CLIENT_ID, params["redirect_uri"], params["nonce"], params["code_challenge"]
        )
        return code

    def token(self, request: httpx.Request) -> httpx.Response:
        assert request.headers["content-type"] == "application/x-www-form-urlencoded"
        scheme, _, credentials = request.headers.get("authorization", "").partition(" ")
        client_id, _, secret = base64.b64decode(credentials).decode().partition(":")
        if scheme != "Basic" or (unquote(client_id), unquote(secret)) != (CLIENT_ID, CLIENT_SECRET):
            return httpx.Response(401, json={"error": "invalid_client"})
        form = parse_qs(request.content.decode())
        self.token_requests.append(form)
        grant_type = form["grant_type"][0]
        if grant_type == "authorization_code":
            grant = self.codes.pop(form["code"][0], None)
            if (
                grant is None
                or form["redirect_uri"] != [grant.redirect_uri]
                or s256(form["code_verifier"][0]) != grant.challenge
            ):
                return httpx.Response(400, json={"error": "invalid_grant"})
            return self.issue(grant, nonce=grant.nonce)
        if grant_type == "refresh_token":
            grant = self.refresh_tokens.pop(form["refresh_token"][0], None)
            if grant is None or self.refuse_refresh:
                return httpx.Response(400, json={"error": "invalid_grant"})
            return self.issue(grant, nonce=None)
        return httpx.Response(400, json={"error": "unsupported_grant_type"})

    def issue(self, grant: Grant, nonce: str | None) -> httpx.Response:
        refresh_token = secrets.token_urlsafe(24)
        self.refresh_tokens[refresh_token] = grant
        # The edge checks the access token against the wall clock; the UI's clock may run ahead.
        now = int(time.time())
        access = jwt.encode(
            {
                "iss": ISSUER,
                "aud": EDGE_AUDIENCE,
                "azp": CLIENT_ID,
                "sub": f"sub-{grant.user}",
                "preferred_username": grant.user,
                "iat": now,
                "exp": now + self.expires_in,
            },
            IDP_KEY,
            algorithm="RS256",
            headers={"kid": "idp-key"},
        )
        issued = int(self.clock())
        id_claims = {
            "iss": ISSUER,
            "aud": CLIENT_ID,
            "sub": f"sub-{grant.user}",
            "preferred_username": grant.user,
            "iat": issued,
            "exp": issued + self.expires_in,
        } | ({"nonce": nonce} if nonce is not None else {})
        id_token = jwt.encode(
            id_claims | self.id_token_claims,
            self.id_token_key,
            algorithm="RS256",
            headers={"kid": "idp-key"},
        )
        return httpx.Response(
            200,
            headers={"Cache-Control": "no-store"},
            json={
                "access_token": access,
                "token_type": "Bearer",
                "expires_in": self.expires_in,
                "refresh_token": refresh_token,
                "id_token": id_token,
            },
        )


def ui_dsn_of(postgres: PostgresContainer, user: str = "golem_ui") -> str:
    host = postgres.get_container_host_ip()
    port = postgres.get_exposed_port(5432)
    password = f"dev-only-{user.replace('_', '-')}"
    return f"host={host} port={port} dbname=golem_ui user={user} password={password}"


@pytest.fixture
async def ui_db(postgres: PostgresContainer) -> AsyncIterator[str]:
    dsn = ui_dsn_of(postgres)
    async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as conn:
        await apply_schema(conn)
        await conn.execute("TRUNCATE sessions, logins")
    yield dsn


@dataclass
class Stack:
    app: Any
    idp: FakeIdP
    clock: Clock
    orchestrator: Any
    dsn: str

    def browser(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url=PUBLIC_URL)


def build_stack(
    audit_dsn: str, ui_dsn: str, orchestrator: Orchestrator, **ui_options: Any
) -> Stack:
    clock = Clock()
    idp = FakeIdP(clock)
    idp_sync = httpx.Client(transport=httpx.MockTransport(idp.handle))
    edge_keys = SigningKeys(partial(fetch_jwks, idp_sync, JWKS_URL))
    edge_keys.refresh()
    listeners = create_listeners(make_card(), orchestrator, edge_token=EDGE_TOKEN)
    edge = create_edge_app(
        authenticate=authenticator(edge_keys, issuer=ISSUER, audience=EDGE_AUDIENCE),
        registry=Registry(allowed_callers={agent: frozenset({"user:*"}) for agent in AGENTS}),
        limits=ChainLimits(max_depth=3),
        audit_dsn=audit_dsn,
        forward=httpx.AsyncClient(
            transport=httpx.ASGITransport(app=listeners.public), base_url="http://tasks"
        ),
        edge_token=EDGE_TOKEN,
        cards={"discovery": discovery_card()},
    )
    app = create_ui_app(
        oidc=OidcClient(
            http=httpx.AsyncClient(transport=httpx.MockTransport(idp.handle)),
            keys_http=idp_sync,
            issuer=ISSUER,
            discovery_url=DISCOVERY_URL,
            client_id=CLIENT_ID,
            client_secret=CLIENT_SECRET,
            redirect_url=REDIRECT_URL,
            clock=clock,
        ),
        store=SessionStore(ui_dsn, SESSION_KEY, clock=clock),
        edge=httpx.AsyncClient(transport=httpx.ASGITransport(app=edge), base_url="http://edge"),
        agents=AGENTS,
        public_base_url=PUBLIC_URL,
        **ui_options,
    )
    return Stack(app, idp, clock, orchestrator, ui_dsn)


@pytest.fixture
async def stack(audit_dsn: str, audit_admin_dsn: str, ui_db: str) -> Stack:
    return build_stack(audit_dsn, ui_db, FakeOrchestrator())


@pytest.fixture
async def browser(stack: Stack) -> AsyncIterator[httpx.AsyncClient]:
    async with stack.browser() as client:
        yield client


def query_of(location: str) -> dict[str, str]:
    return {k: v for k, [v] in parse_qs(urlsplit(location).query).items()}


async def start_login(browser: httpx.AsyncClient) -> str:
    response = await browser.get("/login")
    assert response.status_code == 303, response.text
    return response.headers["location"]


async def login(stack: Stack, browser: httpx.AsyncClient, user: str = "alice") -> httpx.Response:
    location = await start_login(browser)
    code = stack.idp.authorize(location, user)
    response = await browser.get(
        "/callback", params={"code": code, "state": query_of(location)["state"]}
    )
    assert response.status_code == 303, response.text
    return response


def hidden(page: str, name: str) -> str:
    match = re.search(rf'name="{name}" value="([^"]*)"', page)
    assert match, f"no hidden field {name}"
    return match[1]


async def start_task(
    browser: httpx.AsyncClient, goal: str = "fix the test", agent: str = "discovery"
) -> httpx.Response:
    form = (await browser.get("/tasks/new")).text
    return await browser.post(
        "/tasks",
        data={
            "agent": agent,
            "goal": goal,
            "csrf": hidden(form, "csrf"),
            "nonce": hidden(form, "nonce"),
        },
    )


async def session_rows(dsn: str) -> list[tuple[Any, ...]]:
    async with await psycopg.AsyncConnection.connect(dsn) as conn:
        cursor = await conn.execute("SELECT * FROM sessions")
        return await cursor.fetchall()


# --- Login ---------------------------------------------------------------------------------------


async def test_login_redirects_to_the_idp_with_state_nonce_and_an_s256_challenge(
    stack: Stack, browser: httpx.AsyncClient
) -> None:
    location = await start_login(browser)

    params = query_of(location)
    assert location.startswith(f"{AUTHORIZE_URL}?")
    assert params["response_type"] == "code"
    assert params["scope"].split()[0] == "openid"
    assert params["client_id"] == CLIENT_ID
    assert params["redirect_uri"] == REDIRECT_URL
    assert params["code_challenge_method"] == "S256"
    assert len(params["state"]) >= 22 and len(params["nonce"]) >= 22
    assert len(params["code_challenge"]) == 43
    assert "code_verifier" not in params


async def test_the_verifier_sent_to_the_token_endpoint_hashes_to_the_challenge(
    stack: Stack, browser: httpx.AsyncClient
) -> None:
    location = await start_login(browser)
    challenge = query_of(location)["code_challenge"]

    code = stack.idp.authorize(location, "alice")
    await browser.get("/callback", params={"code": code, "state": query_of(location)["state"]})

    [request] = stack.idp.token_requests
    verifier = request["code_verifier"][0]
    assert PKCE_ALPHABET.fullmatch(verifier)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    assert base64.urlsafe_b64encode(digest).rstrip(b"=").decode() == challenge
    assert request["grant_type"] == ["authorization_code"]
    assert request["redirect_uri"] == [REDIRECT_URL]


async def test_a_successful_login_sets_only_an_opaque_session_cookie(
    stack: Stack, browser: httpx.AsyncClient
) -> None:
    response = await login(stack, browser)

    assert response.headers["location"] == "/"
    [session_header] = [
        h for h in response.headers.get_list("set-cookie") if h.startswith(f"{SESSION_COOKIE}=")
    ]
    name_value, *attributes = [part.strip() for part in session_header.split(";")]
    assert sorted(attributes) == sorted(["Path=/", "Secure", "HttpOnly", "SameSite=Lax"])
    value = name_value.split("=", 1)[1]
    assert re.fullmatch(r"[A-Za-z0-9_-]{43}", value)
    assert "." not in value, "a JWT or any token must not reach the browser"
    assert SESSION_COOKIE == "__Host-golem-session"


async def test_the_login_transaction_cookie_is_host_bound_and_cleared_after_login(
    stack: Stack, browser: httpx.AsyncClient
) -> None:
    started = await browser.get("/login")
    [login_header] = started.headers.get_list("set-cookie")
    name, *attributes = [part.strip() for part in login_header.split(";")]
    assert name.startswith("__Host-")
    assert {"Path=/", "Secure", "HttpOnly", "SameSite=Lax"} <= set(attributes)
    assert "Max-Age=600" in attributes

    location = started.headers["location"]
    code = stack.idp.authorize(location, "alice")
    done = await browser.get(
        "/callback", params={"code": code, "state": query_of(location)["state"]}
    )
    cleared = [h for h in done.headers.get_list("set-cookie") if h.startswith(name.split("=")[0])]
    assert cleared and "Max-Age=0" in cleared[0]


async def test_a_state_is_single_use(stack: Stack, browser: httpx.AsyncClient) -> None:
    location = await start_login(browser)
    state = query_of(location)["state"]
    await browser.get(
        "/callback", params={"code": stack.idp.authorize(location, "alice"), "state": state}
    )
    browser.cookies.clear()
    await start_login(browser)

    replay = await browser.get(
        "/callback", params={"code": stack.idp.authorize(location, "alice"), "state": state}
    )

    assert replay.status_code == 400
    assert len(stack.idp.token_requests) == 1


async def test_a_state_expires_after_ten_minutes(stack: Stack, browser: httpx.AsyncClient) -> None:
    location = await start_login(browser)
    code = stack.idp.authorize(location, "alice")
    stack.clock.now += 601

    response = await browser.get(
        "/callback", params={"code": code, "state": query_of(location)["state"]}
    )

    assert response.status_code == 400
    assert stack.idp.token_requests == []
    assert await session_rows(stack.dsn) == []


async def test_a_state_from_another_browser_is_refused(stack: Stack) -> None:
    async with stack.browser() as victim, stack.browser() as attacker:
        location = await start_login(attacker)
        code = stack.idp.authorize(location, "mallory")

        response = await victim.get(
            "/callback", params={"code": code, "state": query_of(location)["state"]}
        )

    assert response.status_code == 400
    assert stack.idp.token_requests == []


async def test_an_unknown_state_is_refused(stack: Stack, browser: httpx.AsyncClient) -> None:
    await start_login(browser)

    response = await browser.get("/callback", params={"code": "x", "state": "made-up"})

    assert response.status_code == 400
    assert stack.idp.token_requests == []


async def test_an_error_from_the_idp_is_shown_and_starts_no_session(
    stack: Stack, browser: httpx.AsyncClient
) -> None:
    location = await start_login(browser)

    response = await browser.get(
        "/callback", params={"error": "access_denied", "state": query_of(location)["state"]}
    )

    assert response.status_code == 400
    assert "access_denied" in response.text
    assert await session_rows(stack.dsn) == []


@pytest.mark.parametrize(
    ("claims", "key"),
    [
        ({"nonce": "not-the-one-sent"}, IDP_KEY),
        ({"iss": "https://evil.example.test/realms/golem"}, IDP_KEY),
        ({"aud": "another-client"}, IDP_KEY),
        ({"aud": [CLIENT_ID, "another-client"], "azp": "another-client"}, IDP_KEY),
        ({}, ROGUE_KEY),
    ],
    ids=["nonce", "issuer", "audience", "azp", "signature"],
)
async def test_an_id_token_that_fails_validation_starts_no_session(
    stack: Stack, browser: httpx.AsyncClient, claims: dict[str, Any], key: Any
) -> None:
    stack.idp.id_token_claims = claims
    stack.idp.id_token_key = key
    location = await start_login(browser)

    response = await browser.get(
        "/callback",
        params={
            "code": stack.idp.authorize(location, "alice"),
            "state": query_of(location)["state"],
        },
    )

    assert response.status_code == 400
    assert SESSION_COOKIE not in response.headers.get("set-cookie", "")
    assert await session_rows(stack.dsn) == []


async def test_an_expired_id_token_starts_no_session(
    stack: Stack, browser: httpx.AsyncClient
) -> None:
    stack.idp.id_token_claims = {"exp": int(stack.clock()) - CLOCK_SKEW_SECONDS - 1}
    location = await start_login(browser)

    response = await browser.get(
        "/callback",
        params={
            "code": stack.idp.authorize(location, "alice"),
            "state": query_of(location)["state"],
        },
    )

    assert response.status_code == 400


async def test_a_login_replaces_the_session_the_browser_had(
    stack: Stack, browser: httpx.AsyncClient
) -> None:
    await login(stack, browser)
    first = browser.cookies[SESSION_COOKIE]

    await login(stack, browser)

    assert browser.cookies[SESSION_COOKIE] != first
    assert len(await session_rows(stack.dsn)) == 1


# --- Sessions ------------------------------------------------------------------------------------


async def test_tokens_are_encrypted_at_rest_and_the_session_id_is_not_stored(
    stack: Stack, browser: httpx.AsyncClient
) -> None:
    await login(stack, browser)
    [token_request] = stack.idp.token_requests
    [refresh_token] = stack.idp.refresh_tokens

    [row] = await session_rows(stack.dsn)

    stored = " ".join(str(column) for column in row)
    assert refresh_token not in stored
    assert "eyJ" not in stored, "a JWT (access or ID token) is stored in the clear"
    assert browser.cookies[SESSION_COOKIE] not in stored
    assert token_request["code"][0] not in stored


async def test_pages_need_a_session(stack: Stack, browser: httpx.AsyncClient) -> None:
    for path in ("/agents", "/tasks", "/tasks/new", "/tasks/discovery/some-task"):
        response = await browser.get(path)
        assert response.status_code == 303, path
        assert response.headers["location"] == "/login"


async def test_a_forged_session_cookie_is_no_session(
    stack: Stack, browser: httpx.AsyncClient
) -> None:
    browser.cookies.set(SESSION_COOKIE, secrets.token_urlsafe(32), domain="golem-ui.example.test")

    response = await browser.get("/tasks")

    assert response.status_code == 303


async def test_the_access_token_is_refreshed_near_expiry(
    stack: Stack, browser: httpx.AsyncClient
) -> None:
    await login(stack, browser)
    stack.clock.now += stack.idp.expires_in - 30

    response = await browser.get("/tasks")

    assert response.status_code == 200
    assert [r["grant_type"] for r in stack.idp.token_requests] == [
        ["authorization_code"],
        ["refresh_token"],
    ]
    # The refresh token was rotated; the new one is the one the session keeps.
    stack.clock.now += stack.idp.expires_in - 30
    assert (await browser.get("/tasks")).status_code == 200
    assert len(stack.idp.token_requests) == 3


async def test_a_token_far_from_expiry_is_not_refreshed(
    stack: Stack, browser: httpx.AsyncClient
) -> None:
    await login(stack, browser)
    stack.clock.now += 60

    await browser.get("/tasks")

    assert len(stack.idp.token_requests) == 1


async def test_a_failed_refresh_ends_the_session(stack: Stack, browser: httpx.AsyncClient) -> None:
    await login(stack, browser)
    stack.idp.refuse_refresh = True
    stack.clock.now += stack.idp.expires_in

    response = await browser.get("/tasks")

    assert response.status_code == 303
    assert response.headers["location"] == "/login"
    assert await session_rows(stack.dsn) == []
    assert "Max-Age=0" in response.headers["set-cookie"]


async def test_a_session_ends_after_its_absolute_lifetime(
    stack: Stack, browser: httpx.AsyncClient
) -> None:
    await login(stack, browser)
    stack.clock.now += 12 * 3600 + 1

    response = await browser.get("/tasks")

    assert response.status_code == 303


async def test_logout_ends_the_session_and_the_idp_session(
    stack: Stack, browser: httpx.AsyncClient
) -> None:
    await login(stack, browser)
    page = (await browser.get("/agents")).text

    response = await browser.post("/logout", data={"csrf": hidden(page, "csrf")})

    assert response.status_code == 303
    location = response.headers["location"]
    assert location.startswith(f"{END_SESSION_URL}?")
    params = query_of(location)
    assert params["client_id"] == CLIENT_ID
    assert params["post_logout_redirect_uri"] == f"{PUBLIC_URL}/"
    assert jwt.decode(params["id_token_hint"], options={"verify_signature": False})["aud"] == (
        CLIENT_ID
    )
    assert await session_rows(stack.dsn) == []
    assert (await browser.get("/tasks")).status_code == 303


async def test_logout_without_an_end_session_endpoint_returns_home(
    stack: Stack, browser: httpx.AsyncClient
) -> None:
    stack.idp.end_session = False
    await login(stack, browser)
    page = (await browser.get("/agents")).text

    response = await browser.post("/logout", data={"csrf": hidden(page, "csrf")})

    assert response.headers["location"] == "/"
    assert await session_rows(stack.dsn) == []


async def test_logout_needs_the_csrf_token(stack: Stack, browser: httpx.AsyncClient) -> None:
    await login(stack, browser)

    response = await browser.post("/logout", data={"csrf": "wrong"})

    assert response.status_code == 403
    assert len(await session_rows(stack.dsn)) == 1


# --- Pages ---------------------------------------------------------------------------------------


async def test_the_home_page_offers_sign_in_without_a_session(browser: httpx.AsyncClient) -> None:
    response = await browser.get("/")

    assert response.status_code == 200
    assert 'href="/login"' in response.text


async def test_agents_come_from_the_edges_public_cards(
    stack: Stack, browser: httpx.AsyncClient
) -> None:
    await login(stack, browser)

    response = await browser.get("/agents")

    assert response.status_code == 200
    assert "Turns an epic into product decisions." in response.text
    # "reviewer" has no card at the edge: listed as unavailable, not an error page.
    assert "reviewer" in response.text


async def test_starting_a_task_goes_through_the_edge_as_the_user(
    stack: Stack, browser: httpx.AsyncClient
) -> None:
    await login(stack, browser, user="alice")

    response = await start_task(browser, goal="write the H-2 evidence")

    assert response.status_code == 303
    assert re.fullmatch(r"/tasks/discovery/[0-9a-f-]+", response.headers["location"])
    [run] = stack.orchestrator.started
    assert (run.caller, run.agent, run.goal) == (
        "user:alice",
        "discovery",
        "write the H-2 evidence",
    )
    assert run.message_id.startswith("ui:")


async def test_a_double_submit_starts_one_run(
    audit_dsn: str, audit_admin_dsn: str, ui_db: str, runs_db: str
) -> None:
    launcher = FakeLauncher()
    orchestrator = PostgresOrchestrator(
        dsn=runs_db,
        limits=Limits(max_runs_per_caller=5, max_runs_per_root=5, budget_per_root=Decimal("10")),
        estimated_cost=Decimal("1"),
        launcher=launcher,
        template=TEMPLATE,
        catalogs={"discovery": CATALOG},
        signing_key=SIGNING_KEY,
        grants={"discovery": ()},
    )
    stack = build_stack(audit_dsn, ui_db, orchestrator)
    async with stack.browser() as browser:
        await login(stack, browser)
        form = (await browser.get("/tasks/new")).text
        data = {
            "agent": "discovery",
            "goal": "once",
            "csrf": hidden(form, "csrf"),
            "nonce": hidden(form, "nonce"),
        }

        first = await browser.post("/tasks", data=data)
        second = await browser.post("/tasks", data=data)

    assert first.status_code == second.status_code == 303
    # A retry relaunches the same Job (the launcher treats the name conflict as launched), so a
    # crash between recording and launching heals; what counts is one run.
    assert len({spec.run_id for spec in launcher.launched}) == 1
    async with await psycopg.AsyncConnection.connect(runs_db) as conn:
        cursor = await conn.execute("SELECT caller FROM runs")
        assert await cursor.fetchall() == [("user:alice",)]


async def test_each_form_gets_its_own_nonce(stack: Stack, browser: httpx.AsyncClient) -> None:
    await login(stack, browser)

    first = hidden((await browser.get("/tasks/new")).text, "nonce")
    second = hidden((await browser.get("/tasks/new")).text, "nonce")

    assert first != second


@pytest.mark.parametrize("csrf", [None, "", "wrong"])
async def test_a_start_without_the_sessions_csrf_token_is_refused(
    stack: Stack, browser: httpx.AsyncClient, csrf: str | None
) -> None:
    await login(stack, browser)
    form = (await browser.get("/tasks/new")).text
    data = {"agent": "discovery", "goal": "go", "nonce": hidden(form, "nonce")}
    if csrf is not None:
        data["csrf"] = csrf

    response = await browser.post("/tasks", data=data)

    assert response.status_code == 403
    assert stack.orchestrator.started == []


async def test_the_csrf_token_of_another_session_is_refused(stack: Stack) -> None:
    async with stack.browser() as alice, stack.browser() as bob:
        await login(stack, alice, "alice")
        await login(stack, bob, "bob")
        bobs = hidden((await bob.get("/tasks/new")).text, "csrf")
        form = (await alice.get("/tasks/new")).text

        response = await alice.post(
            "/tasks",
            data={"agent": "discovery", "goal": "go", "csrf": bobs, "nonce": hidden(form, "nonce")},
        )

    assert response.status_code == 403
    assert stack.orchestrator.started == []


async def test_an_agent_outside_the_list_is_refused(
    stack: Stack, browser: httpx.AsyncClient
) -> None:
    await login(stack, browser)

    response = await start_task(browser, agent="ghost")

    assert response.status_code == 400
    assert stack.orchestrator.started == []


async def test_my_tasks_lists_only_my_tasks(stack: Stack) -> None:
    async with stack.browser() as alice, stack.browser() as bob:
        await login(stack, alice, "alice")
        await login(stack, bob, "bob")
        await start_task(alice, goal="alice's goal")
        await start_task(bob, goal="bob's goal")

        alices = (await alice.get("/tasks")).text
        bobs = (await bob.get("/tasks")).text

    assert "alice&#39;s goal" in alices and "bob&#39;s goal" not in alices
    assert "bob&#39;s goal" in bobs and "alice&#39;s goal" not in bobs


async def test_task_detail_shows_the_state_and_goal(
    stack: Stack, browser: httpx.AsyncClient
) -> None:
    await login(stack, browser)
    location = (await start_task(browser, goal="look closer")).headers["location"]

    response = await browser.get(location)

    assert response.status_code == 200
    assert "working" in response.text
    assert "look closer" in response.text


async def test_another_users_task_is_not_found(stack: Stack) -> None:
    async with stack.browser() as alice, stack.browser() as bob:
        await login(stack, alice, "alice")
        await login(stack, bob, "bob")
        location = (await start_task(alice)).headers["location"]

        response = await bob.get(location)

    assert response.status_code == 404


async def test_cancel_cancels_the_task_through_the_edge(
    stack: Stack, browser: httpx.AsyncClient
) -> None:
    await login(stack, browser)
    location = (await start_task(browser)).headers["location"]
    page = (await browser.get(location)).text

    response = await browser.post(f"{location}/cancel", data={"csrf": hidden(page, "csrf")})

    assert response.status_code == 303
    assert response.headers["location"] == location
    assert stack.orchestrator.canceled == [location.rsplit("/", 1)[1]]
    assert "canceled" in (await browser.get(location)).text


async def test_cancel_without_the_csrf_token_cancels_nothing(
    stack: Stack, browser: httpx.AsyncClient
) -> None:
    await login(stack, browser)
    location = (await start_task(browser)).headers["location"]

    response = await browser.post(f"{location}/cancel", data={})

    assert response.status_code == 403
    assert stack.orchestrator.canceled == []


async def test_a_goal_with_markup_is_escaped_everywhere(
    stack: Stack, browser: httpx.AsyncClient
) -> None:
    await login(stack, browser)
    goal = "<script>alert(1)</script>"
    location = (await start_task(browser, goal=goal)).headers["location"]

    for page in ((await browser.get(location)).text, (await browser.get("/tasks")).text):
        assert "<script>" not in page
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page


@pytest.mark.parametrize(
    ("text", "link"),
    [
        (
            "Run 1 succeeded; merge request: https://gitlab.example.test/p/-/merge_requests/7",
            "https://gitlab.example.test/p/-/merge_requests/7",
        ),
        ("Run 1 succeeded; merge request: http://gitlab.local/mr/1", "http://gitlab.local/mr/1"),
        ("Run 1 succeeded; merge request: javascript:alert(1)", None),
        ("Run 1 succeeded; merge request: data:text/html,<b>x</b>", None),
        ("Run 1 succeeded; merge request: https://", None),
        ("Run 1 failed: the change was rejected by validation.", None),
        ("", None),
    ],
)
def test_only_an_http_merge_request_url_becomes_a_link(text: str, link: str | None) -> None:
    assert merge_request_link(text) == link


# --- Headers -------------------------------------------------------------------------------------


async def test_every_response_carries_the_security_headers(
    stack: Stack, browser: httpx.AsyncClient
) -> None:
    responses = [
        await browser.get("/"),
        await browser.get("/login"),
        await browser.get("/tasks"),
        await browser.get("/nowhere"),
        await browser.get("/static/golem.css"),
        await browser.get("/callback", params={"state": "x", "code": "y"}),
    ]
    await login(stack, browser)
    responses += [await browser.get("/agents"), await browser.post("/tasks", data={})]

    for response in responses:
        headers = response.headers
        assert headers["content-security-policy"].startswith(CSP), response.url
        assert "object-src 'none'" in headers["content-security-policy"]
        assert headers["x-content-type-options"] == "nosniff"
        assert headers["referrer-policy"] == "same-origin"
        assert headers["strict-transport-security"] == "max-age=31536000; includeSubDomains"
        assert headers["cache-control"] == "no-store"


async def test_pages_load_nothing_from_elsewhere_and_run_no_inline_script(
    stack: Stack, browser: httpx.AsyncClient
) -> None:
    await login(stack, browser)
    location = (await start_task(browser)).headers["location"]

    for path in ("/", "/agents", "/tasks", "/tasks/new", location):
        page = (await browser.get(path)).text
        assert not re.search(r"<script|\son[a-z]+=|style=", page), path
        assert not re.search(r'(src|href)="(https?:)?//', page), path


# --- Settings ------------------------------------------------------------------------------------

UI_ENV = {
    "GOLEM_OIDC_ISSUER": ISSUER,
    "GOLEM_OIDC_DISCOVERY_URL": DISCOVERY_URL,
    "GOLEM_OIDC_CLIENT_ID": CLIENT_ID,
    "GOLEM_OIDC_CLIENT_SECRET": CLIENT_SECRET,
    "GOLEM_OIDC_REDIRECT_URL": REDIRECT_URL,
    "GOLEM_EDGE_URL": "http://edge.golem-system.svc:8000",
    "GOLEM_UI_DSN": "host=db dbname=golem_ui user=golem_ui",
    "GOLEM_UI_SESSION_KEY": SESSION_KEY,
    "GOLEM_UI_AGENTS": "discovery, reviewer",
    "GOLEM_PUBLIC_BASE_URL": PUBLIC_URL,
}


def test_ui_settings_are_parsed() -> None:
    settings = ui_settings(UI_ENV | {"GOLEM_PORT": "8080"})

    assert settings.agents == ("discovery", "reviewer")
    assert settings.redirect_url == REDIRECT_URL
    assert settings.port == 8080
    assert CLIENT_SECRET not in repr(settings)
    assert SESSION_KEY not in repr(settings)


def test_every_missing_ui_variable_is_reported_at_once() -> None:
    with pytest.raises(SettingsError) as error:
        ui_settings({})

    for name in UI_ENV:
        assert name in str(error.value)


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("GOLEM_UI_SESSION_KEY", "not-a-fernet-key"),
        ("GOLEM_OIDC_REDIRECT_URL", "https://elsewhere.example.test/callback"),
        ("GOLEM_OIDC_REDIRECT_URL", f"{PUBLIC_URL}/other"),
        ("GOLEM_PUBLIC_BASE_URL", "http://golem-ui.example.test"),
        ("GOLEM_UI_AGENTS", " , "),
    ],
)
def test_bad_ui_settings_are_refused(name: str, value: str) -> None:
    with pytest.raises(SettingsError, match=name):
        ui_settings(UI_ENV | {name: value})


def test_plain_http_is_allowed_for_localhost_only() -> None:
    settings = ui_settings(
        UI_ENV
        | {
            "GOLEM_PUBLIC_BASE_URL": "http://localhost:8090",
            "GOLEM_OIDC_REDIRECT_URL": "http://localhost:8090/callback",
        }
    )

    assert settings.public_base_url == "http://localhost:8090"


async def test_the_ui_role_owns_its_database_and_nobody_else_reaches_it(
    postgres: PostgresContainer, ui_db: str
) -> None:
    with pytest.raises(psycopg.OperationalError):
        await psycopg.AsyncConnection.connect(ui_dsn_of(postgres, "golem_edge"))


# --- Rate limits and paging (ADR 0012) -----------------------------------------------------------


async def login_rows(dsn: str) -> int:
    async with await psycopg.AsyncConnection.connect(dsn) as conn:
        cursor = await conn.execute("SELECT count(*) FROM logins")
        row = await cursor.fetchone()
        assert row is not None
        return int(row[0])


def behind(stack: Stack, peer: str) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=stack.app, client=(peer, 40000)), base_url=PUBLIC_URL
    )


async def test_login_is_limited_per_client_address(
    audit_dsn: str, audit_admin_dsn: str, ui_db: str
) -> None:
    logins = Limiter(Rate(per_minute=60, burst=2), clock=Clock())
    stack = build_stack(audit_dsn, ui_db, FakeOrchestrator(), logins=logins)

    async with behind(stack, "203.0.113.5") as flood, behind(stack, "203.0.113.6") as other:
        started = [await flood.get("/login") for _ in range(2)]
        refused = await flood.get("/login")
        spoofed = await flood.get("/login", headers={"X-Forwarded-For": "198.51.100.1"})
        elsewhere = await other.get("/login")

    assert [r.status_code for r in started] == [303, 303]
    assert refused.status_code == 429 and spoofed.status_code == 429
    assert int(refused.headers["retry-after"]) >= 1
    assert refused.headers["content-security-policy"].startswith("default-src 'self'")
    assert elsewhere.status_code == 303
    assert await login_rows(ui_db) == 3


async def test_behind_a_trusted_proxy_login_is_limited_per_forwarded_client(
    audit_dsn: str, audit_admin_dsn: str, ui_db: str
) -> None:
    logins = Limiter(Rate(per_minute=60, burst=1), clock=Clock())
    stack = build_stack(
        audit_dsn,
        ui_db,
        FakeOrchestrator(),
        logins=logins,
        trusted_proxies=parse_networks("10.0.0.0/8"),
    )

    async with behind(stack, "10.0.0.9") as ingress:
        first = await ingress.get("/login", headers={"X-Forwarded-For": "203.0.113.5"})
        again = await ingress.get("/login", headers={"X-Forwarded-For": "203.0.113.5"})
        other = await ingress.get("/login", headers={"X-Forwarded-For": "203.0.113.6"})

    assert (first.status_code, again.status_code, other.status_code) == (303, 429, 303)


async def test_starting_tasks_is_limited_per_session(
    audit_dsn: str, audit_admin_dsn: str, ui_db: str
) -> None:
    starts = Limiter(Rate(per_minute=60, burst=1), clock=Clock())
    stack = build_stack(audit_dsn, ui_db, FakeOrchestrator(), starts=starts)

    async with stack.browser() as alice, stack.browser() as bob:
        await login(stack, alice, "alice")
        await login(stack, bob, "bob")
        first = await start_task(alice, goal="one")
        second = await start_task(alice, goal="two")
        bobs = await start_task(bob, goal="three")

    assert first.status_code == 303
    assert second.status_code == 429 and int(second.headers["retry-after"]) >= 1
    assert bobs.status_code == 303
    assert sorted(run.goal for run in stack.orchestrator.started) == ["one", "three"]


def next_page(page: str) -> str | None:
    match = re.search(r'<a rel="next" href="([^"]*)"', page)
    return match[1].replace("&amp;", "&") if match else None


async def test_my_tasks_follows_the_next_page_token(
    audit_dsn: str, audit_admin_dsn: str, ui_db: str
) -> None:
    stack = build_stack(audit_dsn, ui_db, FakeOrchestrator(), page_size=2)

    async with stack.browser() as browser:
        await login(stack, browser)
        for goal in ("goal one", "goal two", "goal three"):
            assert (await start_task(browser, goal=goal)).status_code == 303
        first = (await browser.get("/tasks")).text
        link = next_page(first)
        assert link is not None and link.startswith("/tasks?page=")
        second_response = await browser.get(link)
        second = second_response.text

    assert second_response.status_code == 200
    shown = [
        goal
        for goal in ("goal one", "goal two", "goal three")
        for page in (first, second)
        if goal in page
    ]
    assert sorted(shown) == ["goal one", "goal three", "goal two"]
    assert sum(first.count(g) for g in ("goal one", "goal two", "goal three")) == 2
    assert next_page(second) is None


@pytest.mark.parametrize("token", ["not a token", "x" * 300, "<script>"])
async def test_a_malformed_page_token_is_refused(
    stack: Stack, browser: httpx.AsyncClient, token: str
) -> None:
    await login(stack, browser)

    response = await browser.get("/tasks", params={"page": token})

    assert response.status_code == 400


# --- Metrics (ADR 0013) --------------------------------------------------------------------------


async def test_ui_requests_are_counted_by_route_template_and_refusals_by_kind(
    audit_dsn: str, audit_admin_dsn: str, ui_db: str
) -> None:
    metrics = Metrics("ui")
    logins = Limiter(Rate(per_minute=60, burst=2), clock=Clock())
    starts = Limiter(Rate(per_minute=60, burst=1), clock=Clock())
    stack = build_stack(
        audit_dsn, ui_db, FakeOrchestrator(), logins=logins, starts=starts, metrics=metrics
    )

    async with stack.browser() as browser:
        await login(stack, browser)
        await start_task(browser, goal="one")
        await start_task(browser, goal="two")
        for task_id in ("t-1", "t-2", "t-3"):
            await browser.get(f"/tasks/discovery/{task_id}")
        await browser.get("/admin")
        await browser.get("/login")
        await browser.get("/login")
        await browser.get("/callback", params={"code": "x", "state": "made-up"})

    routes = {
        s.labels["route"]
        for family in metrics.registry.collect()
        for s in family.samples
        if s.name == "golem_http_requests_total"
    }
    assert routes == {
        "/login",
        "/callback",
        "/tasks",
        "/tasks/new",
        "/tasks/{agent}/{task_id}",
        "unmatched",
    }
    value = metrics.registry.get_sample_value
    assert value("golem_rate_limit_refusals_total", {"process": "ui", "limit": "start"}) == 1
    assert value("golem_rate_limit_refusals_total", {"process": "ui", "limit": "login"}) == 1
    assert value("golem_authentication_failures_total", {"process": "ui"}) == 1
