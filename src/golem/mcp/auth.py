"""Who may call a platform MCP server: a verified run token, its grant, and a running run.

A run token is the only credential accepted (audience ``golem-mcp``, ASVS 10.3.1); the decision
rests on its claims (ASVS 10.3.2): the token must grant this server's tool group, and the run it
names must still be running, which the task service answers and this server caches briefly.
Anything that cannot be checked is refused.
"""

import time
from collections.abc import Callable
from dataclasses import dataclass

from golem.jwks import SigningKeys, key_id_of
from golem.mcp.groups import Group
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
