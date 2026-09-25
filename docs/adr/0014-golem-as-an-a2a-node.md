# 14. Golem as a node of agent-to-agent traffic

## Status

Accepted, 2026-09-25

## Context

Today Golem speaks A2A only as a server. People and services start runs of Golem's own agents
through the edge (ADR 0001); the edge authenticates, applies the chain policy, rate-limits and
audits every call (ADR 0002, 0012). Nothing in Golem calls an agent: no role can ask another
agent for work, so the chain in the edge's policy is always empty and its checks (registry of
who may call whom, depth, cycles) never meet a real chain. Nothing lists the agents either:
cards are served one at a time, by name.

Three things would make Golem matter to agent-to-agent traffic beyond its own runs:

- **A control point.** The organization's security requirements ask the platform for a registry
  of which agent may call which, a bound on the agents taking part in one request, the chain of
  agents returned with the answer, and the subject on whose behalf an agent acts. The edge
  already enforces most of that for its own callers. "Perhaps the most important part of API
  management is that it can offer a central point to discover APIs, while you continue to make
  changes behind the scenes" (*Mastering API Architecture*, с. 216 (PDF 254)). A group of n
  participants has n(n-1)/2 communication links; coordinating through a representative cuts
  them (*Machine Learning in Production* (Kästner), PDF 558).
- **A directory.** Whoever lists the agents is where callers look first.
- **A distinct capability.** Golem's agents do long work whose result is a reviewed change in a
  context repository, delivered as a merge request. MCP connects an agent to its tools, A2A to
  other agents, and the two are complementary (*The MCP Standard*, PDF 43); other platforms'
  agents can use Golem's agents as a service over A2A.

Two cautions hold for every step. A central point is a single point of failure and "the more
functionality you are relying on within the gateway, the bigger the risk involved"
(*Mastering API Architecture*, с. 79 (PDF 117)); and "if logic has a place where it can be
centralized, it will become centralized!" (*Building Microservices*, 2nd ed., PDF 297). Each
hop also multiplies failure points: "The more complex a task an agent performs, the more
possible failure points there are" (*AI Engineering*, с. 298 (PDF 322)).

## Decision

Golem becomes an A2A node in four steps, the first two inside Golem, the last two with partners.

### 1. Delegation between Golem's own agents (pilot)

A role may **delegate** work to another agent through a runtime tool, `delegate_to_agent(agent,
goal)`. The call goes out of the Job to the edge like any other A2A client call; the Job's
NetworkPolicy already admits the edge.

- **No waiting in the pilot.** Golem's runs take minutes to hours and answer with a merge
  request, so a role does not wait for the child's result inside its own run. The tool returns
  the child task's id at once, and the role records it (for example in the record it writes), so
  the parent's merge request shows what was delegated. The child run proposes its own merge
  request. Waiting for a child's result and continuing afterwards is durable orchestration and
  stays in the target picture with Temporal (ADR 0005).
- **A call token per run.** The Job presents a second Golem-issued token to the edge, separate
  from the run token for MCP servers (audience `golem-a2a`, not `golem-mcp`; a token for one
  audience is never accepted by the other, per ADR 0007). Its claims follow the shape of OAuth
  2.0 Token Exchange (RFC 8693): `sub` is the subject on whose behalf the chain acts (the human
  or service that started the root run), `act` names the acting agent, and the token carries
  the chain of agents so far and the root run id. The edge verifies it against the
  orchestrator's keys like it verifies the identity provider's tokens.
- **The chain policy becomes real.** The edge evaluates `Call(caller=agent:<name>, callee,
  chain)` against the call registry, depth and cycles, audits the decision with the whole chain,
  rate-limits the acting agent, and forwards the subject and the chain to the task service.
- **One budget per chain.** The child run is admitted with the root run id from the token, so
  the chain's concurrency and budget limits (ADR 0004's admission) cover every run a request
  fans out into.
- **The chain comes back.** The child task's metadata carries the chain, so whoever reads the
  task sees which agents took part.

### 2. A directory of agents (pilot)

The edge serves `GET /agents`: the agents a caller may call, each with its public card URL.
Unauthenticated callers get the public list; an authenticated caller gets the list narrowed by
the call registry to the agents it may call. Cards are signed (A2A 1.0 Agent Card signatures,
JWS) with a Golem key published next to the run-token keys, so a caller outside Golem can check
that a card was issued by this platform.

### 3. Outbound calls to other platforms (with a partner)

The edge's outbound client (ADR 0002's target) calls agents of other platforms listed in the
call registry, with a timeout, a circuit breaker per platform and a quota per platform, and
audits the call like an inbound one. It starts with one partner platform and one agent.

### 4. Golem as the gateway for other platforms' traffic (a decision for others)

Routing other platforms' agent-to-agent calls through Golem's edge would give the security
partner one place to see and govern that traffic. That is an organizational decision, not
Golem's; it needs the partners' agreement and the security partner's.

## Consequences

- Delegation proves the chain machinery (identity, policy, audit, budget) on Golem's own agents
  before any partner depends on it.
- A delegated child runs as the subject, not as the delegating agent, so it can never do more
  than the person who started the chain was allowed to start; the acting agent is recorded, not
  trusted with its own rights.
- The token is Golem's own, shaped after RFC 8693, not an identity-provider token exchange:
  delegation with an `act` claim is still a preview feature with open defects in the identity
  provider the organization uses. Moving to the provider's exchange later changes the issuer,
  not the claims the edge checks.
- Without waiting, a parent cannot use the child's result in the same run; people see both
  proposals and decide. A role that needs a result before continuing waits for Temporal.
- The edge grows: token verification for a second issuer, the directory, later the outbound
  client. Each stays small and stateless, and the edge keeps failing closed.
- Steps 3 and 4 depend on other platforms supporting A2A 1.0, which has not been confirmed.
