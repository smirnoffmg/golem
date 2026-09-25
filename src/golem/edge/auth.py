import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import jwt
from jwt.types import Options

from golem import call_token
from golem.run_token import ISSUER as GOLEM_ISSUER
from golem.run_token import RunTokenError

ALLOWED_ALGORITHMS = frozenset({"RS256", "ES256"})
REQUIRED_CLAIMS = ("exp", "iss", "aud")
SERVICE_ACCOUNT_PREFIX = "service-account-"


@dataclass(frozen=True)
class Principal:
    name: str
    chain: tuple[str, ...]
    # Set for an agent holding a call token: the user or service the chain acts for, the
    # chain's root run and the run that holds the token. A person or service acts for itself.
    subject: str = ""
    root_run_id: str = ""
    run_id: str = ""

    @property
    def on_behalf_of(self) -> str:
        return self.subject or self.name


@dataclass(frozen=True)
class AuthFailure:
    reason: str


def authenticate(
    token: str, *, keys: jwt.PyJWKSet, issuer: str, audience: str, now: int | None = None
) -> Principal | AuthFailure:
    key = _signing_key(token, keys)
    if isinstance(key, AuthFailure):
        return key
    claims = _verified_claims(token, key, issuer=issuer, audience=audience)
    if isinstance(claims, AuthFailure):
        return claims
    expiry = _check_validity_window(claims, int(time.time()) if now is None else now)
    if expiry is not None:
        return expiry
    return principal_of(claims)


def authenticate_any(
    token: str,
    *,
    idp: Callable[[str], Principal | AuthFailure],
    golem: Callable[[str], Principal | AuthFailure],
) -> Principal | AuthFailure:
    """Dispatch on the unverified issuer; each branch then verifies issuer, audience and
    signature with its own keys and algorithms, so the claim only picks who checks it."""
    return golem(token) if _issuer_of(token) == GOLEM_ISSUER else idp(token)


def authenticate_call(
    token: str, *, keys: jwt.PyJWKSet, now: int | None = None
) -> Principal | AuthFailure:
    claims = call_token.verify(token, keys, int(time.time()) if now is None else now)
    if isinstance(claims, RunTokenError):
        return AuthFailure(claims.reason)
    return Principal(
        name=f"{call_token.AGENT_PREFIX}{claims.agent}",
        chain=claims.chain,
        subject=claims.subject,
        root_run_id=claims.root_run_id,
        run_id=claims.run_id,
    )


def _issuer_of(token: str) -> object:
    try:
        return jwt.decode(token, options={"verify_signature": False}).get("iss")
    except jwt.PyJWTError:
        return None


def principal_of(claims: dict[str, Any]) -> Principal | AuthFailure:
    username = claims.get("preferred_username")
    if not isinstance(username, str) or not username:
        return AuthFailure("token has no preferred username")
    if not username.startswith(SERVICE_ACCOUNT_PREFIX):
        return Principal(name=f"user:{username}", chain=())
    client = claims.get("azp")
    if not isinstance(client, str) or not client:
        return AuthFailure("service account token has no authorized client (azp)")
    return Principal(name=f"service:{client}", chain=())


def _signing_key(token: str, keys: jwt.PyJWKSet) -> jwt.PyJWK | AuthFailure:
    try:
        header = jwt.get_unverified_header(token)
    except jwt.DecodeError:
        return AuthFailure("malformed token")
    algorithm = header.get("alg")
    if algorithm not in ALLOWED_ALGORITHMS:
        return AuthFailure(f"algorithm {algorithm!r} is not allowed")
    kid = header.get("kid")
    if not isinstance(kid, str):
        return AuthFailure("token has no key id")
    try:
        return keys[kid]
    except KeyError:
        return AuthFailure(f"unknown signing key {kid!r}")


def _verified_claims(
    token: str, key: jwt.PyJWK, *, issuer: str, audience: str
) -> dict[str, Any] | AuthFailure:
    # Time claims are checked by _check_validity_window against an injectable clock;
    # PyJWT always reads the wall clock.
    options: Options = {
        "require": list(REQUIRED_CLAIMS),
        "verify_exp": False,
        "verify_nbf": False,
        "verify_iat": False,
    }
    try:
        return jwt.decode(
            token,
            key,
            algorithms=[key.algorithm_name],
            issuer=issuer,
            audience=audience,
            options=options,
        )
    except jwt.InvalidSignatureError:
        return AuthFailure("bad signature")
    except jwt.InvalidAlgorithmError:
        return AuthFailure("algorithm does not match the signing key")
    except jwt.InvalidIssuerError:
        return AuthFailure("wrong issuer")
    except jwt.InvalidAudienceError:
        return AuthFailure("wrong audience")
    except jwt.MissingRequiredClaimError as error:
        return AuthFailure(f"missing claim {error.claim}")
    except jwt.DecodeError:
        return AuthFailure("malformed token")
    except jwt.InvalidTokenError as error:
        return AuthFailure(f"invalid token: {error}")


def _check_validity_window(claims: dict[str, Any], now: int) -> AuthFailure | None:
    expires = claims["exp"]
    if not _is_number(expires):
        return AuthFailure("malformed exp claim")
    if expires <= now:
        return AuthFailure("token expired")
    not_before = claims.get("nbf", now)
    if not _is_number(not_before):
        return AuthFailure("malformed nbf claim")
    if not_before > now:
        return AuthFailure("token not yet valid")
    return None


def _is_number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)
