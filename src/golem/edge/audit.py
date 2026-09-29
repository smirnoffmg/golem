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
    # A delegated call is the subject's: the account is whom the chain acts for, and the chain
    # ends with the acting agent.
    return AuditEntry(
        account=principal.on_behalf_of,
        request=f"{method} tenant={callee}",
        target_system=f"agent:{callee}",
        operation=method,
        result="allow" if refusal is None else f"deny: {refusal}",
        source=SOURCE,
        source_ip=source_ip,
        chain=principal.chain,
    )


DIRECTORY = "directory"
LIST_AGENTS = "ListAgents"


def directory_entry(
    *,
    principal: Principal,
    listed: tuple[str, ...],
    refusal: str | None,
    source_ip: str | None,
) -> AuditEntry:
    """An authenticated listing: which agents the caller was shown, which is the registry's
    answer to "whom may I call", worth the same record as a call."""
    return AuditEntry(
        account=principal.on_behalf_of,
        request=f"{LIST_AGENTS} agents={','.join(listed)}",
        target_system=DIRECTORY,
        operation=LIST_AGENTS,
        result="allow" if refusal is None else f"deny: {refusal}",
        source=SOURCE,
        source_ip=source_ip,
        chain=principal.chain,
    )


PROCESSES = "processes"
RESOLVE_PROCESS = "ResolveProcess"


def resolution_entry(
    *,
    principal: Principal,
    task_id: str,
    action: str,
    refusal: str | None,
    source_ip: str | None,
) -> AuditEntry:
    """A process owner's answer to a process waiting for a reason (ADR 0019): the task and the
    action, never the reason, which is the person's free text."""
    return AuditEntry(
        account=principal.on_behalf_of,
        request=f"{RESOLVE_PROCESS} task={task_id} action={action}",
        target_system=PROCESSES,
        operation=RESOLVE_PROCESS,
        result="allow" if refusal is None else f"deny: {refusal}",
        source=SOURCE,
        source_ip=source_ip,
        chain=principal.chain,
    )


PROPOSALS = "proposals"
REPORTS = "reports"


def decision_entry(
    *,
    principal: Principal,
    operation: str,
    target_system: str,
    request: str,
    refusal: str | None,
    source_ip: str | None,
) -> AuditEntry:
    """A person reading or deciding proposals, or reading reports (ADR 0015, ADR 0018): what
    they asked for, never a payload or a reason, which are free text."""
    return AuditEntry(
        account=principal.on_behalf_of,
        request=f"{operation} {request}".strip(),
        target_system=target_system,
        operation=operation,
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
