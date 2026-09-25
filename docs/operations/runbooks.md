# Runbooks

One entry per alert of [alerts.md](alerts.md), and per symptom worth waking someone for. The
person on call does not have to be an expert: the job is to «классифицировать проблему, решить,
требует ли она принятия каких-то мер, и передать информацию дальше» ("classify the problem,
decide whether it needs action, and pass the information on") (*Cloud Native DevOps with
Kubernetes*, с. 368). Each entry says how to confirm, what to do, and whom to pass it to.

Conventions: queries are PromQL for your Prometheus; `kubectl` commands assume the namespaces
of the base; SQL runs with the process's own connection string, which the operator can read
from its Secret, for example:

```sh
export GOLEM_RUNS_DSN=$(kubectl -n golem-system get secret golem-tasks -o jsonpath='{.data.GOLEM_RUNS_DSN}' | base64 -d)
```

Run `psql` from a host that reaches Postgres at the address the pods use.

Deeper causes of single failed runs, and user-reported problems: [troubleshooting.md](troubleshooting.md).

## GolemAuditWriteFailures

**Severity** critical. **Means** the edge or an MCP server could not write an audit row and
refused the request it belonged to. While it lasts, that process refuses everything (fail
closed): an outage for users, and possibly someone making the audit log unwritable.

**Confirm.** Which process, and why:

```promql
sum by (process) (increase(golem_audit_write_failures_total[5m]))
```

```sh
kubectl -n golem-system logs deploy/edge --since=15m | tail -50
kubectl -n golem-system logs deploy/mcp-tracker-read --since=15m | tail -50
```

Then check the database path the process uses: its connection string, the role's grants, the
connection limit.

```sh
psql "$(kubectl -n golem-system get secret golem-edge -o jsonpath='{.data.GOLEM_AUDIT_DSN}' | base64 -d)" -c 'select 1'
psql -d golem_audit -c '\dp audit_log'      # as a superuser: golem_edge and golem_mcp need "a" (INSERT)
psql -d postgres -c "select usename, count(*) from pg_stat_activity where datname = 'golem_audit' group by usename"
```

**Act.**

| Cause | Fix |
| --- | --- |
| Postgres down or unreachable | the database team's incident; see [GolemTargetDown](#golemtargetdown) for the other processes |
| password rotated in Postgres, not in the Secret (or back) | align them ([security.md](security.md#other-secrets)), restart the process |
| grant missing (`\dp` shows no `a` for the role) | re-run the `GRANT` lines of `deploy/postgres/init.sql` as a superuser; find out who changed it |
| `CONNECTION LIMIT` of `golem_edge` or `golem_mcp` (10) reached | a burst of calls; raise the limit with `ALTER ROLE ... CONNECTION LIMIT`, look for the burst's source |
| `statement_timeout` (2 s) | the database is overloaded; the database team |

**Escalate** to security when the grant or the table changed and nobody on the platform team
did it.

## GolemOutboxGrowing

**Severity** critical. **Means** runs have finished, but their tasks are not being told: every
caller whose run ended in the last quarter hour is still waiting.

**Confirm.** The outbox, and what the task service answers the reconciler:

```sh
psql "$GOLEM_RUNS_DSN" -c "select t.task_id, r.id as run_id, r.status, r.created_at from run_tasks t join runs r on r.id = t.run_id where t.notified_at is null and (r.status = 'failed' or (r.status = 'succeeded' and r.proposal_settled_at is not null)) order by r.created_at limit 20"
kubectl -n golem-system logs deploy/tasks --since=15m | grep run-outcome | tail -20
kubectl -n golem-system logs deploy/reconciler --since=15m | tail -50
```

The reconciler marks a task told on 200 or 404 and retries everything else. In the task
service's log, `POST /internal/run-outcome` answers show which: `409` (the task service sees no
final outcome), `5xx`, or nothing at all (the reconciler cannot reach it).

**Act.**

| Cause | Fix |
| --- | --- |
| no `run-outcome` requests reach the task service | the reconciler's egress or the `tasks` policy's ingress on 8002; `GOLEM_TASK_SERVICE_URL` of the reconciler must name port 8002; run the network check |
| `500` answers | the task service cannot read `golem_runs` or `golem_tasks`: its log shows the database error |
| `409` for a run that is final | the task service reads a different `golem_runs` than the reconciler: compare `GOLEM_RUNS_DSN` in `golem-tasks` and `golem-reconciler` |
| the task service is down | [GolemTargetDown](#golemtargetdown) |

Once fixed the outbox drains by itself within a few passes.

**Escalate** to the platform team if none of these fit; the outbox holds everything, so no
outcome is lost while you wait.

## GolemMergeRequestsFailing

**Severity** warning (ticket). **Means** succeeded runs are waiting for their merge request:
the reconciler cannot find the branch or open the merge request in GitLab. It retries every
pass; their tasks stay working meanwhile.

**Confirm.**

```sh
kubectl -n golem-system logs deploy/reconciler --since=30m | grep -A3 'could not propose' | tail -30
psql "$GOLEM_RUNS_DSN" -c "select id, agent, created_at from runs where status = 'succeeded' and proposal_settled_at is null order by created_at"
```

The log names the GitLab call and its answer, for example
`GitLabError: GET /projects/product%2Fdiscovery-context/repository/branches: 401 ...`.

**Act.**

| Answer | Fix |
| --- | --- |
| `401` | `GOLEM_GITLAB_TOKEN` expired or revoked: [rotate it](security.md#other-secrets) |
| `403` | the bot lost its role in the project, or the project forbids it to open merge requests |
| `404` | the project path in `gitlab-projects.yaml` is wrong, or the project moved |
| `409`, `422` | GitLab refuses the merge request itself (for example the target branch is missing); open it by hand from the branch the log names and look at the project's settings |
| timeouts, `5xx` | GitLab is down or unreachable: the reconciler's egress to GitLab, then GitLab's owners |

**Escalate** to GitLab's administrators for outages and permission changes.

## GolemReconcilePassesFailing

**Severity** critical. **Means** the reconciler's passes raise: no run finishes, no merge
request opens, no outcome is delivered.

**Confirm.**

```sh
kubectl -n golem-system logs deploy/reconciler --since=15m | grep -B2 -A20 'reconcile pass failed' | tail -60
```

**Act** on the exception at the bottom of the traceback:

| Exception | Cause | Fix |
| --- | --- | --- |
| `psycopg.OperationalError` | `golem_runs` unreachable, password, connection limit (20) | as for the audit database above, with the `golem_runs` role |
| `kubernetes.client.exceptions.ApiException: (403)` | the `golem-reconciler` Role or RoleBinding in `golem-jobs` changed | re-apply your overlay |
| `urllib3 ... ConnectTimeoutError` to the API server | the API server's endpoint changed; the `reconciler` policy names the old address | [install.md, step 7](install.md#7-write-your-overlay), apply |
| `ApiException: (401)` | the service account token was not mounted or is invalid | restart the reconciler; check `automountServiceAccountToken` |

**Escalate** to the platform team with the traceback.

## GolemReconcilerDown

**Severity** critical. **Means** Prometheus has no reconciler target that answers: the
reconciler is not running, or Prometheus cannot reach its metrics port.

**Confirm.**

```sh
kubectl -n golem-system get pods -l app.kubernetes.io/name=golem-reconciler
kubectl -n golem-system describe deployment reconciler | tail -20
kubectl -n golem-system logs deploy/reconciler --previous | tail -20
```

**Act.** A pod in `CrashLoopBackOff` prints its reason on the last line: `golem reconciler:
missing environment variables: ...` (fix the ConfigMap or Secret), or a database error (as
above). A pod that is `Running` means the scrape is broken instead: the Prometheus namespace
lost its `golem.dev/monitoring: "true"` label, or the `ServiceMonitor` is not selected; see
[GolemTargetDown](#golemtargetdown).

**Escalate** to the platform team.

## GolemTargetDown

**Severity** critical. **Means** a Golem pod has not answered Prometheus for five minutes.

**Confirm** which one, then whether the pod or the scrape is broken:

```sh
kubectl -n golem-system get pods -o wide
kubectl -n golem-system get events --sort-by=.lastTimestamp | tail -20
```

**Act.**

| Seen | Cause | Fix |
| --- | --- | --- |
| `CrashLoopBackOff` | the process exits at start; `kubectl logs --previous` names the setting or the error | fix the ConfigMap or Secret |
| `CreateContainerConfigError` | a Secret or one of its keys is missing | create it ([configuration.md](configuration.md#secrets)) |
| `ImagePullBackOff` | the image reference or the registry's credentials | fix the overlay |
| `Running` and Ready, still down | the scrape cannot reach port 9090 | label the Prometheus namespace, check `allow-metrics-scrape` |
| `Running`, not Ready | the probe fails: for `tasks` its `internal-read` port, for the others their TCP port | the pod's log; for a CNI change, [troubleshooting.md](troubleshooting.md#a-process-does-not-become-ready) |

The edge and the task service down means every caller is refused; start with them.

**Escalate** to the cluster's administrators for node, CNI or registry problems.

## GolemRunsFailing

**Severity** critical. **Means** more than half of the runs of the last 30 minutes failed for
a reason other than validation: users get nothing back.

**Confirm.** The reasons of the latest failed runs: the report of each Job that still exists
(for `GOLEM_JOB_TTL_SECONDS` after it ended) holds them, while the task only says
`Run <id> failed.`:

```sh
kubectl -n golem-jobs get jobs -l app.kubernetes.io/name=golem-run --sort-by=.metadata.creationTimestamp | tail -10
kubectl -n golem-jobs logs job/golem-run-<run id> | tail -1
kubectl -n golem-jobs describe job golem-run-<run id> | tail -10
```

**Act** on the reason:

| Reason | Fix |
| --- | --- |
| `ToolLoadError: ... 401 Unauthorized` | the MCP servers do not know the run token's key: restart them ([install.md, step 8](install.md#8-apply)); after a key rotation, [security.md](security.md#run-token-signing-key) |
| `ToolLoadError: ... unavailable` | an MCP server is down or cannot reach the task service or the audit log |
| `GitError: git clone ...` | the Git token, or the run's egress to GitLab |
| `ModelResponseError`, `APITimeoutError`, `APIConnectionError` | the model gateway: its key, budget, or the run's egress to it |
| Job `DeadlineExceeded`, no report | the run took longer than `GOLEM_JOB_DEADLINE_SECONDS`, or its pod never started (next line) |
| pod `CreateContainerConfigError` | `golem-run-secrets` is missing in `golem-jobs` or lacks a key |

**Escalate** to the owners of the failing dependency (gateway, GitLab), or the platform team.

## GolemHttpErrors

**Severity** warning (ticket). **Means** more than 5 % of a process's requests answered 5xx for
ten minutes.

**Confirm** which routes:

```promql
sum by (route, status_class) (rate(golem_http_requests_total{process="mcp"}[5m]))
```

**Act** by process:

| Process | 5xx means | Look at |
| --- | --- | --- |
| `mcp` | `503`: the run status lookup or the audit write failed | the task service's `internal-read`, the audit database |
| `tasks` | the database under `golem_tasks` or `golem_runs` | its log |
| `ui` | the edge or the identity provider did not answer | the UI's log, the edge |
| `jira-adapter`, `mattermost-adapter` | `502`: the edge, the identity provider or Jira/Mattermost refused | the adapter's log |
| `edge` | rare: the edge answers most failures inside JSON-RPC with status 200 | its log |

**Escalate** with the route and the log lines.

## Admission rejects most runs

Not an alert: a [dashboard panel](alerts.md#dashboards-not-alerts). Look at it when callers
complain that their tasks are `rejected`.

```promql
sum by (reason) (rate(golem_admission_rejections_total[10m]))
```

| Reason | Means | Do |
| --- | --- | --- |
| `caller_concurrency` | one caller has `GOLEM_MAX_RUNS_PER_CALLER` runs running; for the adapters every chat or Jira user shares one caller | wait for runs to end, or raise the limit ([configuration.md](configuration.md#task-service-python--m-golemtasks)) |
| `chain_concurrency`, `chain_budget` | one call chain hit its limit | a runaway chain: find its root in `runs.root_run_id`, cancel its tasks |
| `unknown_agent` | the agent is not in the catalogs file | add it, or tell the caller the name |

## Every call to the edge is refused

A symptom, not an alert of its own: users report `401` in their tools, the UI sends everyone
to sign in, adapters log `edge did not start`. `golem_authentication_failures_total{process="edge"}`
jumps.

```sh
kubectl -n golem-system logs deploy/edge --since=15m | grep -i 'signing keys' | tail
```

- `could not fetch signing keys` at start: the edge cannot reach the identity provider's JWKS;
  it refuses every token until a fetch succeeds (it retries at most once a minute, on an
  unknown key id). Check the edge's egress to the provider and `GOLEM_OIDC_JWKS_URL`.
- The provider rotated its keys: the edge refetches on the first token with the new key id;
  nothing to do unless the fetch fails.
- An issuer or audience change at the provider: every token fails `wrong issuer` or
  `wrong audience` ([troubleshooting.md](troubleshooting.md#401-or-429-at-the-edge)).

**Escalate** to the identity provider's administrators.

## How this page was checked

The `kubectl` commands were run on the k3s cluster of
[install.md](install.md#how-this-guide-was-checked) after its runs, with the placeholders
filled in (a `grep` that finds nothing and `--previous` on a pod that never restarted exit
with 1 there, as they will for you); the SQL against Postgres 17 holding the task states of
`scripts/ui_demo.py`; the PromQL with `promtool check rules`. The messages quoted are the
code's own, from the files linked in each section.
