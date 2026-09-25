# Security operations

What each control protects, how to rotate every key, and what to take to a security review.
The reasoning behind each control is in the ADRs linked from the tables.

## Trust boundaries

Everything inside a run's Job is untrusted: a model drives it
([ADR 0004](../adr/0004-security-boundary-outside-the-job.md)). Every other boundary is
between parties that do not trust each other's claims.

| Boundary | Control | Protects against | Decision |
| --- | --- | --- | --- |
| People, services, agents → Golem | the edge checks every token (issuer, audience, signature, expiry), the call registry, the chain policy (depth, cycles), a rate limit per caller; it audits the decision before forwarding and refuses what it cannot check | calls by anyone the identity provider did not vouch for, to agents they may not call; floods | [0002](../adr/0002-edge-and-task-service-split.md), [0012](../adr/0012-rate-limits.md) |
| Run → another agent | the run's call token (ES256, audience `golem-a2a`, the subject, the acting agent, the chain, the root run, the run's deadline), checked by the edge against the orchestrator's keys and refused once its run is no longer running (status cached `GOLEM_RUN_STATUS_TTL_SECONDS`, 10 s; unknown status refused); the call registry must name `agent:<name>`, the chain may not grow past `GOLEM_MAX_CHAIN_DEPTH` or repeat an agent; the child runs as the subject under the root's budget; the agent may only start a task | a leaked call token outliving its run; a run acting as its own principal, reading or cancelling the subject's tasks, fanning out without bound, or calling agents nobody allowed it to | [0014](../adr/0014-golem-as-an-a2a-node.md) |
| Browser → UI | authorization code with PKCE S256, `state`/`nonce` bound to the browser, tokens only on the server (encrypted in `golem_ui`), `__Host-` cookie, CSRF token on every `POST`, strict CSP | token theft by scripts, login CSRF, session fixation, clickjacking | [0011](../adr/0011-web-ui.md) |
| Edge → task service | NetworkPolicy admits only the edge to port 8000, and every request must carry `GOLEM_EDGE_TOKEN`; the principal, chain and root-run headers are trusted only then, and the edge builds them from nothing, so a client's own never pass | a pod in the namespace acting as any user; a client claiming a chain or another chain's budget | [0009](../adr/0009-deployment-on-kubernetes.md), [0014](../adr/0014-golem-as-an-a2a-node.md) |
| MCP servers, edge, reconciler → task service | one listener per kind of caller: MCP servers read run keys and status (8001), the edge reads the run keys too (8001, for call tokens), the reconciler only notifies (8002), and the outcome is read from `golem_runs` | a compromised MCP server starting runs or forging outcomes | [0009](../adr/0009-deployment-on-kubernetes.md) |
| Run → anything | namespace `golem-jobs` with default-deny egress to five destinations, `restricted` Pod Security, no service account token, a `ResourceQuota`, admission quotas per caller and chain | a run reaching databases, other runs, the Kubernetes API or the internet; one caller exhausting the cluster | [0004](../adr/0004-security-boundary-outside-the-job.md), [0009](../adr/0009-deployment-on-kubernetes.md) |
| Run → Jira, Confluence | platform MCP servers accept only run tokens (ES256, audience `golem-mcp`, the group in `tools`, the run still running), hold the upstream credentials, audit every request | a leaked run token outliving its run; a run using tools it was not granted; upstream credentials in untrusted code | [0007](../adr/0007-run-tokens.md), [0008](../adr/0008-platform-mcp-servers.md) |
| Run → Git | protected default branch; the run only pushes `golem/<target>/<run>`; the merge request is opened by the reconciler; a human merges | a run changing the record of truth by itself | [0004](../adr/0004-security-boundary-outside-the-job.md) |
| Jira → adapter | HMAC-SHA256 of the body with a shared secret; the message id from the signed body | forged or replayed webhooks | README, "Jira adapter" |
| Mattermost → adapter | the command token, NetworkPolicy admitting only the Mattermost server, team, channel and agent allowlists | forged commands from elsewhere | [0010](../adr/0010-mattermost-adapter.md) |
| Task service → adapters | push URLs only under `GOLEM_PUSH_ALLOWED_PREFIXES`; a per-run HMAC push token | pushes to arbitrary cluster addresses; forged outcomes posted to Jira or chat | README, [0010](../adr/0010-mattermost-adapter.md) |
| Services → databases | one owner role per database, `CONNECT` revoked from `PUBLIC`, the audit log `INSERT` only, per-role connection limits and statement timeouts | one service reading or changing another's data; audit rows being edited | [0003](../adr/0003-one-postgres-cluster-per-owner-databases.md) |
| Processes → Kubernetes API | only the task service (create Jobs and token Secrets) and the reconciler (read Jobs and pods) have tokens; nobody can read a Secret | a process reading run tokens back or escaping its role | [0009](../adr/0009-deployment-on-kubernetes.md) |
| Metrics | a port of their own, reachable only from the namespace labelled `golem.dev/monitoring` | traffic, refusals and agent names leaking to callers | [0013](../adr/0013-metrics.md) |

### What a delegating agent can and cannot do

A role gets `delegate_to_agent` only when its catalog names the `agents.delegate` tool group;
the edge then decides every call:

- it **can** start a task of an agent whose call registry entry names `agent:<its name>`, with
  a goal it writes, as the subject of its chain (the person or service that started the first
  run); the child is admitted under the chain's root, so the chain's concurrency and budget
  cover it;
- it **cannot** delegate once its run is no longer running (`run_not_active`, 401, audited;
  the edge refuses with 503 when it cannot learn the run's status), act as itself or as anyone else than that subject, call an agent already in
  its chain, go deeper than `GOLEM_MAX_CHAIN_DEPTH`, read, list or cancel tasks (the edge
  allows it `SendMessage` of a new task only), send into an existing task, or present its call
  token to an MCP server or its run token to the edge (the audiences differ);
- every call is audited with the subject as the account and the whole chain, the acting agent
  last, and rate-limited per acting agent (`GOLEM_RATE_CALLER_*`).

## Keys and secrets

| Secret | Held by | Leaked, it allows | Rotation below |
| --- | --- | --- | --- |
| run token signing key | task service | minting run tokens with any tool group for any running run, and call tokens for any subject and chain the call registry admits, until rotated | [yes](#run-token-signing-key) |
| `GOLEM_EDGE_TOKEN` | edge, task service | with a network policy gap too, starting runs as any principal | [yes](#edge-token) |
| `GOLEM_UI_SESSION_KEY` | UI | with a copy of `golem_ui`, users' access and refresh tokens | [yes](#fernet-keys) |
| `GOLEM_PUSH_CONFIG_KEY` | task service | with a copy of `golem_tasks`, the adapters' push tokens | [yes](#fernet-keys) |
| adapters' client secrets, `GOLEM_PUSH_TOKEN_SECRET`, `GOLEM_JIRA_WEBHOOK_SECRET`, Mattermost tokens | adapters | starting runs as the adapter; posting fake outcomes | [yes](#adapter-secrets) |
| `GOLEM_GIT_TOKEN`, `GOLEM_MODEL_KEY` | every run (`golem-run-secrets`) | pushing branches to every context repository; spending the gateway budget | [yes](#other-secrets) |
| `GOLEM_GITLAB_TOKEN` | reconciler | the GitLab API as the bot | [yes](#other-secrets) |
| database passwords | each service | its own database | [yes](#other-secrets) |

A process reads its Secrets at start. Every rotation below ends with a restart; verify with
the check after it. Rotate on a schedule your policy sets, and at once when a holder leaves or
a value may have leaked.

### Before a rotation that affects runs in flight

Rotating the run token key or the push config key breaks runs that are running at that moment
(below). Find a quiet moment and check that nothing is running:

<!-- run: running-runs -->
```sh
kubectl -n golem-jobs get jobs -l app.kubernetes.io/name=golem-run \
  -o jsonpath='{range .items[?(@.status.active)]}{.metadata.name}{"\n"}{end}'
psql "$GOLEM_RUNS_DSN" -c "select count(*) from runs where status = 'running'"
```

Nothing can pause admission, so a run may still start meanwhile; it fails, the caller sees
`failed`, and starting it again works.

### Run token signing key

The task service signs every run token and every call token with one key and publishes only
that key at `/internal/run-keys`. **Two keys cannot be published at once**, so there is no overlap: a
token signed with the old key is refused as soon as an MCP server has fetched the new key
set, and a run that holds one fails at its next tool call. Rotate when nothing is running.

1. Generate a key and replace the Secret:

<!-- run: rotate-run-key -->
```sh
openssl genpkey -algorithm EC -pkeyopt ec_paramgen_curve:P-256 -out golem-secrets/run-token-key.pem
kubectl -n golem-system create secret generic golem-run-token-key \
  --from-file=key.pem=golem-secrets/run-token-key.pem --dry-run=client -o yaml | kubectl apply -f -
```

2. Give it a new key id: set `GOLEM_RUN_TOKEN_KID` in `golem-tasks-env` in your overlay (for
   example `golem-2`) and apply the overlay.
3. Restart the task service, then the MCP servers and the edge, so all hold the new key at
   once:

<!-- run: rotate-run-key-restart -->
```sh
kubectl -n golem-system rollout restart deployment/tasks
kubectl -n golem-system rollout status deployment/tasks --timeout=5m
kubectl -n golem-system rollout restart deployment/mcp-tracker-read deployment/mcp-wiki-read
kubectl -n golem-system rollout status deployment/mcp-tracker-read --timeout=5m
kubectl -n golem-system rollout status deployment/mcp-wiki-read --timeout=5m
kubectl -n golem-system rollout restart deployment/edge
kubectl -n golem-system rollout status deployment/edge --timeout=5m
```

4. Check that the new key id is published, from a pod that may read it:

<!-- run: rotate-run-key-check -->
```sh
kubectl -n golem-system exec deploy/mcp-tracker-read -- python -c \
  "import urllib.request; print(urllib.request.urlopen('http://tasks.golem-system.svc:8001/internal/run-keys').read().decode())"
```

The answer is a JWKS with one key whose `kid` is the new one. Then start a run of an agent
with tools and check that the MCP servers allowed it (`result` = `allow` in `audit_log`, source
`mcp:<group>`, read as the database owner) or that
`golem_mcp_tool_calls_total{decision="allow"}` grows.

Serving the previous key next to the current one, as [ADR 0007](../adr/0007-run-tokens.md)
describes, would make this rotation safe with runs in flight; it is not built.

### Edge token

`GOLEM_EDGE_TOKEN` is one value in two Secrets, `golem-edge` and `golem-tasks`. Between the two
restarts the edge's forwards are refused (the caller gets an error and may retry); nothing is
let through.

<!-- run: rotate-edge-token -->
```sh
openssl rand -hex 32 > golem-secrets/edge-token
for name in golem-edge golem-tasks; do
  kubectl -n golem-system patch secret "$name" --type merge \
    -p "{\"stringData\": {\"GOLEM_EDGE_TOKEN\": \"$(cat golem-secrets/edge-token)\"}}"
done
kubectl -n golem-system rollout restart deployment/tasks deployment/edge
kubectl -n golem-system rollout status deployment/tasks --timeout=5m
kubectl -n golem-system rollout status deployment/edge --timeout=5m
```

Check: an A2A call through the edge answers with a result, not `edge token required`, and
`golem_authentication_failures_total{process="tasks"}` stops growing.

With the External Secrets Operator, change the remote key `golem/edge-token` instead; both
Secrets are refreshed within their `refreshInterval` (1 h), then restart both Deployments.

### Fernet keys

`GOLEM_UI_SESSION_KEY` encrypts the UI's stored tokens. A new key makes every stored session
unreadable: everyone is signed out and signs in again. Nothing else is lost.

<!-- run: rotate-ui-key -->
```sh
openssl rand -base64 32 | tr '+/' '-_' > golem-secrets/ui-session-key
kubectl -n golem-system patch secret golem-ui --type merge \
  -p "{\"stringData\": {\"GOLEM_UI_SESSION_KEY\": \"$(cat golem-secrets/ui-session-key)\"}}"
kubectl -n golem-system rollout restart deployment/ui
```

Check: sign in to the UI; the old cookie leads to the sign-in page. The unreadable rows expire
by themselves (twelve hours after their sign-in).

`GOLEM_PUSH_CONFIG_KEY` encrypts the push configs of tasks in `golem_tasks`. A new key makes the
push configs of existing tasks unreadable: their outcomes never reach Jira or Mattermost. Rotate
it when no run is running and `golem_outbox_pending` is 0, then restart the task service.

### Adapter secrets

| Secret | Rotate | In between |
| --- | --- | --- |
| `GOLEM_OIDC_CLIENT_SECRET` | regenerate at the identity provider, update the Secret, restart the adapter | new starts fail until the restart; the adapter's cached token works until it expires |
| `GOLEM_JIRA_WEBHOOK_SECRET` | update the Secret and the webhook's secret in Jira together, restart | webhooks are refused with 401 |
| `GOLEM_PUSH_TOKEN_SECRET` | when nothing is running; update, restart | pushes for runs started before are refused: their outcome is not posted |
| `GOLEM_MATTERMOST_COMMAND_TOKEN` | regenerate in Mattermost's slash command settings, update, restart | commands fail |
| `GOLEM_MATTERMOST_BOT_TOKEN` | create a new token for the bot, update, restart, revoke the old one | outcomes fail to post (at most once, [ADR 0010](../adr/0010-mattermost-adapter.md)) |

Check: label an issue or run `/golem` and see the reply and the outcome.

### Other secrets

- **Database passwords.** `ALTER ROLE <role> PASSWORD ...` as in
  [install.md](install.md#3-create-the-databases-and-roles), update the Secrets whose
  connection strings name the role, restart their Deployments. Existing connections keep
  working until the restart.
- **`GOLEM_GIT_TOKEN`, `GOLEM_MODEL_KEY`** in `golem-run-secrets`: create the new token or key,
  update the Secret; the next run's pod reads it. Revoke the old one after the longest running
  run has ended (`GOLEM_JOB_DEADLINE_SECONDS`).
- **`GOLEM_GITLAB_TOKEN`**: new token, update `golem-reconciler`, restart the reconciler,
  revoke the old one. A merge request that fails meanwhile is retried on the next pass.

## For the security review

Known gaps, each recorded where it was decided:

- **The chat caller is not the subject** ([ADR 0010](../adr/0010-mattermost-adapter.md)). The
  edge authenticates the Mattermost adapter, not the person who typed the command; the user is
  an unverified claim. Every chat user shares one quota and acts with the agent's grant. Keep
  `GOLEM_MATTERMOST_AGENTS` and the channels to what every member may do. The same holds for
  Jira: the caller is `service:golem-jira-adapter`, whoever added the label.
- **Sessions** ([ADR 0011](../adr/0011-web-ui.md)): no listing of one's own sessions and no
  administrative termination (ASVS 7.4.5, 7.5.2); signing a user out everywhere means rotating
  `GOLEM_UI_SESSION_KEY`, which signs everyone out.
- **Rate limits are per replica** ([ADR 0012](../adr/0012-rate-limits.md)); IPv6 clients are
  keyed by whole address, not prefix; clients behind one address share a bucket.
- **Network** ([ADR 0009](../adr/0009-deployment-on-kubernetes.md)): on kube-router, a pod's
  own node passes every ingress policy and a new pod's egress is unfiltered for about a second;
  `ipBlock`s cannot name hosts; the External Secrets Operator can write Secrets in both
  namespaces; the edge token proves possession of a secret, not the edge pod.
- **Run tokens** ([ADR 0007](../adr/0007-run-tokens.md)) stay valid until `exp`; revocation
  is the MCP servers' status check, cached 10 s. The signing key has no rotation overlap
  (above).
- **Call tokens** ([ADR 0014](../adr/0014-golem-as-an-a2a-node.md)) are revoked like run
  tokens: the edge asks whether the delegating run is still running, cached 10 s, so a
  canceled or finished run can start children for at most that long. Cancelling a run does not
  cancel the children it already started; cancel each child task too.
- **Audit immutability** rests on grants ([ADR 0003](../adr/0003-one-postgres-cluster-per-owner-databases.md));
  a database administrator can change the log. Ship it to a central log store if that is not
  acceptable. There is no retention job.
- **Evaluation runs outside the isolation** ([ADR 0006](../adr/0006-evaluation-in-ci-first.md)):
  in CI, with a gateway key anyone who can open a merge request can read.
- **One Git token and one model key for every run.** `golem-run-secrets` holds a single
  `GOLEM_GIT_TOKEN` and `GOLEM_MODEL_KEY` for all runs of all agents. "Branch-only" rests on
  the bot's Developer role and protected default branches: the token can push or delete any
  unprotected branch of every context repository, other runs' proposals included. A key per
  agent, as the architecture draws it, is not built.
- **No TLS between processes.** In-cluster calls are plain HTTP; the UI sends users' access
  tokens to the edge and runs send run tokens to the MCP servers over it. Encrypt pod traffic
  with your CNI or a mesh if your policy requires it.

## How this page was checked

The run-marked blocks of the rotation sections were executed on the k3s cluster of
[install.md](install.md#how-this-guide-was-checked), after its first run: the running-runs
check (0), the run token key rotation with a new key id (the JWKS then held one key,
`golem-2`, and the next run's tool loading was audited `allow` by the MCP server), the edge
token rotation (an A2A call through the edge answered afterwards), and the UI key rotation (the
UI rolled out Ready). The GitLab, Jira, Mattermost and identity provider steps were not.
