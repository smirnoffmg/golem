from typing import Any

import psycopg
import pytest

from golem.edge.audit import AuditEntry, audit_entry, directory_entry, record, source_ip_of
from golem.edge.auth import Principal

ALICE = Principal(name="user:alice", chain=())


def test_allowed_call_entry_names_caller_agent_and_method() -> None:
    entry = audit_entry(
        principal=ALICE,
        callee="reviewer",
        method="SendMessage",
        refusal=None,
        source_ip="10.0.0.7",
    )

    assert entry == AuditEntry(
        account="user:alice",
        request="SendMessage tenant=reviewer",
        target_system="agent:reviewer",
        operation="SendMessage",
        result="allow",
        source="a2a-edge",
        source_ip="10.0.0.7",
        chain=(),
    )


def test_refused_call_entry_carries_the_reason_and_chain() -> None:
    entry = audit_entry(
        principal=Principal(name="agent:planner", chain=("root", "planner")),
        callee="ghost",
        method="GetTask",
        refusal="unknown_agent: agent 'ghost' is not registered",
        source_ip=None,
    )

    assert entry.result == "deny: unknown_agent: agent 'ghost' is not registered"
    assert entry.chain == ("root", "planner")
    assert entry.source_ip is None


def test_a_directory_listing_entry_names_what_the_caller_was_shown() -> None:
    entry = directory_entry(
        principal=Principal(
            name="agent:discovery", chain=("discovery",), subject="user:alice", run_id="run-a"
        ),
        listed=("evaluator", "reviewer"),
        refusal=None,
        source_ip="10.0.0.7",
    )

    assert entry == AuditEntry(
        account="user:alice",
        request="ListAgents agents=evaluator,reviewer",
        target_system="directory",
        operation="ListAgents",
        result="allow",
        source="a2a-edge",
        source_ip="10.0.0.7",
        chain=("discovery",),
    )


def test_a_refused_directory_listing_entry_carries_the_reason() -> None:
    entry = directory_entry(principal=ALICE, listed=(), refusal="rate_limited", source_ip=None)

    assert (entry.request, entry.result) == ("ListAgents agents=", "deny: rate_limited")


@pytest.mark.parametrize(
    ("host", "expected"),
    [("10.0.0.7", "10.0.0.7"), ("::1", "::1"), ("testclient", None), (None, None)],
)
def test_source_ip_keeps_only_valid_addresses(host: str | None, expected: str | None) -> None:
    assert source_ip_of(host) == expected


async def rows(admin_dsn: str) -> list[tuple[Any, ...]]:
    async with await psycopg.AsyncConnection.connect(admin_dsn) as conn:
        cursor = await conn.execute(
            "SELECT account, request, target_system, operation, result, rows_affected,"
            " source, host(source_ip), chain FROM audit_log ORDER BY id"
        )
        return await cursor.fetchall()


async def test_record_inserts_as_the_edge_role(audit_dsn: str, audit_admin_dsn: str) -> None:
    entry = audit_entry(
        principal=Principal(name="agent:planner", chain=("root", "planner")),
        callee="reviewer",
        method="SendMessage",
        refusal="not_allowed: nope",
        source_ip="10.0.0.7",
    )

    async with await psycopg.AsyncConnection.connect(audit_dsn, autocommit=True) as conn:
        await record(conn, entry)

    assert await rows(audit_admin_dsn) == [
        (
            "agent:planner",
            "SendMessage tenant=reviewer",
            "agent:reviewer",
            "SendMessage",
            "deny: not_allowed: nope",
            None,
            "a2a-edge",
            "10.0.0.7",
            ["root", "planner"],
        )
    ]


async def test_record_without_source_ip_or_chain(audit_dsn: str, audit_admin_dsn: str) -> None:
    entry = audit_entry(
        principal=ALICE, callee="reviewer", method="GetTask", refusal=None, source_ip=None
    )

    async with await psycopg.AsyncConnection.connect(audit_dsn, autocommit=True) as conn:
        await record(conn, entry)

    [row] = await rows(audit_admin_dsn)
    assert (row[7], row[8]) == (None, [])


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE audit_log SET result = 'allow'",
        "DELETE FROM audit_log",
        "TRUNCATE audit_log",
        "SELECT * FROM audit_log",
    ],
)
async def test_edge_role_may_only_insert(
    audit_dsn: str, audit_admin_dsn: str, statement: str
) -> None:
    async with await psycopg.AsyncConnection.connect(audit_dsn, autocommit=True) as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            await conn.execute(statement)
