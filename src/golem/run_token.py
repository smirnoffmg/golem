"""Run tokens: what a Job presents to platform MCP servers.

The orchestrator issues one per run, signed with its key (ES256 on P-256). A token names the run,
the agent, the caller on whose behalf it runs, the root of the call chain and the tool groups the
role may use, and it expires with the run's deadline. MCP servers verify it against the
orchestrator's public JWKS; the audience keeps it from being usable anywhere else.
"""

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

ISSUER = "golem"
AUDIENCE = "golem-mcp"
ALGORITHM = "ES256"


@dataclass(frozen=True)
class RunClaims:
    run_id: str
    agent: str
    caller: str
    root_run_id: str
    tools: tuple[str, ...]
    expires_at: int


@dataclass(frozen=True)
class RunTokenError:
    reason: str


@dataclass(frozen=True)
class SigningKey:
    kid: str
    private_pem: str = field(repr=False)

    @classmethod
    def generate(cls, kid: str) -> "SigningKey":
        key = ec.generate_private_key(ec.SECP256R1())
        return cls(kid=kid, private_pem=_pem(key))

    @classmethod
    def from_pem(cls, pem: str, kid: str) -> "SigningKey":
        key = serialization.load_pem_private_key(pem.encode(), password=None)
        if not isinstance(key, ec.EllipticCurvePrivateKey) or key.curve.name != "secp256r1":
            raise ValueError("run tokens are signed with ES256: the key must be EC P-256")
        return cls(kid=kid, private_pem=pem)


def _pem(key: ec.EllipticCurvePrivateKey) -> str:
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()


def issue(claims: RunClaims, key: SigningKey, now: int) -> str:
    payload = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sub": claims.run_id,
        "iat": now,
        "exp": claims.expires_at,
        "agent": claims.agent,
        "caller": claims.caller,
        "root": claims.root_run_id,
        "tools": list(claims.tools),
    }
    return jwt.encode(payload, key.private_pem, algorithm=ALGORITHM, headers={"kid": key.kid})


def public_jwks(keys: Iterable[SigningKey]) -> dict[str, Any]:
    entries = []
    for key in keys:
        public = serialization.load_pem_private_key(key.private_pem.encode(), None).public_key()
        jwk = jwt.algorithms.ECAlgorithm.to_jwk(public, as_dict=True)
        entries.append({**jwk, "kid": key.kid, "alg": ALGORITHM, "use": "sig"})
    return {"keys": entries}


def verify(token: str, keys: jwt.PyJWKSet, now: int) -> RunClaims | RunTokenError:
    try:
        header = jwt.get_unverified_header(token)
    except jwt.PyJWTError as error:
        return RunTokenError(f"malformed token: {error}")
    # Decided from the header before any key lookup, so "none" and HS* never reach a key.
    if header.get("alg") != ALGORITHM:
        return RunTokenError(f"algorithm {header.get('alg')!r} is not {ALGORITHM}")
    try:
        key = keys[header.get("kid", "")]
    except KeyError:
        return RunTokenError("unknown signing key")
    try:
        payload = jwt.decode(
            token,
            key.key,
            algorithms=[ALGORITHM],
            audience=AUDIENCE,
            issuer=ISSUER,
            options={
                "require": ["exp", "sub", "aud", "iss"],
                # Time is checked below against the injected clock, not the wall clock.
                "verify_exp": False,
                "verify_iat": False,
                "verify_nbf": False,
            },
        )
    except jwt.PyJWTError as error:
        return RunTokenError(f"invalid token: {error}")
    if payload["exp"] <= now:
        return RunTokenError("token expired")
    return _claims(payload)


def _claims(payload: dict[str, Any]) -> RunClaims | RunTokenError:
    tools = payload.get("tools")
    fields = (payload.get("agent"), payload.get("caller"), payload.get("root"))
    if not all(isinstance(value, str) and value for value in fields):
        return RunTokenError("token lacks agent, caller or root")
    if not isinstance(tools, list) or not all(isinstance(t, str) for t in tools):
        return RunTokenError("token tools must be a list of names")
    return RunClaims(
        run_id=payload["sub"],
        agent=payload["agent"],
        caller=payload["caller"],
        root_run_id=payload["root"],
        tools=tuple(tools),
        expires_at=payload["exp"],
    )
