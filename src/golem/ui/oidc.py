"""The UI as an OpenID Connect relying party: authorization code flow with PKCE, a
confidential client.

Sections cited are of OpenID Connect Core 1.0 ("Core"), OpenID Connect Discovery 1.0,
OpenID Connect RP-Initiated Logout 1.0, RFC 6749 (OAuth 2.0) and RFC 7636 (PKCE).
"""

import asyncio
import base64
import hashlib
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import partial
from typing import Any
from urllib.parse import quote, urlencode, urlsplit

import httpx
import jwt
from jwt.types import Options

from golem.jwks import SigningKeys, fetch_jwks, key_id_of

SCOPE = "openid profile"
ID_TOKEN_ALGORITHMS = ("RS256", "ES256")
# Without expires_in (RECOMMENDED in RFC 6749, 5.1) the token is refreshed within a minute.
DEFAULT_EXPIRES_IN = 60
CLOCK_SKEW_SECONDS = 30
LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


class OidcError(Exception):
    pass


@dataclass(frozen=True)
class Discovery:
    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    jwks_uri: str
    end_session_endpoint: str | None


@dataclass(frozen=True)
class Tokens:
    access_token: str = field(repr=False)
    expires_in: int
    refresh_token: str | None = field(repr=False)
    id_token: str | None = field(repr=False)


def s256(verifier: str) -> str:
    """RFC 7636, 4.2: BASE64URL-ENCODE(SHA256(ASCII(code_verifier)))."""
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def new_verifier() -> str:
    # RFC 7636, 4.1: 43 to 128 unreserved characters; 32 random bytes give 43.
    return secrets.token_urlsafe(32)


def parse_discovery(data: Any, issuer: str) -> Discovery:
    if not isinstance(data, dict):
        raise OidcError("discovery document is not a JSON object")
    # Discovery 4.3: the issuer returned must be identical to the one discovery was for.
    if data.get("issuer") != issuer:
        raise OidcError(f"discovery names issuer {data.get('issuer')!r}, expected {issuer!r}")
    required = ("authorization_endpoint", "token_endpoint", "jwks_uri")
    missing = [name for name in required if not _https_or_local(data.get(name))]
    if missing:
        raise OidcError(f"discovery lacks usable {', '.join(missing)}")
    methods = data.get("code_challenge_methods_supported")
    if isinstance(methods, list) and "S256" not in methods:
        raise OidcError("the identity provider does not support PKCE S256")
    end_session = data.get("end_session_endpoint")
    return Discovery(
        issuer=issuer,
        authorization_endpoint=data["authorization_endpoint"],
        token_endpoint=data["token_endpoint"],
        jwks_uri=data["jwks_uri"],
        end_session_endpoint=end_session if _https_or_local(end_session) else None,
    )


def authorization_url(
    discovery: Discovery,
    *,
    client_id: str,
    redirect_uri: str,
    state: str,
    nonce: str,
    code_challenge: str,
) -> str:
    """Core 3.1.2.1 with RFC 7636, 4.3."""
    query = urlencode(
        {
            "response_type": "code",
            "scope": SCOPE,
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "state": state,
            "nonce": nonce,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }
    )
    return f"{discovery.authorization_endpoint}?{query}"


def end_session_url(
    discovery: Discovery, *, id_token: str | None, client_id: str, post_logout_redirect_uri: str
) -> str | None:
    """RP-Initiated Logout 1.0, section 2; None when the provider offers no endpoint."""
    if discovery.end_session_endpoint is None:
        return None
    params = {"client_id": client_id, "post_logout_redirect_uri": post_logout_redirect_uri}
    if id_token:
        params["id_token_hint"] = id_token
    return f"{discovery.end_session_endpoint}?{urlencode(params)}"


def client_secret_basic(client_id: str, client_secret: str) -> str:
    """RFC 6749, 2.3.1: the id and secret are form-urlencoded before Base64."""
    pair = f"{quote(client_id, safe='')}:{quote(client_secret, safe='')}"
    return "Basic " + base64.b64encode(pair.encode()).decode("ascii")


def parse_token_response(response: httpx.Response) -> Tokens:
    """RFC 6749, 5.1 and 5.2; Core 3.1.3.3."""
    try:
        body = response.json()
    except ValueError as error:
        raise OidcError(f"token endpoint answered {response.status_code} without JSON") from error
    if not isinstance(body, dict):
        raise OidcError("token response is not a JSON object")
    if response.status_code != 200:
        raise OidcError(f"token endpoint refused: {body.get('error', response.status_code)}")
    access_token = body.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise OidcError("token response has no access_token")
    if str(body.get("token_type", "")).lower() != "bearer":
        raise OidcError(f"token_type is {body.get('token_type')!r}, not Bearer")
    expires_in = body.get("expires_in", DEFAULT_EXPIRES_IN)
    if isinstance(expires_in, bool) or not isinstance(expires_in, int) or expires_in <= 0:
        raise OidcError(f"expires_in is not a positive integer: {expires_in!r}")
    refresh_token = body.get("refresh_token")
    id_token = body.get("id_token")
    return Tokens(
        access_token=access_token,
        expires_in=expires_in,
        refresh_token=refresh_token if isinstance(refresh_token, str) and refresh_token else None,
        id_token=id_token if isinstance(id_token, str) and id_token else None,
    )


def verify_id_token(
    token: str,
    *,
    keys: jwt.PyJWKSet,
    issuer: str,
    client_id: str,
    nonce: str | None,
    now: float,
) -> dict[str, Any]:
    """Core 3.1.3.7: issuer, audience, azp, signature and algorithm, expiry, nonce."""
    try:
        kid = jwt.get_unverified_header(token).get("kid")
        if not isinstance(kid, str):
            raise KeyError(kid)
        key = keys[kid]
    except (jwt.DecodeError, KeyError) as error:
        raise OidcError("ID token is malformed or names an unknown key") from error
    if key.algorithm_name not in ID_TOKEN_ALGORITHMS:
        raise OidcError(f"ID token algorithm {key.algorithm_name!r} is not allowed")
    # Time claims are checked below against the injectable clock; PyJWT reads the wall clock.
    options: Options = {
        "require": ["iss", "aud", "sub", "exp", "iat"],
        "verify_exp": False,
        "verify_iat": False,
        "verify_nbf": False,
    }
    try:
        claims = jwt.decode(
            token,
            key,
            algorithms=[key.algorithm_name],
            issuer=issuer,
            audience=client_id,
            options=options,
        )
    except jwt.InvalidTokenError as error:
        raise OidcError(f"ID token refused: {error}") from error
    audiences = claims["aud"] if isinstance(claims["aud"], list) else [claims["aud"]]
    azp = claims.get("azp")
    if (len(audiences) > 1 or azp is not None) and azp != client_id:
        raise OidcError("ID token is authorized for another party (azp)")
    if not isinstance(claims["exp"], int | float) or claims["exp"] <= now - CLOCK_SKEW_SECONDS:
        raise OidcError("ID token expired")
    if nonce is not None and not secrets.compare_digest(str(claims.get("nonce", "")), nonce):
        raise OidcError("ID token nonce does not match the authentication request")
    if not isinstance(claims["sub"], str) or not claims["sub"]:
        raise OidcError("ID token has no subject")
    return claims


class OidcClient:
    """Discovery, the token endpoint and ID token keys of one identity provider.

    Discovery is fetched on first use and kept, so the UI starts while the provider is down.
    Keys come from golem.jwks: refetched on an unknown key id, rate limited.
    """

    def __init__(
        self,
        *,
        http: httpx.AsyncClient,
        keys_http: httpx.Client,
        issuer: str,
        discovery_url: str,
        client_id: str,
        client_secret: str,
        redirect_url: str,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._http = http
        self._keys_http = keys_http
        self.issuer = issuer
        self._discovery_url = discovery_url
        self.client_id = client_id
        self._authorization = client_secret_basic(client_id, client_secret)
        self.redirect_url = redirect_url
        self._clock = clock
        self._discovery: Discovery | None = None
        self._keys: SigningKeys | None = None
        self._lock = asyncio.Lock()

    async def discovery(self) -> Discovery:
        async with self._lock:
            if self._discovery is None:
                try:
                    response = await self._http.get(self._discovery_url)
                    response.raise_for_status()
                    data = response.json()
                except (httpx.HTTPError, ValueError) as error:
                    raise OidcError(f"discovery failed: {error}") from error
                discovery = parse_discovery(data, self.issuer)
                self._keys = SigningKeys(partial(fetch_jwks, self._keys_http, discovery.jwks_uri))
                self._discovery = discovery
            return self._discovery

    async def exchange(self, code: str, verifier: str) -> Tokens:
        """Core 3.1.3.1, RFC 6749 4.1.3, RFC 7636 4.5."""
        return await self._token(
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": self.redirect_url,
                "code_verifier": verifier,
            }
        )

    async def refresh(self, refresh_token: str) -> Tokens:
        """RFC 6749, 6; Core 12.1."""
        return await self._token({"grant_type": "refresh_token", "refresh_token": refresh_token})

    async def verify(self, id_token: str, *, nonce: str | None) -> dict[str, Any]:
        await self.discovery()
        keys = self._keys
        if keys is None:
            raise OidcError("the identity provider's signing keys are unavailable")
        # A refetch of the keys is a blocking request; it must not stall the event loop.
        current = await asyncio.to_thread(keys.for_key_id, key_id_of(id_token))
        if current is None:
            raise OidcError("the identity provider's signing keys are unavailable")
        return verify_id_token(
            id_token,
            keys=current,
            issuer=self.issuer,
            client_id=self.client_id,
            nonce=nonce,
            now=self._clock(),
        )

    async def _token(self, form: dict[str, str]) -> Tokens:
        discovery = await self.discovery()
        try:
            response = await self._http.post(
                discovery.token_endpoint,
                data=form,
                headers={"Authorization": self._authorization, "Accept": "application/json"},
            )
        except httpx.HTTPError as error:
            raise OidcError(f"token endpoint unreachable: {error}") from error
        return parse_token_response(response)


def _https_or_local(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    url = urlsplit(value)
    local = url.scheme == "http" and url.hostname in LOCAL_HOSTS
    return bool(url.hostname) and (url.scheme == "https" or local)
