"""One audit row per request a platform MCP server decides on, written before it is served.

ASVS 5.0 16.3.2 at L3 asks for every authorization decision to be logged, and 16.2.5 allows a
session token in a log only hashed or masked: the row names the tool and its argument values,
bounded, never a result and never the run token, only a short hash of it that ties together
the requests of one token, including tokens that failed verification.
"""

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass, field

import psycopg

from golem.edge.audit import AuditEntry, record
from golem.mcp.auth import Refusal
from golem.mcp.groups import Group
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


def request_text(operation: Operation, token: str | None) -> str:
    head = operation.method if operation.tool is None else f"{operation.method} {operation.tool}"
    tail = f"token={token_digest(token)}"
    room = REQUEST_CHARS - len(head) - len(tail) - 2
    arguments = " ".join(
        f"{name}={clipped(repr(value), VALUE_CHARS)}" for name, value in operation.arguments.items()
    )
    if not arguments:
        return clipped(f"{head} {tail}", REQUEST_CHARS)
    return f"{head} {clipped(arguments, room)} {tail}"


def clipped(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: max(0, limit - 3)] + "..."


def audit_entry(
    *,
    group: Group,
    target_system: str,
    operation: Operation,
    token: str | None,
    claims: RunClaims | None,
    refusal: Refusal | None,
    source_ip: str | None,
) -> AuditEntry:
    return AuditEntry(
        account=claims.caller if claims else UNAUTHENTICATED,
        request=request_text(operation, token),
        target_system=target_system,
        operation=clipped(operation.name, VALUE_CHARS),
        result="allow" if refusal is None else f"deny: {refusal.reason}",
        source=f"mcp:{group.name}",
        source_ip=source_ip,
        chain=(claims.root_run_id, claims.run_id) if claims else (),
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
