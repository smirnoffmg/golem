import time
from dataclasses import dataclass
from typing import Any

import jwt
from jwt.types import Options

ALLOWED_ALGORITHMS = frozenset({"RS256", "ES256"})
REQUIRED_CLAIMS = ("exp", "iss", "aud")
SERVICE_ACCOUNT_PREFIX = "service-account-"


@dataclass(frozen=True)
class Principal:
    name: str
    chain: tuple[str, ...]


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
