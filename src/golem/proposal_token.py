"""Proposal tokens: what the task service presents to a write server (ADR 0015).

One per call, signed with the run-token key and verified against the same JWKS. The subject is
the person who decided, the actor the task service; the token names one proposal, one write
group and the digest of the payload the person saw, and lives two minutes. A write server
refuses anything else, and a read server refuses any token that carries ``proposal``.
"""

import re
import uuid
from dataclasses import dataclass
from typing import Any

import jwt

from golem.catalog import REVIEWER
from golem.run_token import ALGORITHM, ISSUER, RunTokenError, SigningKey, decode

ACTOR = "service:golem-tasks"
PREVIEW = "preview"
APPLY = "apply"
SCOPES = frozenset({PREVIEW, APPLY})
TOKEN_SECONDS = 120
DIGEST = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class ProposalClaims:
    # The write server's canonical URI: one token per server, as for run tokens (ADR 0016).
    audience: str
    decider: str
    proposal_id: str
    scope: str
    group: str
    digest: str


def issue(claims: ProposalClaims, key: SigningKey, now: int) -> str:
    payload = {
        "iss": ISSUER,
        "aud": claims.audience,
        "sub": claims.decider,
        "act": {"sub": ACTOR},
        "proposal": claims.proposal_id,
        "scope": claims.scope,
        "tools": [claims.group],
        "digest": claims.digest,
        "iat": now,
        "exp": now + TOKEN_SECONDS,
        "jti": str(uuid.uuid4()),
    }
    return jwt.encode(payload, key.private_pem, algorithm=ALGORITHM, headers={"kid": key.kid})


def verify(
    token: str, keys: jwt.PyJWKSet, audience: str, now: int
) -> ProposalClaims | RunTokenError:
    payload = decode(token, keys, audience, now)
    return payload if isinstance(payload, RunTokenError) else _claims(payload)


def _claims(payload: dict[str, Any]) -> ProposalClaims | RunTokenError:
    proposal, act = payload.get("proposal"), payload.get("act")
    if not isinstance(proposal, str) or not proposal or not isinstance(act, dict):
        return RunTokenError("not a proposal token: it lacks proposal or act")
    if act != {"sub": ACTOR}:
        return RunTokenError(f"only {ACTOR} acts on a proposal")
    subject, scope, tools, digest = (
        payload["sub"],
        payload.get("scope"),
        payload.get("tools"),
        payload.get("digest"),
    )
    if not REVIEWER.fullmatch(subject):
        return RunTokenError("a proposal token's subject is a person")
    if scope not in SCOPES:
        return RunTokenError(f"scope must be {PREVIEW} or {APPLY}")
    if not (isinstance(tools, list) and len(tools) == 1 and isinstance(tools[0], str)):
        return RunTokenError("a proposal token names exactly one write group")
    if not isinstance(digest, str) or not DIGEST.fullmatch(digest):
        return RunTokenError("digest must be a SHA-256 in hex")
    return ProposalClaims(
        audience=str(payload["aud"]),
        decider=subject,
        proposal_id=proposal,
        scope=scope,
        group=tools[0],
        digest=digest,
    )
