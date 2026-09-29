# 16. Reading the running system: metrics, logs and the services' own MCP servers

## Status

Proposed, 2026-09-28

## Context

Golem's agents work on a system that is running. Its state is in its metrics and logs, and
some of its components expose their own MCP servers. An agent that analyses the system (on an
alert or on a schedule) or changes it (code, pages, replies) is guessing if it cannot read that
state. Today a role can read Jira and Confluence and nothing else ([ADR 0008](0008-platform-mcp-servers.md)).

The system is monitored the usual way. Every component serves Prometheus metrics and has
required alerts. Several Prometheus instances cover one environment: a cluster-wide one, one
per namespace, a team instance for services on virtual machines, and a long-term store for
trends over days. Development and test environments, a preview environment and production each
have their own set. Logs go to OpenSearch through the organization's log platform. The logging
standard that fixes field names is still a draft, and the platform renames fields on the way in
(anything outside `[a-zA-Z0-9_]` becomes `_`, so `log.level` is stored as `log_level`). A tool
therefore cannot assume one log schema.

Four questions:

- how a role reads metrics and logs without holding the credentials;
- how a role calls an MCP server owned by the team of a component, not by Golem;
- how Golem keeps an agent from overloading the monitoring systems it reads;
- how personal data, card numbers and secrets are kept away from the model.

The organization's security requirements already answer the last question at the source. Its
logging requirement CS-SIEM-005 forbids writing in clear text "account and contract details,
personal data, CVV, PAN, authentication information" into logs. A log line a role reads is one
the component was required to write clean. Separately, the security requirements for LLM
gateways (document 584837296) make masking of PAN and secrets at the gateway the gateway's
job, and storing prompts and replies the platform's. (The quotations here are translated from
the Russian original.)

Sources:

- MCP specification 2026-07-28, Authorization
  (<https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization>):
  - Token Handling: "MCP servers **MUST** validate that access tokens were issued specifically
    for them as the intended audience, according to RFC 8707 Section 2"; "MCP servers
    **MUST** only accept tokens that are valid for use with their own resources"; "MCP servers
    **MUST NOT** accept or transit any other tokens".
  - Resource Parameter Implementation: the `resource` parameter "**MUST** identify the MCP
    server that the client intends to use the token with" and "**MUST** use the canonical URI
    of the MCP server"; canonical URIs are lowercase scheme and host, with no fragment, and
    implementations "**SHOULD** consistently use the form without the trailing slash".
  - Overview, item 4: "MCP servers **MUST** implement OAuth 2.0 Protected Resource Metadata
    (RFC9728)".
- Prometheus HTTP API (<https://prometheus.io/docs/prometheus/latest/querying/api/>):
  `/api/v1/query` (`query`, `time`, `timeout`, `limit` = "maximum returned series",
  `lookback_delta`), `/api/v1/query_range` (plus required `start`, `end`, `step`),
  `/api/v1/series` and `/api/v1/label/<name>/values` (`match[]`, `start`, `end`, `limit`).
  The response carries `warnings` and `infos`. Prometheus refuses a range query with
  `(end - start) / step > 11000`: "exceeded maximum resolution of 11,000 points per
  timeseries" (`web/api/v1/api.go`). Server defaults: `--query.timeout` 2m,
  `--query.max-concurrency` 20, `--query.max-samples` 50 000 000 (`cmd/prometheus/main.go`).
- OpenSearch 3.5, Search API (<https://docs.opensearch.org/3.5/api-reference/search-apis/search>):
  `timeout` ("how long the operation should wait for a response from active shards", default
  `1m`), `terminate_after` ("the maximum number of matching documents [...] OpenSearch should
  process before terminating the request", default no maximum), `size`, `_source_includes`,
  `allow_partial_search_results` (default `true`). Paging with `from` and `size` is limited to
  10 000 results. The `query_string` query has `allow_leading_wildcard`. The `terms`
  aggregation returns `size` buckets (default 10).
- The organization's information security requirements, logging family CS-SIEM, item
  CS-SIEM-005: writing account and contract details, personal data, CVV, PAN and
  authentication information into logs in clear text is forbidden.
- LiteLLM guardrails (<https://docs.litellm.ai/docs/proxy/guardrails/pii_masking_v2>,
  <https://docs.litellm.ai/docs/proxy/guardrails/quick_start>):
  - The Presidio guardrail's `pre_call` mode runs "before LLM call, on input", with
    `pii_entities_config` per entity `MASK` or `BLOCK`. `logging_only` masks only what is
    logged, not what the model gets.
  - `skip_tool_message_in_guardrail`, set globally or per guardrail, excludes "tool call
    results from guardrail evaluation while preserving the full message list sent to the
    model".
  - Guardrails attached to a virtual key or team "always apply automatically"; a team's
    `modify_guardrails: false` makes a request that tries to switch a guardrail off fail with
    403.
- *OWASP Application Security Verification Standard 5.0*, с. 31 (PDF 32), 2.4.1:
  "anti‑automation controls [...] to protect against excessive calls to application functions
  that could lead to [...] denial‑of‑service, or overuse of costly resources".
- [ADR 0004](0004-security-boundary-outside-the-job.md): everything inside a Job is untrusted.
  [ADR 0007](0007-run-tokens.md) and [ADR 0008](0008-platform-mcp-servers.md): run tokens and
  the servers that accept them.

## Decision

### 1. Tool groups per environment

Metrics and logs are platform tool groups, served by `python -m golem.mcp` like `tracker.read`
and `wiki.read`. Their names follow `<system>.read.<environment>`:

| Group | Upstream |
| --- | --- |
| `metrics.read.test`, `metrics.read.preview`, `metrics.read.prod` | the environment's Prometheus instances |
| `logs.read.test`, `logs.read.preview`, `logs.read.prod` | the environment's OpenSearch |

`test` covers every development and test environment; `preview` the pre-production one; `prod`
production. The names match the registry's `TOOL_GROUP` pattern as they are. `tracker.read` and
`wiki.read` keep their names: Jira and Confluence have no environments.

**One server per group, as now.** Each group is its own Deployment (`GOLEM_MCP_GROUP`), with
its own read-only technical accounts upstream. So a production credential never sits in a
process that serves a test group, and a production server can be scaled, limited or stopped on
its own.

**Named sources.** One group fronts several upstream endpoints: for metrics, the cluster,
namespace, team and long-term Prometheus instances; for logs, the index patterns an agent may
read. A file `GOLEM_MCP_SOURCES_FILE` gives each source a name, a base URL, a reference to its
credential and its limits. Tools take a source by name and never a URL, so a role cannot point
a server at an address of its own choosing. `list_sources()` returns the names with a
one-line description, for example: "namespace Prometheus: now, 15 days; long-term: trends, 90
days".

**Granting production.** An agent gets a `*.prod` group only if two layers allow it:

- the platform's grant file (`GOLEM_AGENT_TOOLS_FILE`, [ADR 0007](0007-run-tokens.md)), owned by operators, names
  the group for the agent;
- the agent's catalog asks for it in the role that needs it.

A catalog merge request that adds a production group is evaluated against the golden set like
any other change ([ADR 0006](0006-evaluation-in-ci-first.md)). An agent absent from the grant file still gets nothing.

### 2. Tools and their cost limits

A server's limits are checked **before** the upstream call, and a refusal is a tool error
result that names the limit, so the model can narrow the query. Default values:

**`metrics.read.*`** (Prometheus HTTP API):

- `query(source, promql, time?)` → `/api/v1/query`.
- `query_range(source, promql, start, end, step)` → `/api/v1/query_range`. The window
  `end - start` is at most the source's `max_window` (7 days by default, 90 days for a
  long-term store). There are at most 500 points per series, that is `(end - start) / step
  ≤ 500`, far below Prometheus's 11 000.
- `series(source, match, start?, end?)` and `label_values(source, label, match?)`, so the
  model can find metric and label names instead of guessing them.
- Every request sends `timeout=20s` and `limit=50` series (200 for label values). The HTTP
  client gives up after 25 s. A server that predates `limit` returns more; the tool then cuts
  the result at 50 series itself and says so.
- An answer lists each series' labels, and for a range its first, last, minimum, maximum and
  mean, followed by the points while the answer stays within 8 000 characters. Prometheus'
  `warnings` and `infos` are passed through.

**`logs.read.*`** (OpenSearch `_search`, no other endpoint):

- `search_logs(source, query, start, end, limit?, fields?)`. The server builds the request
  body itself:
  - a `bool` filter with a `range` on the source's timestamp field, which is configured per
    source because field names differ;
  - a `query_string` with `allow_leading_wildcard: false`;
  - sorting newest first, `size` of 1..50 (20 by default), `_source_includes` of the
    requested fields or the source's default set;
  - `timeout=10s`, `terminate_after=100000`.
- `count_logs(source, query, start, end, interval)`: a `date_histogram` with at most 200
  buckets, for "since when, and how often".
- `top_values(source, query, start, end, field, size?)`: a `terms` aggregation on a keyword
  field, `size` of 1..20, for "which service, which error, which host".
- The window is at most 24 hours per call. The index is always a pattern of the named source,
  never an argument, so security and audit indices stay unreachable. The OpenSearch role of
  the server's account grants read on exactly those patterns, so a bug in the server widens
  nothing.
- There is no raw query DSL, no scripts, no other aggregations and no scrolling. These three
  tools answer what an investigation asks first, and each has a bounded cost. A timed-out
  search's partial result is returned with `timed_out` stated, not hidden. Each field value is
  cut at 500 characters and the whole answer at 8 000.

**Pressure on the upstream.** Besides [ADR 0012](0012-rate-limits.md)'s limits at the door, each server replica holds
at most 4 upstream requests at once per source. Each run gets a token bucket of 30 tool calls
a minute (burst 10) per server; past it, the call is a tool error saying to wait. Prometheus
allows 20 concurrent queries by default and serves dashboards and alerts first. A handful of
agents on an alert storm must not take those slots.

### 3. The services' own MCP servers

A component team may expose its own MCP server to Golem's agents. That server accepts Golem's
run tokens itself; there is no proxy in between.

**Group names** are `svc.<component>.read.<environment>`, for example
`svc.client-cache.read.prod`. The `svc.` prefix keeps them apart from platform groups; the
environment suffix and the two-layer production grant work as in section 1.

**The registry** ([ADR 0007](0007-run-tokens.md)) gains a field. A group's entry is `url`, `tools` and now
`resource`: the server's canonical URI, which is the token audience. It defaults to `url`
without a trailing slash. `tools` stays the allowlist: the runtime loads only the named tools,
whatever the server offers.

**The contract a service's MCP server meets** to be listed:

- It is served over HTTPS.
- It accepts a request only with `Authorization: Bearer <run token>` and verifies it as ADR
  0008 does:
  - ES256 only, algorithm from a fixed list;
  - issuer `golem`;
  - `aud` equal to its own canonical URI;
  - `exp`;
  - keys from the task service's JWKS, refetched on an unknown key id at most once a minute.

  It refuses with 401 `invalid_token` otherwise and never forwards the token upstream.
- It serves a request only if the token's `tools` names its group, and a `tools/call` only for
  that group's tools; otherwise 403 `insufficient_scope`.
- It asks the task service for the run's status (`GET /internal/runs/{run_id}`), caches the
  answer for at most 10 s, and treats anything but `running`, or a failed lookup, as a refusal.
- It offers run tokens read-only tools only. A change to the system goes through a proposal
  that the platform applies after a person decides ([ADR 0015](0015-proposals.md)), never through a run.
- It writes one audit record per request to its own logs, which reach the log platform. The
  record has [ADR 0008](0008-platform-mcp-servers.md)'s fields: `caller`, root run and run from the token, the tool, argument
  names and values cut at 120 characters, `token=sha256:<16 hex>`, and `allow` or
  `deny: <reason>`. Results and the token itself are never logged.
- Its answers carry no account or contract details, personal data, CVV, PAN or
  authentication information in clear text: the rule CS-SIEM-005 sets for logs, applied to
  what the server returns. The component team owns it, as it owns its logs.
- Its answers are bounded (8 000 characters as a guide), and its own cost limits protect what
  is behind it, as section 2 does for metrics and logs.

Before its first group is granted, the component team shows the contract met with the
checklist in the operator guide: forged, expired, wrong-audience, wrong-group and
revoked-run tokens.

**Reaching the task service.** A service's server may run in another cluster. The task
service's `internal-read` routes (`/internal/run-keys` and `/internal/runs/{run_id}`) are
published on an internal HTTPS host. The network admits only the listed servers' addresses
to it, as the cluster's NetworkPolicy does for Golem's own servers. The routes return public
keys and a single status for a run id that is a UUID; they need no credential.

**Egress.** A Job reaches only what `deploy/k8s/base/network-policies-jobs.yaml` admits. Each
service server listed in the registry comes with one egress rule to its address and port, in
the same change. A registry entry without a rule fails closed: the connection is refused and
the run's tool loading fails.

### 4. One run token per MCP server (amends ADR 0007 and ADR 0008)

A token that every MCP server accepts could be replayed by any of them against every other.
Once servers belong to other teams, that matters. So:

- **Audience.** A run token's `aud` is the canonical URI of the one server it is for, not
  `golem-mcp`. Its `tools` holds only the granted groups that server serves. The other claims
  are [ADR 0007](0007-run-tokens.md)'s.
- **Issuing.** The orchestrator reads the registry that the Job mounts. For each distinct
  server among the agent's granted groups it signs one token. It delivers them in the run's
  Secret as `GOLEM_RUN_TOKENS`: a JSON object from canonical URI to token, replacing
  `GOLEM_RUN_TOKEN`. The runtime sends each server only its own token. The call token for the
  edge (`golem-a2a`, [ADR 0014](0014-golem-as-an-a2a-node.md)) is unchanged.
- **Golem's own servers** get `GOLEM_MCP_RESOURCE`, their canonical URI, and verify `aud`
  against it. For one release they also accept `golem-mcp`, so Jobs launched before the
  upgrade finish; the release after that drops it.
- **Protected Resource Metadata.** The MCP specification requires servers to publish RFC 9728
  metadata, so that a client can discover the authorization server. Golem's runtime discovers
  nothing: its tokens are issued before the Job starts, and Golem publishes no OAuth
  authorization server metadata for the issuer `golem`. Golem's servers do not serve the
  document in this step; this is a recorded deviation. A service's server may serve one for
  its other clients.

### 5. Personal data: clean at the source

Golem does not mask what its tools return and does not make masking at the model gateway a
condition for granting a group:

- Logs are clean by requirement (CS-SIEM-005), and service servers by contract (section 4).
  Personal data found in a log line is a defect of the component that wrote it, reported to
  its team; the same line is already visible to everyone with access to the log platform.
- The model gateway's masking (PAN, secrets, whatever profile its owner sets for Golem's key)
  stays the gateway owner's, under document 584837296. Golem relies on it as a second layer,
  not as the first, and does not check it before granting `logs.read.*` or `svc.*`.
- The runs call the OpenAI-compatible route (`GOLEM_MODEL_GATEWAY_URL` ends in `/v1`), not a
  provider pass-through route, so whatever guardrails the gateway applies do apply.

What Golem stores itself is kept from holding raw tool results:

- **Traces.** Prompt and tool-result content reaches the trace store only when
  `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT` is `true`. The orchestrator sets it to
  `false` in every Job whose grant holds a `logs.read.*`, `*.prod` or `svc.*` group, whatever
  the default. The trace store does not mask, and storing traces is the platform's
  responsibility.
- **The result branch.** Raw tool results stay inside the Job. deepagents offloads large
  results to agent state, not to the clone (`workspace_backend`). The branch, the report and
  the proposals are written from the model's output, which is produced from masked input.

## Consequences

- An investigation can use the same numbers and log lines a person would open in Grafana or
  OpenSearch Dashboards. It cannot run an unbounded query against production: every limit is
  checked before the upstream sees the request.
- Six new MCP Deployments (three environments, two systems). Each has its own credentials,
  NetworkPolicy and upstream allowlist. The production servers are the only ones with
  production credentials.
- A limit can make a question unanswerable in one call ("errors over the last month"). The
  model has to use the long-term source, a coarser step or `count_logs`. The refusal says
  which limit was hit.
- A service's server is trusted to meet the contract; Golem cannot enforce it from outside.
  Golem's own `audit_log` has no row for calls to it: the record is in the service's logs.
  The price of a component team owning its server is that an investigation of such a call
  reads two logs.
- Per-server audiences mean a leaked token is worth one server's groups, for one running run,
  until the status cache expires. A service's server can no longer replay what it receives.
- The Secret now holds one token per server, and the orchestrator must read the registry.
  Adding a server to an agent's grant takes effect at the next run.
- The deviation from RFC 9728 means a generic MCP client cannot discover how to get a Golem
  token. Only Golem's runtime calls these servers with Golem tokens, so nothing breaks. It
  stays open until Golem publishes authorization server metadata.
- Golem relies on the components: a component that writes personal data into its logs in
  breach of CS-SIEM-005, or a service server that returns it, passes it to the model unless
  the gateway's masking happens to catch it. Such a finding goes to the component's team as a
  defect. Masking inside Golem stays open as a later decision, if these defects turn out to
  be common.
- Content capture in traces is off for exactly the runs whose content is most useful to
  debug. Debugging them relies on the report, the tool call spans (names, ids, status) and
  the audit rows.
- The internal HTTPS host for `internal-read` is a new published endpoint of the task service.
  Its protection is network admission only, as inside the cluster.
- Deployment manifests, the sources files, and the operator checklist for service servers
  are separate steps.
