"""One audit row per request a platform MCP server decides on, written before it is served.

ASVS 5.0 16.3.2 at L3 asks for every authorization decision to be logged, and 16.2.5 allows a
session token in a log only hashed or masked: the row names the tool and its argument values,
bounded, never a result and never the run token, only a short hash of it that ties together
the requests of one token, including tokens that failed verification.

At a write server the row names the person who decided (the token's subject), the platform that
acts for them and the proposal (ADR 0015); a page body or a reply is not logged, only the digest
of the payload that carries it.
"""

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass, field

import psycopg

from golem.edge.audit import AuditEntry, record
from golem.mcp.auth import Refusal
from golem.mcp.groups import Group
from golem.proposal_payload import ProposalError, payload_digest
from golem.proposal_token import ACTOR, ProposalClaims
from golem.run_token import RunClaims

UNAUTHENTICATED = "unauthenticated"
REQUEST_CHARS = 400
VALUE_CHARS = 120
DIGEST_CHARS = 16
CONNECT_TIMEOUT_SECONDS = 2


@dataclass(frozen=True)
class Operation:
    # The JSON-RPC method, or the HTTP method of a request that carries no message.
    method: str
    tool: str | None = None
    arguments: Mapping[str, object] = field(default_factory=dict)
    problem: Refusal | None = None

    @property
    def name(self) -> str:
        return self.tool or self.method


def token_digest(token: str | None) -> str:
    if token is None:
        return "none"
    return "sha256:" + hashlib.sha256(token.encode()).hexdigest()[:DIGEST_CHARS]


def request_text(operation: Operation, token: str | None, acting: str = "") -> str:
    head = operation.method if operation.tool is None else f"{operation.method} {operation.tool}"
    tail = f"{acting} token={token_digest(token)}" if acting else f"token={token_digest(token)}"
    room = REQUEST_CHARS - len(head) - len(tail) - 2
    arguments = " ".join(
        f"{name}={clipped(repr(value), VALUE_CHARS)}" for name, value in operation.arguments.items()
    )
    if not arguments:
        return clipped(f"{head} {tail}", REQUEST_CHARS)
    return f"{head} {clipped(arguments, room)} {tail}"


def clipped(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: max(0, limit - 3)] + "..."


def payload_named(payload: object) -> str:
    if not isinstance(payload, dict):
        return "<not a payload>"
    try:
        return f"sha256:{payload_digest(payload)[:DIGEST_CHARS]}"
    except ProposalError:
        return "<not a payload>"


def without_payload(operation: Operation) -> Operation:
    if "payload" not in operation.arguments:
        return operation
    arguments = {
        name: payload_named(value) if name == "payload" else value
        for name, value in operation.arguments.items()
    }
    return Operation(operation.method, operation.tool, arguments, operation.problem)


def audit_entry(
    *,
    group: Group,
    target_system: str,
    operation: Operation,
    token: str | None,
    claims: RunClaims | ProposalClaims | None,
    refusal: Refusal | None,
    source_ip: str | None,
) -> AuditEntry:
    if group.writes:
        operation = without_payload(operation)
    if isinstance(claims, ProposalClaims):
        account = claims.decider
        acting = f"act={ACTOR} proposal={claims.proposal_id}"
        chain: tuple[str, ...] = ()
    elif isinstance(claims, RunClaims):
        account, acting, chain = claims.caller, "", (claims.root_run_id, claims.run_id)
    else:
        account, acting, chain = UNAUTHENTICATED, "", ()
    return AuditEntry(
        account=account,
        request=request_text(operation, token, acting),
        target_system=target_system,
        operation=clipped(operation.name, VALUE_CHARS),
        result="allow" if refusal is None else f"deny: {refusal.reason}",
        source=f"mcp:{group.name}",
        source_ip=source_ip,
        chain=chain,
    )


async def written(dsn: str, entry: AuditEntry) -> bool:
    try:
        async with await psycopg.AsyncConnection.connect(
            dsn, autocommit=True, connect_timeout=CONNECT_TIMEOUT_SECONDS
        ) as conn:
            await record(conn, entry)
    except (psycopg.Error, OSError):
        return False
    return True
