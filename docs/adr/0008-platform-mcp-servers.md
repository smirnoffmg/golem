# 8. Platform MCP servers are resource servers for run tokens

## Status

Accepted, 2026-09-24

## Context

A role's tools come from platform MCP servers, one server per tool group of the registry
(`tracker.read`, `wiki.read`). The Job calls them with its run token
([ADR 0007](0007-run-tokens.md)); the servers hold the credentials to Jira and Confluence
([ADR 0004](0004-security-boundary-outside-the-job.md)). Everything inside the Job is
untrusted, so a server must decide on its own what a caller may do, and leave a record of it.

Four questions: which tokens a server accepts and what it does with them upstream, what a token
lets its holder do, how a canceled run loses access before its token expires, and what is
logged.

Sources:

- *OWASP Application Security Verification Standard 5.0*, с. 64 (PDF 65): a resource server
  accepts only tokens "intended for use with that service (audience)" (10.3.1) and bases its
  authorization decision on the token's claims (10.3.2).
- *OWASP ASVS 5.0*, с. 66 (PDF 67): tokens can be revoked "to mitigate the risk of malicious
  clients or stolen tokens" (10.4.9).
- *OWASP ASVS 5.0*, с. 90 (PDF 91): "Verify that failed authorization attempts are logged. For
  L3, this must include logging all authorization decisions, including logging when sensitive
  data is accessed (without logging the sensitive data itself)" (16.3.2); session tokens appear
  in logs only hashed or masked (16.2.5).
- MCP specification, Security Best Practices, Token Passthrough
  (<https://modelcontextprotocol.io/docs/2026-07-28/tutorials/security/security_best_practices#token-passthrough>):
  "MCP servers **MUST NOT** accept any tokens that were not explicitly issued for the MCP
  server", and forwarding a client's token downstream is the confused deputy the section
  describes.
- RFC 6750, section 3.1: `invalid_token` covers a token that is "expired, revoked, malformed, or
  invalid"; `insufficient_scope` answers 403.

## Decision

`python -m golem.mcp` serves one tool group (`GOLEM_MCP_GROUP`) over MCP streamable HTTP at
`/mcp`, stateless, with FastMCP from the MCP Python SDK. A gate in front of it decides every HTTP
request before the SDK sees it.

- **Only run tokens.** A request needs `Authorization: Bearer <run token>`, verified with
  `golem.run_token.verify` (ES256, issuer `golem`, audience `golem-mcp`) against the JWKS the
  task service publishes at `/internal/run-keys`. The keys are cached and refetched on an
  unknown key id at most once per `GOLEM_MCP_KEYS_REFRESH_SECONDS` (60 s); a server that never
  got keys refuses every token. The key cache is the edge's, moved to `golem.jwks` and shared.
  Missing token: 401 with `WWW-Authenticate: Bearer realm="golem-mcp"`; invalid or expired:
  401 with `Bearer error="invalid_token"`.
- **Authorization from claims.** The token's `tools` must name the server's group, otherwise
  403 `insufficient_scope` with the group as `scope`. The server offers exactly the group's
  tools, and a `tools/call` naming any other tool is refused before the SDK runs it.
- **Revocation by run status.** The server asks the task service
  (`GET /internal/runs/{run_id}`, answered from `golem_runs` by the orchestrator's runs module)
  whether the run is still `running`, and keeps the answer for
  `GOLEM_MCP_RUN_STATUS_TTL_SECONDS` (10 s). Any other status, or an unknown run, is a revoked
  token: 401 `invalid_token`. A failed lookup is not cached and refuses the call with 503.
- **No token passthrough.** Jira and Confluence are called with the server's own credentials
  (`GOLEM_MCP_UPSTREAM_TOKEN`, Basic with `GOLEM_MCP_UPSTREAM_USER` on Atlassian Cloud, Bearer
  personal access token on Data Center, the Jira adapter's helper). The upstream client is built
  once at start; nothing from the request's headers reaches it.
- **Read-only tools, compact answers.** `tracker.read`: `search_issues(jql, limit)` and
  `get_issue(key)` on the Jira REST API v2 (Cloud's `/rest/api/2/search/jql`, Data Center's
  `/rest/api/2/search`, chosen by `GOLEM_MCP_JIRA_DEPLOYMENT`). `wiki.read`:
  `search_pages(cql, limit)` and `get_page(page_id)` on the Confluence REST API v1
  `/rest/api/content/search`, the only CQL search on both deployments; a page is read by
  `id = <digits>` through the same endpoint. Answers carry key fields only; bodies become plain
  text cut at 8 000 characters; limits are held to 1..50. An upstream error or timeout becomes a
  tool error result, not a failed request.
- **Every decision audited.** Each request writes one `audit_log` row as the `golem_mcp` role
  before it is served: account = the token's `caller` (`unauthenticated` when the token did not
  verify), source `mcp:<group>`, target `jira:<host>` or `confluence:<host>`, operation = the
  tool name or the MCP method, request = method, tool, argument names and values (each cut at
  120, the whole at 400 characters) and `token=sha256:<16 hex>`, result `allow` or
  `deny: <reason>`, chain = [root run, run]. Results are never logged. A row that cannot be
  written refuses the request with 503.

## Consequences

- A leaked run token is worth one group's read tools, for one running run, for at most the
  status TTL after the run ends. A canceled run's calls stop within 10 s.
- Every request of a tool call is audited, not just the call: the MCP client opens a session
  (`initialize`, `notifications/initialized`) around calls, so the log holds a few protocol rows
  per call. That is the price of L3's "all authorization decisions"; they are cheap to filter by
  operation.
- The audit row is written before the upstream call, so it records the decision, not whether
  Jira answered; the tool error reaches the caller and the trace.
- The task service and the audit database are now on the path of every tool call. Both failing
  closed means their outage stops tools, which the runtime turns into a failed run rather than a
  run without controls.
- Failed authentication is logged as `unauthenticated` with the token's hash, not the claims it
  asserts: an unverified token's claims are the caller's words.
- The SDK's `token_verifier`/`AuthSettings` hook is not used: it decides on the token alone and
  answers before any code could audit the request with its method and arguments.
- Deployment manifests (Deployments, Services, NetworkPolicy, secrets) are a separate step.
