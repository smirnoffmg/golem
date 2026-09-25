"""An OpenID Connect provider faked at its HTTP boundary, as an httpx transport handler for
in-process tests and as an ASGI app a browser can sign in at.

It implements what the UI calls, with the shapes of:

- OpenID Connect Discovery 1.0, section 3 (provider metadata: issuer, authorization_endpoint,
  token_endpoint, jwks_uri) and 4.3 (the issuer must equal the one discovery was asked for);
  OpenID Connect RP-Initiated Logout 1.0, section 2 (end_session_endpoint, id_token_hint,
  client_id, post_logout_redirect_uri) and 3 (back only to a registered URI).
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
import json
import secrets
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, unquote, urlencode, urlsplit

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse, RedirectResponse, Response
from starlette.routing import Route
from starlette.types import ASGIApp

from golem.ui.oidc import s256

ISSUER = "https://idp.example.test/realms/golem"
CLIENT_ID = "golem-ui"
# Characters that client_secret_basic must form-urlencode before Base64 (RFC 6749, 2.3.1).
CLIENT_SECRET = "s3cret:with/odd+chars"
EDGE_AUDIENCE = "golem-edge"
PUBLIC_URL = "https://golem-ui.example.test"
IDP_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
KEY_ID = "idp-key"
DISCOVERY_PATH = "/.well-known/openid-configuration"
AUTHORIZE_PATH = "/protocol/openid-connect/auth"
TOKEN_PATH = "/protocol/openid-connect/token"
JWKS_PATH = "/protocol/openid-connect/certs"
END_SESSION_PATH = "/protocol/openid-connect/logout"


def jwk(private_key: Any, kid: str) -> dict[str, Any]:
    public = json.loads(jwt.get_algorithm_by_name("RS256").to_jwk(private_key.public_key()))
    return {**public, "kid": kid, "alg": "RS256", "use": "sig"}


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
    clock: Clock = field(default_factory=Clock)
    issuer: str = ISSUER
    client_id: str = CLIENT_ID
    client_secret: str = CLIENT_SECRET
    redirect_url: str = f"{PUBLIC_URL}/callback"
    audience: str = EDGE_AUDIENCE
    expires_in: int = 300
    codes: dict[str, Grant] = field(default_factory=dict)
    refresh_tokens: dict[str, Grant] = field(default_factory=dict)
    token_requests: list[dict[str, list[str]]] = field(default_factory=list)
    refuse_refresh: bool = False
    end_session: bool = True
    # Overrides of the ID token's claims, or another signing key, to test the UI's checks.
    id_token_claims: dict[str, Any] = field(default_factory=dict)
    id_token_key: Any = IDP_KEY

    @property
    def discovery_url(self) -> str:
        return f"{self.issuer}{DISCOVERY_PATH}"

    @property
    def authorize_url(self) -> str:
        return f"{self.issuer}{AUTHORIZE_PATH}"

    @property
    def jwks_url(self) -> str:
        return f"{self.issuer}{JWKS_PATH}"

    @property
    def end_session_url(self) -> str:
        return f"{self.issuer}{END_SESSION_PATH}"

    def handle(self, request: httpx.Request) -> httpx.Response:
        route = (request.method, request.url.path.removeprefix(urlsplit(self.issuer).path))
        if route == ("GET", DISCOVERY_PATH):
            return httpx.Response(200, json=self.metadata())
        if route == ("GET", JWKS_PATH):
            return httpx.Response(200, json={"keys": [jwk(IDP_KEY, KEY_ID)]})
        if route == ("POST", TOKEN_PATH):
            return self.token(request)
        return httpx.Response(404)

    def metadata(self) -> dict[str, Any]:
        metadata = {
            "issuer": self.issuer,
            "authorization_endpoint": self.authorize_url,
            "token_endpoint": f"{self.issuer}{TOKEN_PATH}",
            "jwks_uri": self.jwks_url,
            "response_types_supported": ["code"],
            "subject_types_supported": ["public"],
            "id_token_signing_alg_values_supported": ["RS256"],
            "code_challenge_methods_supported": ["S256"],
        }
        if self.end_session:
            metadata["end_session_endpoint"] = self.end_session_url
        return metadata

    def authorize(self, location: str, user: str) -> str:
        """The user agent at the authorization endpoint: the user signs in, a code is issued."""
        assert location.startswith(f"{self.authorize_url}?")
        params = {k: v for k, [v] in parse_qs(urlsplit(location).query).items()}
        assert params["response_type"] == "code"
        assert "openid" in params["scope"].split()
        assert params["client_id"] == self.client_id
        assert params["redirect_uri"] == self.redirect_url
        assert params["code_challenge_method"] == "S256"
        assert params["state"] and params["nonce"]
        code = secrets.token_urlsafe(16)
        self.codes[code] = Grant(
            user, self.client_id, params["redirect_uri"], params["nonce"], params["code_challenge"]
        )
        return code

    def token(self, request: httpx.Request) -> httpx.Response:
        assert request.headers["content-type"] == "application/x-www-form-urlencoded"
        scheme, _, credentials = request.headers.get("authorization", "").partition(" ")
        client_id, _, secret = base64.b64decode(credentials).decode().partition(":")
        if scheme != "Basic" or (unquote(client_id), unquote(secret)) != (
            self.client_id,
            self.client_secret,
        ):
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

    def access_token(self, user: str) -> str:
        # The edge checks the access token against the wall clock; the UI's clock may run ahead.
        now = int(time.time())
        return jwt.encode(
            {
                "iss": self.issuer,
                "aud": self.audience,
                "azp": self.client_id,
                "sub": f"sub-{user}",
                "preferred_username": user,
                "iat": now,
                "exp": now + self.expires_in,
            },
            IDP_KEY,
            algorithm="RS256",
            headers={"kid": KEY_ID},
        )

    def issue(self, grant: Grant, nonce: str | None) -> httpx.Response:
        refresh_token = secrets.token_urlsafe(24)
        self.refresh_tokens[refresh_token] = grant
        issued = int(self.clock())
        id_claims = {
            "iss": self.issuer,
            "aud": self.client_id,
            "sub": f"sub-{grant.user}",
            "preferred_username": grant.user,
            "iat": issued,
            "exp": issued + self.expires_in,
        } | ({"nonce": nonce} if nonce is not None else {})
        id_token = jwt.encode(
            id_claims | self.id_token_claims,
            self.id_token_key,
            algorithm="RS256",
            headers={"kid": KEY_ID},
        )
        return httpx.Response(
            200,
            headers={"Cache-Control": "no-store"},
            json={
                "access_token": self.access_token(grant.user),
                "token_type": "Bearer",
                "expires_in": self.expires_in,
                "refresh_token": refresh_token,
                "id_token": id_token,
            },
        )


def idp_app(idp: FakeIdP, user: str, post_logout_redirects: tuple[str, ...]) -> ASGIApp:
    """The provider over HTTP. Whoever reaches the authorization endpoint is ``user``, signed in
    without a form, since the UI never sees the provider's login page anyway."""

    async def authorize(request: Request) -> Response:
        code = idp.authorize(str(request.url), user)
        state = request.query_params["state"]
        query = urlencode({"code": code, "state": state})
        return RedirectResponse(f"{request.query_params['redirect_uri']}?{query}", 303)

    async def end_session(request: Request) -> Response:
        target = request.query_params.get("post_logout_redirect_uri", "")
        if request.query_params.get("client_id") != idp.client_id:
            return PlainTextResponse("unknown client", 400)
        if target not in post_logout_redirects:
            return PlainTextResponse("post_logout_redirect_uri is not registered", 400)
        return RedirectResponse(target, 303)

    async def backchannel(request: Request) -> Response:
        answer = idp.handle(
            httpx.Request(
                request.method,
                str(request.url),
                headers=request.headers.raw,
                content=await request.body(),
            )
        )
        return Response(answer.content, answer.status_code, dict(answer.headers))

    prefix = urlsplit(idp.issuer).path
    return Starlette(
        routes=[
            Route(f"{prefix}{AUTHORIZE_PATH}", authorize, methods=["GET"]),
            Route(f"{prefix}{END_SESSION_PATH}", end_session, methods=["GET"]),
            Route(f"{prefix}/{{path:path}}", backchannel, methods=["GET", "POST"]),
        ]
    )
