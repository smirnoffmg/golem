import ipaddress
from dataclasses import dataclass

import psycopg

from golem.edge.auth import Principal

SOURCE = "a2a-edge"

INSERT = """
INSERT INTO audit_log
    (account, request, target_system, operation, result, source, source_ip, chain)
VALUES (%s, %s, %s, %s, %s, %s, %s::inet, %s::text[])
"""


@dataclass(frozen=True)
class AuditEntry:
    account: str
    request: str
    target_system: str
    operation: str
    result: str
    source: str
    source_ip: str | None
    chain: tuple[str, ...]


def audit_entry(
    *,
    principal: Principal,
    callee: str,
    method: str,
    refusal: str | None,
    source_ip: str | None,
) -> AuditEntry:
    # Only the method and tenant: message bodies may carry credentials or personal data,
    # and the log is insert-only, so nothing written here can ever be masked afterwards.
    return AuditEntry(
        account=principal.name,
        request=f"{method} tenant={callee}",
        target_system=f"agent:{callee}",
        operation=method,
        result="allow" if refusal is None else f"deny: {refusal}",
        source=SOURCE,
        source_ip=source_ip,
        chain=principal.chain,
    )


def source_ip_of(host: str | None) -> str | None:
    if host is None:
        return None
    try:
        return str(ipaddress.ip_address(host))
    except ValueError:
        return None


async def record(conn: psycopg.AsyncConnection, entry: AuditEntry) -> None:
    await conn.execute(
        INSERT,
        (
            entry.account,
            entry.request,
            entry.target_system,
            entry.operation,
            entry.result,
            entry.source,
            entry.source_ip,
            list(entry.chain),
        ),
    )
