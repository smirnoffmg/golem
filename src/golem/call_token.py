"""Call tokens: what a Job presents to the edge when its role delegates to another agent.

The orchestrator issues one per run next to the run token, signed with the same key, for
another audience (``golem-a2a``), so neither token is accepted where the other is (ADR 0007,
ADR 0014). The claims follow OAuth 2.0 Token Exchange (RFC 8693, 4.1): ``sub`` is the subject
the whole chain acts for, the human or service that started the root run; ``act`` names the
acting agent. ``chain`` lists the agents so far, the acting one last; ``root`` and ``run`` name
the chain's root run and the run that holds the token.
"""

import re
from dataclasses import dataclass
from typing import Any

import jwt

from golem.run_token import ALGORITHM, ISSUER, RunTokenError, SigningKey, decode

AUDIENCE = "golem-a2a"
AGENT_PREFIX = "agent:"
# A chain acts for a person or a service, never for an agent: the subject's rights bound it.
SUBJECT = re.compile(r"^(user|service):.+$")


@dataclass(frozen=True)
class CallClaims:
    subject: str
    agent: str
    chain: tuple[str, ...]
    root_run_id: str
    run_id: str
    expires_at: int


def issue(claims: CallClaims, key: SigningKey, now: int) -> str:
    payload = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sub": claims.subject,
        "act": {"sub": f"{AGENT_PREFIX}{claims.agent}"},
        "chain": list(claims.chain),
        "root": claims.root_run_id,
        "run": claims.run_id,
        "iat": now,
        "exp": claims.expires_at,
    }
    return jwt.encode(payload, key.private_pem, algorithm=ALGORITHM, headers={"kid": key.kid})


def verify(token: str, keys: jwt.PyJWKSet, now: int) -> CallClaims | RunTokenError:
    payload = decode(token, keys, AUDIENCE, now)
    return payload if isinstance(payload, RunTokenError) else _claims(payload)


def _claims(payload: dict[str, Any]) -> CallClaims | RunTokenError:
    subject = payload["sub"]
    if not isinstance(subject, str) or not SUBJECT.match(subject):
        return RunTokenError("token subject must be a user or a service")
    agent = _actor(payload.get("act"))
    if agent is None:
        return RunTokenError("token act must name the acting agent")
    chain = payload.get("chain")
    if not isinstance(chain, list) or not all(isinstance(a, str) and a for a in chain):
        return RunTokenError("token chain must be a list of agent names")
    if not chain or chain[-1] != agent:
        return RunTokenError("token chain must end with the acting agent")
    root, run = payload.get("root"), payload.get("run")
    if not (isinstance(root, str) and root and isinstance(run, str) and run):
        return RunTokenError("token lacks root or run")
    return CallClaims(
        subject=subject,
        agent=agent,
        chain=tuple(chain),
        root_run_id=root,
        run_id=run,
        expires_at=payload["exp"],
    )


def _actor(act: object) -> str | None:
    if not isinstance(act, dict):
        return None
    actor = act.get("sub")
    if not isinstance(actor, str) or not actor.startswith(AGENT_PREFIX):
        return None
    return actor.removeprefix(AGENT_PREFIX) or None
