# 3. One Postgres cluster with one database per owner

## Status

Accepted, 2026-09-24

## Context

Three services need relational storage: the task service (A2A tasks, including input from agents
of other platforms), the orchestrator (run records, cost, quotas), and the audit log written by
the edge and the platform MCP servers. In the target picture Temporal adds two more databases.

A trust boundary that crosses a data store means data of different trust levels lives in one
place (*Threat Modeling* (Shostack), p. 50). Tasks submitted by external agents should not sit in
the same store, under the same role, as the records that drive runs and the audit trail.

Running one cluster per service multiplies operational cost for a platform that is still a pilot.

## Decision

One Postgres cluster, several databases, exactly one owner each.

| Database | Owner | Other writers |
| --- | --- | --- |
| `golem_tasks` | task service (`golem_tasks`) | none |
| `golem_runs` | orchestrator (`golem_runs`) | none |
| `golem_audit` | `golem_audit_owner`, a `NOLOGIN` role no service uses | `golem_edge`, `golem_mcp`: `INSERT` only |
| `golem_ui` | web UI (`golem_ui`): sessions, tokens encrypted by the UI (ADR 0011) | none |
| `temporal`, `temporal_visibility` | Temporal | none (target only) |

- `CONNECT` is revoked from `PUBLIC` on every database; a role can reach only the database it
  owns or is explicitly granted.
- The audit writers get `CONNECT` and `INSERT` on `audit_log` only; no service role holds
  `UPDATE`, `DELETE`, `TRUNCATE` or even `SELECT` on it.
- Every login role has its own `CONNECTION LIMIT` and `statement_timeout`, so a burst of A2A
  tasks cannot take all connections from the orchestrator.

`deploy/postgres/init.sql` is the executable form of this decision for local development.

## Consequences

- The trust boundary runs between databases rather than through one store.
- Physical backup and point-in-time recovery apply to the whole cluster. A stricter regime for
  one database (for example, data produced by agents) cannot be expressed at cluster level and
  needs an additional logical backup of that database.
- Immutability of `golem_audit` rests on role grants; a cluster administrator can bypass it. If
  the organization's security requirements do not accept that, the log must also be shipped to a
  central log collection system.
- One cluster is one failure domain for all three services.
