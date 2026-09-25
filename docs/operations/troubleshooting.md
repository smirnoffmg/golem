# Troubleshooting

Symptom, then its causes, each with the check that tells it apart. The causes follow the code
paths named in each section. For alerts, start with [runbooks.md](runbooks.md).

Useful everywhere:

```sh
export GOLEM_RUNS_DSN=$(kubectl -n golem-system get secret golem-tasks -o jsonpath='{.data.GOLEM_RUNS_DSN}' | base64 -d)
psql "$GOLEM_RUNS_DSN" -c "select r.id, r.status, r.detail, r.proposal_settled_at, t.notified_at from run_tasks t join runs r on r.id = t.run_id where t.task_id = '<task id>'"
```

Run `psql` from a host that reaches Postgres at the address the pods use. A task's run id is
also in the task's metadata (`runId`, in the A2A answer); a run's Job is
`golem-run-<run id>` in `golem-jobs`, and its pod's log ends with the run's report as one line
of JSON.

## A task stays working

A task is `working` from the moment its run is admitted until the reconciler tells the task
service its outcome ([orchestrator/reconcile.py](../../src/golem/orchestrator/reconcile.py)).

| Check | Seen | Cause and fix |
| --- | --- | --- |
| `kubectl -n golem-jobs get job golem-run-<run id>` | `Running`, younger than `GOLEM_JOB_DEADLINE_SECONDS` | still working; nothing to do. A long run ends at its deadline at the latest |
| the same | the Job exists, its pod is not running | [a Job never starts](#a-jobs-pod-never-starts) |
| the same | `Complete` or `Failed`, and the run is still `running` in `golem_runs` | the reconciler is not finishing runs: [GolemReconcilePassesFailing](runbooks.md#golemreconcilepassesfailing), [GolemReconcilerDown](runbooks.md#golemreconcilerdown) |
| the query above | `succeeded`, `proposal_settled_at` empty | the merge request cannot be opened: [GolemMergeRequestsFailing](runbooks.md#golemmergerequestsfailing) |
| the query above | final (`failed`, or `succeeded` and settled), `notified_at` empty | the outcome is not delivered: [GolemOutboxGrowing](runbooks.md#golemoutboxgrowing) |
| the query above | no row | the task has no run: the task service crashed before recording it, or a restore put `golem_tasks` ahead of `golem_runs`. Nothing will finish it; cancel it and start again |

## A run is rejected

`rejected` means the task service refused to start the run; the task's message says why
([orchestrator/service.py](../../src/golem/orchestrator/service.py),
[orchestrator/admission.py](../../src/golem/orchestrator/admission.py)).

| Message | Cause | Fix |
| --- | --- | --- |
| `No agent named '<agent>' is registered.` | the agent is missing from `catalogs.yaml` | add it ([configuration.md](configuration.md#catalogs)), restart `tasks` |
| `Caller <caller> already has N running runs; the limit is N.` | `GOLEM_MAX_RUNS_PER_CALLER` | wait for runs to end, or raise it. Every Jira and every Mattermost user counts as one caller: the adapter |
| `Call chain <id> already has N running runs; ...` | `GOLEM_MAX_RUNS_PER_ROOT` | a chain of agents calling agents; the same |
| `Call chain <id> has spent ...; a run estimated at ... would exceed the budget of ...` | `GOLEM_BUDGET_PER_ROOT` | the same |
| `Run <id> for this message is already <status>.` | the same message id was sent again after its run ended (a client retry, a replayed command) | a new message id starts a new run; the UI and the adapters make one per action |
| `Could not launch run <id>: ...` | the Kubernetes API refused the Job | the text says why: `exceeded quota: golem-runs` (the `ResourceQuota` in `golem-jobs`), `forbidden` (the `golem-tasks` Role), a timeout (the `tasks` policy's API endpoint) |
| `no agent named: the request carries no tenant` | an A2A client without `params.tenant` | the edge refuses that first; seen only when calling the task service directly |

Refusals that never become a task come from the edge: [401, 403 or 429 at the edge](#401-or-429-at-the-edge).

## The task completed, but there is no merge request

The task's message is the reconciler's outcome
([orchestrator/merge_requests.py](../../src/golem/orchestrator/merge_requests.py)):

| Message | Means | Do |
| --- | --- | --- |
| `Run <id> succeeded; merge request: <url>` | the merge request exists | open it |
| `Run <id> succeeded and proposed no changes: <reasons>.` | the lead found nothing to do; each reason names a rule and why it is idle | often `all pending`: every target already has a proposal branch. A merge request closed without merging keeps its branch on purpose ([reviewing-proposals.md](../guide/reviewing-proposals.md#reject)) |
| `Run <id> succeeded and proposed no changes.` (no reasons) | the reconciler found no branch for the run in the project of `gitlab-projects.yaml` | if the run's log shows a pushed `branch`, the agent's `context.url` in its catalog and the project in `gitlab-projects.yaml` name different repositories: make them the same |
| `Run <id> succeeded, but no GitLab project is configured for agent '<agent>'; its branch was not proposed.` | the agent is missing from `gitlab-projects.yaml` | add it; open a merge request from the pushed branch by hand |
| `Run <id> failed: the change was rejected by validation: ...` | the role's change broke a rule, nothing was pushed | the agent author's problem ([writing-an-agent.md](../guide/writing-an-agent.md#what-validation-checks)) |
| `Run <id> failed.` | the run failed without a validator's verdict | its reason is only in the Job's report: [a run failed without a reason](#a-run-failed-without-a-reason) |

## A run failed without a reason

The task says `Run <id> failed.` and nothing more: the reconciler shows a run's own reasons
only for `invalid` and `idle` outcomes, since the report comes from untrusted code. Read the
report while the Job still exists (`GOLEM_JOB_TTL_SECONDS` after it ended, 600 s in the base):

```sh
kubectl -n golem-jobs logs job/golem-run-<run id> | tail -1
```

The `reasons` name the failure; [GolemRunsFailing](runbooks.md#golemrunsfailing) maps the
common ones to fixes. No report at all: [the termination message is empty](#the-termination-message-is-empty).

## A Job's pod never starts

```sh
kubectl -n golem-jobs describe job golem-run-<run id> | tail -20
kubectl -n golem-jobs get pods -l golem.dev/run-id=<run id>
kubectl -n golem-jobs get events --sort-by=.lastTimestamp | tail -20
```

| Seen | Cause | Fix |
| --- | --- | --- |
| pod `CreateContainerConfigError`, `secret "golem-run-secrets" not found` | the run Secret is missing in `golem-jobs` | create it ([configuration.md](configuration.md#secrets)) |
| the same, for `golem-run-<run id>-token` | the task service crashed between creating the Job and its token Secret | it heals when the same message is retried; otherwise the run fails at its deadline |
| `ImagePullBackOff` | `GOLEM_JOB_IMAGE` names an image the nodes cannot pull (kustomize `images` does not change it) | set it in the overlay |
| no pod, event `exceeded quota` | `ResourceQuota golem-runs` is full | fewer runs at once, or a larger quota |
| no pod, event `violates PodSecurity` | a changed Job template or namespace label | the task service builds the Job; check the namespace's `pod-security` labels |
| pod `Pending`, `Insufficient cpu` or `memory` | `GOLEM_JOB_CPU`, `GOLEM_JOB_MEMORY` larger than a node has free | size them, or add nodes |

A pod that never starts is failed by the Job's deadline, and the run with it.

## The termination message is empty

The runtime writes its report to `/dev/termination-log` and to its log just before it exits
([runtime/main.py](../../src/golem/runtime/main.py)). The reconciler reads the message from the
pod; without it, the run's outcome comes from the Job's status alone.

```sh
kubectl -n golem-jobs get pods -l golem.dev/run-id=<run id> \
  -o jsonpath='{.items[0].status.containerStatuses[0].state.terminated}'
```

| `terminated` shows | Cause |
| --- | --- |
| `"reason": "OOMKilled"` | the run exceeded `GOLEM_JOB_MEMORY` |
| pod `Evicted`, `ephemeral local storage usage exceeds` | the run wrote more than 2Gi to `/workspace` and `/tmp` together |
| nothing (no container ran), Job condition `DeadlineExceeded` | the run hit `GOLEM_JOB_DEADLINE_SECONDS`, or its pod never started |
| `"exitCode": 1` and a Python traceback in the log | the process died before writing a report (a broken image) |
| no pod at all | the Job's TTL removed it; the reconciler reads the report as soon as the Job ends, so this means it was not running then. A run whose branch was pushed is still settled as succeeded; one without a branch is failed |

## Jira labels start nothing, or no comment comes back

The Jira adapter's path ([adapters/jira.py](../../src/golem/adapters/jira.py)): signed webhook,
label mapped to an agent, `SendMessage` to the edge as `service:golem-jira-adapter` with a push
config, a terminal push, one comment.

| Check | Seen | Cause and fix |
| --- | --- | --- |
| `golem_authentication_failures_total{process="jira-adapter"}` grows | webhooks answered 401 | the webhook's secret differs from `GOLEM_JIRA_WEBHOOK_SECRET`, or Jira does not sign (the webhook needs a secret) |
| adapter log | nothing for the issue | the event is not `jira:issue_updated`, the label is not in `jira-labels.yaml`, or the webhook does not reach the adapter (ingress route `/jira/webhook`, the `jira-adapter` policy) |
| adapter log | `could not start <agent> for <issue>: ...` | the adapter got no service token: its client secret, the token URL, its egress to the identity provider |
| adapter log | `edge did not start <agent> for <issue>: 200 {..."code": -32041...not_allowed...}` | the call registry lacks `service:golem-jira-adapter` for the agent |
| the same, `Invalid push notification URL` | `GOLEM_PUBLIC_BASE_URL` of the adapter is not under the task service's `GOLEM_PUSH_ALLOWED_PREFIXES` | align them |
| the run finished, no comment, no adapter log | `GOLEM_PUSH_ALLOWED_PREFIXES` is empty: pushes are off, and the push config is silently dropped | set it (and `GOLEM_PUSH_CONFIG_KEY`) on the task service |
| adapter log `could not comment on <issue> for <task>: ...` | Jira refused the comment: `GOLEM_JIRA_USER`/`GOLEM_JIRA_TOKEN`, the account's permission on the project, the egress to Jira | fix it; this outcome is lost, since pushes are sent once |
| `golem_authentication_failures_total{process="jira-adapter"}` grows while runs finish | pushes answered 401: `GOLEM_PUSH_TOKEN_SECRET` changed while runs were running | those outcomes are lost; see [security.md](security.md#adapter-secrets) |

To find the run of an issue:

```sh
psql "$GOLEM_RUNS_DSN" -c "select id, status, detail, created_at from runs where caller = 'service:golem-jira-adapter' and message_id like 'jira:PROJ-123:%' order by created_at"
```

Mattermost has the same shape ([adapters/mattermost.py](../../src/golem/adapters/mattermost.py));
its private replies already say what went wrong:
[channels.md](../guide/channels.md#mattermost).

## 401 or 429 at the edge

The edge answers in JSON-RPC; the `error.message` is the reason
([edge/app.py](../../src/golem/edge/app.py), [edge/auth.py](../../src/golem/edge/auth.py)).

| Status, code | Message | Cause |
| --- | --- | --- |
| 401, -32040 | `bearer token required` | no `Authorization: Bearer` header |
| 401, -32040 | `unknown signing key '<kid>'`, `signing keys unavailable` | the edge has not got the provider's current keys: its egress to the JWKS URL |
| 401, -32040 | `wrong issuer`, `wrong audience` | the token's `iss` is not `GOLEM_OIDC_ISSUER`, or its `aud` lacks `GOLEM_OIDC_AUDIENCE`: the audience mapper ([install.md](install.md#4-set-up-the-identity-provider)) |
| 401, -32040 | `token expired`, `token not yet valid` | the token's time, or clocks |
| 401, -32040 | `token has no preferred username`, `service account token has no authorized client (azp)` | the provider's claims do not name a caller |
| 401, -32040 | `algorithm 'HS256' is not allowed`, `token has no key id`, `bad signature`, `malformed token` | not a token of the provider |
| 502, -32603 | `task service refused the edge` | `GOLEM_EDGE_TOKEN` differs between `golem-edge` and `golem-tasks`; the edge logs it as an error |
| 429, -32042 | `rate limited: retry after N s` | the caller's limit (`GOLEM_RATE_CALLER`, per replica), or, before the token is checked, the client address has too many failed authentications |
| 200, -32041 | `not_allowed: 'user:bob' may not call agent 'discovery'` | the call registry |
| 200, -32041 | `unknown_agent: agent 'x' is not registered` | the agent is not in the call registry |
| 200, -32041 | `depth_exceeded`, `cycle`, `malformed_chain` | an agent chain broke the chain policy |
| 200, -32603 | `audit log unavailable` | [GolemAuditWriteFailures](runbooks.md#golemauditwritefailures) |
| 200, -32603 | `task service unavailable` | the edge cannot reach `tasks:8000` |

A refusal of an authenticated caller is also an `audit_log` row (source `a2a-edge`) with
`result` = `deny: <reason>`; of a streak of 429s only the first is written, as
`deny: rate_limited`. Failed authentications are not audited at the edge; they are counted in
`golem_authentication_failures_total{process="edge"}`.

## A role's tool is denied

A run that names tools gets them from the MCP servers with its run token
([mcp/gate.py](../../src/golem/mcp/gate.py)); a refusal fails the run with `ToolLoadError` or
becomes an error the model sees. Every decision is an audit row:

```sh
psql -d golem_audit -c "select occurred_at, account, source, operation, result from audit_log where source like 'mcp:%' and result <> 'allow' order by id desc limit 20"
```

(as a superuser or `golem_audit_owner`: the services cannot read the log).

| `result` | Cause | Fix |
| --- | --- | --- |
| `deny: signing keys unavailable`, `deny: unknown signing key` | the MCP server has no current run key: it started before the task service, or the key rotated | restart the MCP servers ([install.md](install.md#8-apply)) |
| `deny: token expired` | the run outlived `GOLEM_JOB_DEADLINE_SECONDS` + 60 s | cannot happen for a live run; a replayed token |
| `deny: the run token does not grant tool group '<group>'` | `agent-tools.yaml` does not grant the group the role names | grant it, or remove it from the role |
| `deny: the run is canceled` (or `failed`, `succeeded`, `unknown`) | the run ended; its token is revoked | expected after a cancel |
| `deny: run status unavailable: ...` | the MCP server cannot reach the task service's `internal-read` port | the `mcp` policy, `GOLEM_TASK_SERVICE_URL` (port 8001) |
| `deny: tool '<tool>' is not in tool group '<group>'` | the model called a tool outside the group | nothing: the gate did its job |
| `deny: rate_limited` | too many failed authentications from one address | a misbehaving client |

A role naming a group that is missing from the MCP registry fails before the first model call:
`ToolAccessError: role '<role>' names tool groups [...] that are not in the MCP registry`.

## Netcheck fails

Each `FAIL` line names a check of one client ([deploy/k8s/netcheck/netcheck.sh](../../deploy/k8s/netcheck/netcheck.sh)):

| Line | Cause | Fix |
| --- | --- | --- |
| `FAIL settled:netcheck-canary...` | the client could still reach the canary after the wait: the CNI does not enforce egress policies at all | a CNI that enforces NetworkPolicy; nothing else here is trustworthy until this passes |
| `FAIL dns:...` | DNS egress is refused: your DNS pods are not `k8s-app: kube-dns` in `kube-system` (another DNS, node-local DNS) | patch `allow-dns` and `golem-run-egress` to your DNS pods |
| `FAIL open:<service>:<port>` | an allow is missing or not enforced as written: the client's policy, the target's ingress, or the target is not Ready | compare the policy with the matrix in [deploy/k8s/README.md](../../deploy/k8s/README.md#verify-the-network-after-deploy) |
| `FAIL closed:<target>` | the client reached something it must not: a policy too wide (an `ipBlock` covering the pod network, an empty selector), or a CNI that ignores ingress rules | fix the policy in your overlay; the base's tests reject such rules |
| a Job neither completes nor fails | its pod cannot start: busybox cannot be pulled | mirror the image and set it in the netcheck overlay |

`egress unfiltered for Ns` with N above 0 is not a failure: it is how long a new pod's traffic
goes unfiltered on your CNI ([ADR 0009](../adr/0009-deployment-on-kubernetes.md)).

## A process does not become ready

```sh
kubectl -n golem-system describe pod -l app.kubernetes.io/name=golem-<process> | tail -30
kubectl -n golem-system logs deploy/<process> --previous | tail -20
```

| Seen | Cause | Fix |
| --- | --- | --- |
| exits with `golem <process>: missing environment variables: ...` | a ConfigMap or Secret key is missing | the list is complete: add them all |
| exits with `... must be ...` | a value has the wrong form (a slash at the end of a URL, a non-Fernet key, a redirect URL that is not the base URL + `/callback`) | [configuration.md](configuration.md) |
| `tasks`, `reconciler` or `ui` exit with a database error | Postgres unreachable, the password, the role's grants; they create their tables at start | [install.md, step 3](install.md#3-create-the-databases-and-roles) |
| running, never Ready, probes time out | the CNI does not admit the kubelet's probes under the default deny (kube-router does; others may not) | allow the nodes' addresses to the probe ports in your overlay (environment-specific) |
| `tasks` exits with `golem task service: GOLEM_RUN_TOKEN_KEY_FILE must hold an unencrypted EC P-256 private key` | the `golem-run-token-key` Secret holds another kind of key | generate it as in [install.md](install.md#6-generate-the-secrets) |
| pod `ContainerCreating`, event `secret "golem-run-token-key" not found` | the Secret is missing | create it |
| `edge` exits with `golem edge: GOLEM_CARD_SIGNING_KEY_FILE must hold an unencrypted EC P-256 private key` | the `golem-card-signing-key` Secret holds another kind of key | generate it as in [install.md](install.md#6-generate-the-secrets) |
| pod `ContainerCreating`, event `secret "golem-card-signing-key" not found` | the Secret is missing (added with the directory of agents) | create it as in [install.md, step 8](install.md#8-apply) |

## Sign-in to the UI fails

The UI's error page says which step failed ([ui/app.py](../../src/golem/ui/app.py)):

| Page says | Cause |
| --- | --- |
| `The identity provider is unavailable. Try again later.` | discovery could not be read: `GOLEM_OIDC_DISCOVERY_URL`, the UI's egress, or discovery names another issuer or lacks PKCE S256 (the UI's log says which) |
| `The identity provider refused: <error>` | the provider's own error: the redirect URL is not registered, PKCE is not allowed for the client |
| `This sign-in expired or was not started here. Sign in again.` | more than ten minutes passed, the sign-in was started in another browser, or the browser dropped the `__Host-` cookie (the UI is not reached over https) |
| `Sign-in failed. Sign in again.` | the code exchange or the ID token check failed: the client secret, the ID token's audience or `azp` |
| `Too many requests. Try again in N s.` | `GOLEM_RATE_LOGIN` per address; behind an ingress, `GOLEM_TRUSTED_PROXIES` must name it or everyone shares one address |
| every page sends you back to sign in | the access token is refused at the edge (its audience) or cannot be refreshed; the edge's log shows the reason |

## How this page was checked

The `kubectl` commands were run on the k3s cluster of
[install.md](install.md#how-this-guide-was-checked) after its runs, with the placeholders
filled in (a `grep` that finds nothing and `--previous` on a pod that never restarted exit
with 1 there, as they will for you); the SQL against Postgres 17 holding the task states of
`scripts/ui_demo.py`; the PromQL with `promtool check rules`. The messages quoted are the
code's own, from the files linked in each section.
