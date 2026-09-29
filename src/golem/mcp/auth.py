"""Who may call a platform MCP server.

A read server accepts a run token only (audience ``golem-mcp``, ASVS 10.3.1); the decision
rests on its claims (ASVS 10.3.2): the token must grant this server's tool group, and the run it
names must still be running, which the task service answers and this server caches briefly.

A write server accepts a proposal token only (ADR 0015), for its own audience: the token names
one write group, one proposal and the digest of the payload the person saw. The proposal must
still allow the token's scope (``accepted`` to apply, ``pending`` or ``failed`` to preview), and
a call must name that proposal and carry that payload. Anything that cannot be checked is
refused.
"""

import time
from collections.abc import Callable
from dataclasses import dataclass

from golem.jwks import SigningKeys, key_id_of
from golem.mcp.groups import Group
from golem.proposal_payload import WRITE_GROUPS
from golem.proposal_status import ALLOWED_STATES, ProposalState
from golem.proposal_token import ProposalClaims
from golem.proposal_token import verify as verify_proposal_token
from golem.run_status import RUNNING, StatusUnavailable
from golem.run_token import RunClaims, RunTokenError, verify


@dataclass(frozen=True)
class Refusal:
    status_code: int
    # RFC 6750 error code for 401 and 403; empty when the request carried no credentials.
    error: str
    reason: str


def run_token_verifier(
    keys: SigningKeys, clock: Callable[[], float] = time.time
) -> Callable[[str], RunClaims | RunTokenError]:
    def check(token: str) -> RunClaims | RunTokenError:
        current = keys.for_key_id(key_id_of(token))
        if current is None:
            return RunTokenError("signing keys unavailable")
        return verify(token, current, int(clock()))

    return check


def grant_refusal(claims: RunClaims, group: Group) -> Refusal | None:
    if group.name in claims.tools:
        return None
    return Refusal(
        403, "insufficient_scope", f"the run token does not grant tool group {group.name!r}"
    )


def status_refusal(status: str | StatusUnavailable) -> Refusal | None:
    if isinstance(status, StatusUnavailable):
        return Refusal(503, "temporarily_unavailable", f"run status unavailable: {status.reason}")
    if status != RUNNING:
        # RFC 6750 names a revoked token invalid_token; a finished run's token is revoked.
        return Refusal(401, "invalid_token", f"the run is {status}")
    return None


def proposal_token_verifier(
    keys: SigningKeys, resource: str, clock: Callable[[], float] = time.time
) -> Callable[[str], ProposalClaims | RunTokenError]:
    def check(token: str) -> ProposalClaims | RunTokenError:
        current = keys.for_key_id(key_id_of(token))
        if current is None:
            return RunTokenError("signing keys unavailable")
        return verify_proposal_token(token, current, resource, int(clock()))

    return check


def proposal_grant_refusal(claims: ProposalClaims, group: Group) -> Refusal | None:
    if claims.group == group.name:
        return None
    return Refusal(
        403, "insufficient_scope", f"the proposal token does not grant tool group {group.name!r}"
    )


def proposal_state_refusal(
    found: ProposalState | StatusUnavailable | None, claims: ProposalClaims
) -> Refusal | None:
    if isinstance(found, StatusUnavailable):
        return Refusal(
            503, "temporarily_unavailable", f"proposal state unavailable: {found.reason}"
        )
    # A proposal that no longer allows the scope revokes the token, as a stopped run does.
    if found is None:
        return Refusal(401, "invalid_token", "the proposal is unknown")
    if found.state not in ALLOWED_STATES[claims.scope]:
        return Refusal(401, "invalid_token", f"the proposal is {found.state}")
    if found.digest != claims.digest:
        return Refusal(401, "invalid_token", "the token was issued for another payload")
    if WRITE_GROUPS.get(found.kind) != claims.group:
        return Refusal(401, "invalid_token", f"the proposal is not a {claims.group} proposal")
    return None
