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

### Step 1 as built

- **The call token** (`golem.call_token`) carries `iss` = `golem`, `aud` = `golem-a2a`, `sub`
  = the subject (`user:…` or `service:…`, never an agent), `act` = `{"sub": "agent:<name>"}`
  (RFC 8693, section 4.1: "a JSON object, and members in the JSON object are claims that
  identify the actor"), `chain` (the agents so far, the acting one last), `root`, `run`, `iat`
  and `exp` (the run token's: the deadline plus a minute). The orchestrator signs it with the
  run-token key and delivers it as `GOLEM_CALL_TOKEN` in the run's token Secret; a child's
  chain is its parent's plus its own agent, and its subject is its parent's. Earlier actors are
  in `chain`, not in nested `act` claims, which the policy would have to unwind.
- **The edge dispatches on the unverified issuer**: `golem` goes to the call-token check
  (ES256 only, audience `golem-a2a`, the orchestrator's keys from the task service's
  `internal-read` port, so the edge now reaches port 8001 too, amending ADR 0009's port table),
  anything else to the identity provider's (its algorithms, issuer and audience). A run token
  presented to the edge fails on its audience; `GOLEM_OIDC_ISSUER` may not be `golem`.
- **An agent may only start a new task.** Forwarded as the subject, it would otherwise read,
  list, cancel or write into the subject's tasks; the edge refuses every other method and any
  message naming an existing task (`method_not_allowed`).
- **The chain crosses the edge in two headers**, `X-Golem-Chain` and `X-Golem-Root-Run`, built
  by the edge from the verified token and trusted by the task service only with the edge
  token, like the principal. The child counts against the subject's per-caller concurrency as
  well as against the chain's limits.
- **The tool is a tool group**, `agents.delegate`, entered in the platform's MCP registry with
  the edge's A2A URL and served by the runtime; a role names it in its catalog, and the call
  registry (`agent:<name>`) decides what it reaches.
- **Keys at startup.** A verifier that has never loaded keys (an MCP server or the edge started
  before the task service) retries after 1 s, doubling up to its refresh interval, instead of
  refusing every token for the whole interval; an unknown key id with keys loaded stays rate
  limited.
- **Revoked with its run.** Before it serves a call token, the edge asks the task service
  whether the token's `run` is still running (`GET /internal/runs/{run_id}` on the
  `internal-read` port, set by `GOLEM_TASK_SERVICE_READ_URL`), the same check the MCP servers
  make for run tokens (ADR 0008; ASVS 10.4.9, "tokens can be revoked"), with the answer cached
  `GOLEM_RUN_STATUS_TTL_SECONDS` (10 s). A run that is canceled, finished or unknown gets 401
  `invalid_token` (`run_not_active`), audited with the chain; a lookup that fails gets 503,
  also audited: the edge fails closed. The status client is shared with the MCP servers
  (`golem.run_status`).
- **No cascade on cancel.** Cancelling a run stops it delegating but does not cancel the
  children it already started; that is left for the step that waits for children (Temporal).

### Step 2 as built

- **Signed cards** (`golem.edge.card_signing`). The edge signs every public card at start
  with the a2a-sdk's own helpers (`a2a.utils.signing`), so what it signs is what an SDK client
  checks. Per the A2A 1.0 specification: the payload is the card without `signatures` ("The
  `signatures` field itself **MUST** be excluded from the content being signed", 8.4.1) and
  without default values, canonicalized with RFC 8785 (8.4.1); the signature is a JWS (RFC
  7515) whose protected header carries `alg` `ES256`, `typ` `JOSE`, `kid` and `jku` (8.4.2:
  "The protected header **MUST** include" `alg`, `typ` "**SHOULD** be set to "JOSE"", `kid`;
  "**MAY** include" `jku`), kept as `protected` and `signature` in `AgentCardSignature`, the
  payload detached. Signing replaces any signature a card had.
- **A key of its own, not next to the run-token keys.** The card key is
  `GOLEM_CARD_SIGNING_KEY_FILE` with kid `GOLEM_CARD_SIGNING_KID`, held by the edge alone, not
  the orchestrator's run-token key the Decision above suggested: the run-token key cannot
  overlap in rotation and breaks runs in flight when rotated, the card key touches no run; and
  the task service's `internal-read` port is not reachable from outside, so the public key
  could not be published there anyway. The edge publishes it at
  `GET /.well-known/golem-card-keys.json` (the `jku` of each signature), one key, cacheable.
- **Verification** is a pure function, `verify_card(card_json, jwks)`: it parses the card as
  the SDK's client does, takes keys only from the given key set (never from `jku`: 8.4.3 allows
  keys "from a trusted key store"), accepts only ES256 and passes when one signature verifies
  (8.4.3: "Multiple signatures **MAY** be present to support key rotation").
- **Two limits of the canonical form.** The SDK drops every empty value before canonicalizing,
  so the card's security requirement (`oidc` with an empty scope list) is outside the signed
  bytes; its security scheme and the identity provider's URL are inside. And fields A2A 1.0
  does not define are dropped when the card is parsed, so they are never signed or checked.
  Both are the reference implementation's behaviour, kept so partners using an SDK agree with
  the edge on the bytes.
- **`GET /agents`** answers `{"agents": [{"name", "description", "card_url", "skills"}]}`.
  Without an `Authorization` header: every published card, cacheable by anyone
  (`public, max-age=60`). With one: the token is verified exactly as for `/a2a` (IdP or call
  token, the failure limit per address, revocation of a stopped run's call token), then the
  list is
  narrowed by `golem.edge.policy.callable_agents`, which is `evaluate` for each published
  agent, so the directory never shows what a call would be refused: registry entry, cycle
  (an agent never sees itself or its chain), depth. `Cache-Control: private, max-age=60`;
  both answers `Vary: Authorization`. A header with a bad token is 401, never the public list.
- **Rate limits.** An authenticated listing takes a token from the caller's `/a2a` bucket
  (`GOLEM_RATE_CALLER`), so listing cannot be used to go around it. Anonymous listings and key
  set fetches take one from a new per-address bucket, `GOLEM_RATE_DIRECTORY` (120/min, burst
  60), amending ADR 0012's table. Cards stay unlimited, as before: the UI fetches every card
  for every user from its own pod address, which a per-address limit would make one client.
- **Audited when authenticated, never anonymous.** An authenticated listing is the registry's
  answer to "whom may I call" for that caller, an agent's reconnaissance before delegating
  among them, so it is recorded like a call: `operation` `ListAgents`, `target_system`
  `directory`, the agents shown in `request`, the subject as the account and the chain, before
  the answer; an audit log that cannot be written refuses the listing (503), as for calls.
  Rows are bounded by the caller's rate limit and a refusal streak writes one row (ADR 0012).
  An anonymous listing shows only what every card already shows, has no principal to record,
  and a row per request would grow the insert-only log at the rate of a flood, the
  "garbage-data creation" ASVS 2.4.1 names (ADR 0012), so it is not audited.
- **Every edge response** now carries `X-Content-Type-Options: nosniff`, a
  `Content-Security-Policy` of `default-src 'none'; frame-ancestors 'none'`,
  `Referrer-Policy: no-referrer`, `Strict-Transport-Security` when the public base URL is
  HTTPS, and `Cache-Control: no-store` unless the route set its own. Cards and the key set
  are `public, max-age=300` with an `ETag` and answer `If-None-Match` with 304 (8.6.1:
  "**SHOULD** include a `Cache-Control` response header with a `max-age` directive",
  "**SHOULD** include an `ETag`").
- **Not done:** a partner verifying a card with their own client; publishing the previous card
  key next to the current one for a rotation overlap; a time in the signature.
