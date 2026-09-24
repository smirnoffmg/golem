# 7. A run token per run, issued by Golem and delivered in a Secret owned by the Job

## Status

Accepted, 2026-09-24

## Context

A role calls platform MCP servers (Jira, Confluence, GitLab) for its tools
([ADR 0004](0004-security-boundary-outside-the-job.md)). The servers hold the secrets to the
systems behind them; they need to know which run is calling, on whose behalf and which tool
groups it may use. Everything inside the Job is untrusted, so whatever the Job presents must be
worth as little as possible if it leaks.

Three candidates:

- **The user's identity provider token.** It is scoped to the edge's audience and lives as long
  as the user's session, not the run; it carries the user's rights, not the agent's grant; and a
  run started by an agent or an adapter has no user token at all. Handing it to untrusted code
  would give that code everything the user can do.
- **A long-lived shared secret** between the Jobs and the MCP servers. Every Job would hold the
  same credential, it would name no run, carry no grant, and revoking it would break every run
  at once.
- **A token Golem issues per run.** It names one run, carries exactly the platform's grant for
  the agent, and expires with the run.

ASVS asks that a client be assigned "only the required scopes" (10.4.11) and that tokens be
revocable (10.4.9) (*OWASP Application Security Verification Standard 5.0*, с. 66 (PDF 67)), and
that a resource server accept only tokens "intended for use with that service (audience)" and
base its decision on claims such as `sub` and `scope` (10.3.1, 10.3.2, с. 64 (PDF 65)).

## Decision

The orchestrator issues a run token when it launches a run's Job.

- **Claims.** `iss` = `golem`, `aud` = `golem-mcp`, `sub` = the run id, `iat`, `exp`, `agent`,
  `caller` (the principal the run acts for), `root` (the root run of the call chain) and `tools`
  (tool group names). `tools` is the platform's grant for the agent, from the file named by
  `GOLEM_AGENT_TOOLS_FILE` (`agent: [tool group, ...]`), not what the agent's catalog asks for:
  an agent absent from the file gets no tools. Once the MCP servers serve only the groups in
  `tools` (the next step), a role's list in the catalog can narrow the grant but never widen it.
- **ES256.** Signed with an EC P-256 key (`GOLEM_RUN_TOKEN_KEY_FILE`, `GOLEM_RUN_TOKEN_KID`).
  Verifiers need only the public key, which the task service serves as a JWKS at
  `GET /internal/run-keys`. The route is internal: the edge forwards only `/a2a`. Verification
  takes the algorithm from a fixed list, never from the token header.
- **Audience.** `golem-mcp`, so a run token is refused by the edge and anything else that checks
  its own audience, and no other token is accepted by the MCP servers.
- **Lifetime.** `exp` = issue time + the Job's `activeDeadlineSeconds` + 60 s of grace for clock
  skew. The token cannot outlive the run by more than a minute, however it leaks.
- **Delivery.** The launcher creates the Job first, then a Secret `golem-run-<run id>-token`
  holding `GOLEM_RUN_TOKEN`, with an `ownerReference` to the Job's uid. The container reads it
  through a second `envFrom.secretRef` with `optional: false`, so the pod waits for the Secret
  instead of starting without a token. Deleting the Job (cancel, or its TTL after it finishes)
  garbage-collects the Secret with it. A relaunch of a running run is idempotent: an existing
  Secret for the run is kept. The token never appears in the Job manifest.
- **MCP registry.** When `GOLEM_MCP_REGISTRY_CONFIGMAP` is set, the Job mounts that ConfigMap
  read-only at `/etc/golem/mcp` and gets `GOLEM_MCP_REGISTRY=/etc/golem/mcp/registry.yaml`.

## Consequences

- **RBAC must be tight.** A Secret is readable, base64-decoded, by anyone with RBAC read on
  Secrets in its namespace (*Cloud Native DevOps with Kubernetes*, с. 247). The orchestrator's
  service account may create and delete Jobs and Secrets in the Jobs namespace only, with no
  `get`, `list` or `watch` on Secrets; nothing may read Secrets there except the kubelet, which
  needs no role for it. The Job's pod has no service account token
  (`automountServiceAccountToken: false`).
- A leaked token is limited to one run's grant, one audience and the run's deadline.
- Until revocation exists, a token stays valid until `exp` even after its run is canceled. In
  the next step the MCP servers check that the run is still active before serving a call, which
  gives the revocation ASVS 10.4.9 asks for without a token blocklist.
- Rotating the signing key means publishing the new key in the JWKS before signing with it and
  keeping the old one until the last token signed with it expires.
- A crash between creating the Job and its Secret leaves a pod waiting on the Secret; the
  orchestrator's relaunch of the running run creates it, and the Job's deadline bounds the wait
  otherwise.
