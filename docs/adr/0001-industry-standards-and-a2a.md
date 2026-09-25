# 1. Industry standards at every boundary; A2A as the only entry

## Status

Accepted, 2026-09-24

## Context

The main argument for building our own runtime rather than adopting another platform's is that
the executor stays replaceable behind the agent catalog contract. Replacement is cheap only when
the interfaces on both sides mean the same thing; otherwise an isolating layer is needed
(*POSA Vol. 1*, p. 49). A contract of our own does not give that: no other agent platform
implements it. An open standard does.

Agents need two different kinds of connection: to their tools and data, and to other agents.
MCP connects an agent to its tools; A2A connects one agent to other agents, and the two are
complementary rather than competing (*The MCP Standard*, PDF p. 43).

A group of n participants has n(n-1)/2 communication links; coordinating through
representatives cuts them (from 21 to 10 in the book's example, which is about teams of people),
and a stable interface spares the others from knowing the implementation (*Machine Learning in
Production*, PDF p. 558). The same arithmetic applies to agents calling agents.

## Decision

Every boundary between Golem and the outside world uses an industry standard. Custom formats
are allowed only where no standard exists, and those places are listed explicitly.

| Boundary | Standard |
| --- | --- |
| Entry into the platform; agent to agent | A2A 1.0 |
| Agent to tools and data | MCP, specification 2026-07-28 |
| Agent and skill description | Agent Skills (`SKILL.md`) |
| Agent card for others | A2A Agent Card, generated from the catalog, never hand-written |
| Human and service login | OpenID Connect, OAuth 2.0 (Keycloak) |
| Identity along the call chain | OAuth 2.0 Token Exchange, RFC 8693, `act` claim; fallback: a signed run token |
| Traces | OpenTelemetry GenAI semantic conventions, W3C Trace Context |
| Model calls | OpenAI-compatible API through a LiteLLM gateway |
| Execution | Kubernetes Job, NetworkPolicy |

A2A is the only way into the platform. The UI, webhooks, the chat bot, the catalog CI pipeline
and agents of other platforms all enter the same way. The orchestrator has no launch API of its
own.

An A2A task is the run. Task states map onto run states (`SUBMITTED`/`WORKING`: Job running;
`INPUT_REQUIRED`: waiting for a human; `COMPLETED`/`FAILED`/`CANCELED`: terminal with a reason;
`REJECTED`: out of scope or over a chain limit). The result (branch, merge request, trace link,
cost) is a task artifact. Long runs answer with a push notification instead of holding a stream.

A Job does not host an A2A server and stays ephemeral. Inside the Job, "ask an agent" is a thin
A2A client that the network policy lets reach only the A2A edge.

Where no standard exists, Golem defines its own and says so:

- the audit log format, set by the organization's security requirements;
- the chain policy: who may call whom, depth, no cycles, budget per root run;
- the process file with stages and gates;
- the agent catalog schema on top of `SKILL.md`: roles, path permissions, model per data class.

Rules for applying standards:

- Standards apply at boundaries between systems, not inside a Job.
- Versions are pinned: A2A 1.0, not 0.3 (state and method names differ); MCP 2026-07-28.
  Where a library lags a pinned version, the gap is written down, not hidden: the MCP Python
  SDK that `langchain-mcp-adapters` allows (`mcp<2`, 1.30 in `uv.lock`) negotiates at most
  protocol 2025-11-25, so clients and servers speak that until the adapters accept `mcp` 2.x
  (note of 2026-09-25).
- For a standard in Development status (OpenTelemetry GenAI), take the shape but do not treat
  attribute names as a frozen contract.
- Where the organization requires more than a standard, the requirement sits on top of the
  standard, not instead of it: the audit log sits on top of traces.

## Consequences

- Any agent platform that speaks A2A can call Golem agents and be called by them, without an
  adapter.
- Human-in-the-loop confirmation goes through an A2A extension rather than a custom API, so the
  gate works the same from a merge request, a chat bot or another platform's A2A client
  (target, see ADR 0005).
- Keycloak delegation for RFC 8693 is a preview feature with open defects. Until it is proven,
  the chain travels in a signed run token and the standard is adopted later.
- Traces go through OpenTelemetry, so the platform is not tied to one trace store's SDK.
- `a2a-sdk` must be available with A2A 1.0 support in the organization's package mirror.
