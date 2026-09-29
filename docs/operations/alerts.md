# Metrics and alerts

Every Golem process serves Prometheus metrics at `GET /metrics` on its metrics port (9090,
`GOLEM_METRICS_PORT`), reachable from the monitoring namespace only
([ADR 0013](../adr/0013-metrics.md), [deploy/k8s/README.md](../../deploy/k8s/README.md#metrics)).
With the Prometheus Operator, `deploy/k8s/overlays/prometheus-operator` scrapes them.

## Metrics

RED, for every HTTP listener of every process:

| Metric | Type | Labels |
| --- | --- | --- |
| `golem_http_requests_total` | counter | `process`, `route` (template or `unmatched`), `method`, `status_class` |
| `golem_http_request_duration_seconds` | histogram | `process`, `route`, `method` |

`process` is `edge`, `tasks`, `reconciler`, `jira-adapter`, `mattermost-adapter`, `mcp` or `ui`;
the two MCP servers are told apart by the scrape's own target labels (`service`, `pod`).

Runs and the reconciler:

| Metric | Type | Labels | Meaning |
| --- | --- | --- | --- |
| `golem_runs_started_total` | counter | `agent` | runs admitted and recorded |
| `golem_admission_rejections_total` | counter | `reason` | run starts refused: `caller_concurrency`, `chain_concurrency`, `chain_budget`, `unknown_agent` |
| `golem_run_reserved_cost_total` | counter | `agent` | estimated cost reserved at admission |
| `golem_run_outcomes_total` | counter | `agent`, `outcome` | final statuses: `succeeded`, `idle`, `reported`, `invalid`, `failed`, `canceled` |
| `golem_run_duration_seconds` | histogram | `agent`, `outcome` | from recording a run to its final status |
| `golem_reconcile_pass_duration_seconds` | histogram | | one reconcile pass |
| `golem_reconcile_pass_errors_total` | counter | | passes that raised |
| `golem_outbox_pending` | gauge | | tasks whose run is final but who have not been told |
| `golem_proposals_pending` | gauge | | succeeded runs whose merge request is not settled |
| `golem_proposals_settled_total` | counter | | succeeded runs whose merge request (or none) was settled |
| `golem_merge_request_failures_total` | counter | | failed attempts to find a branch or open its merge request |

Guards at the entry points:

| Metric | Type | Labels | Meaning |
| --- | --- | --- | --- |
| `golem_rate_limit_refusals_total` | counter | `process`, `limit` | 429s by limit (ADR 0012) |
| `golem_authentication_failures_total` | counter | `process` | missing or failed credentials |
| `golem_audit_write_failures_total` | counter | `process` | audit rows not written; the request was refused |
| `golem_policy_denials_total` | counter | `reason` | calls refused by the edge's chain policy |
| `golem_mcp_tool_calls_total` | counter | `group`, `tool`, `decision` | tool calls: `allow`, `deny`, `unavailable` |

`agent` is a configured agent or `other`; `tool` is a tool of its group or `other`. Nothing
names a user, a task or a run: follow those in the audit log and the traces.

## Alert examples

An alert should mean one simple thing: «прямо сейчас требуются действия со стороны человека»
("a human has to act right now"); «Если действовать не нужно, не нужны и уведомления. Если
что-то следует предпринять, но не сейчас, уведомление можно послать по электронной почте или в
чате» ("if no action is needed, no alert is needed; if something should be done, but not now,
send an email or a chat message") (*Cloud Native DevOps with Kubernetes*, с. 368). The rules
below follow that test:

| Severity | Means | Route it to |
| --- | --- | --- |
| `critical` | users are affected now and nothing fixes itself | the on-call person's pager, at any hour |
| `warning` | something needs a person, but not tonight | a ticket or the team's channel, never a pager |

Each alert has an entry in [runbooks.md](runbooks.md): what it means, how to confirm it, what to
do, when to escalate. Signals that need no action at all are
[dashboards, not alerts](#dashboards-not-alerts).

A `PrometheusRule` for the Prometheus Operator (API `monitoring.coreos.com/v1`); the thresholds
are starting points, sized for the base's defaults. Without the operator, the `groups` are a
plain Prometheus rule file.

```yaml
apiVersion: monitoring.coreos.com/v1
kind: PrometheusRule
metadata:
  name: golem
  namespace: golem-system
spec:
  groups:
    - name: golem
      rules:
        # A security signal: while the audit log cannot be written, the edge and the MCP
        # servers refuse every request (fail closed). Any occurrence is worth a look: an
        # unreachable database, a revoked grant, or someone making the log unwritable.
        - alert: GolemAuditWriteFailures
          expr: sum by (process) (increase(golem_audit_write_failures_total[5m])) > 0
          labels:
            severity: critical
          annotations:
            summary: "{{ $labels.process }} could not write audit rows and refused requests"

        # Tasks wait for their outcome: the task service refuses notifications, or the
        # reconciler cannot reach it. Growing for a quarter of an hour, not a burst: every
        # caller whose run finished is left waiting until a person fixes it.
        - alert: GolemOutboxGrowing
          expr: min_over_time(golem_outbox_pending[15m]) > 0 and deriv(golem_outbox_pending[15m]) > 0
          for: 15m
          labels:
            severity: critical
          annotations:
            summary: "{{ $value }} task outcomes are waiting to be delivered"

        # Succeeded runs whose merge request cannot be opened keep their tasks waiting. Nothing
        # is lost (every pass retries), so a ticket, not a page.
        - alert: GolemMergeRequestsFailing
          expr: increase(golem_merge_request_failures_total[15m]) > 0 and golem_proposals_pending > 0
          for: 15m
          labels:
            severity: warning
          annotations:
            summary: "merge requests are failing while succeeded runs wait for them"

        # The reconciler logs a failed pass and runs the next one; failing every pass means
        # no run finishes.
        - alert: GolemReconcilePassesFailing
          expr: sum(increase(golem_reconcile_pass_errors_total[10m])) > 3
          labels:
            severity: critical
          annotations:
            summary: "reconcile passes are failing"

        - alert: GolemReconcilerDown
          expr: absent(up{job="reconciler"} == 1)
          for: 5m
          labels:
            severity: critical
          annotations:
            summary: "no reconciler is scraped: runs are not being finished"

        # A process that Prometheus cannot scrape is down or cut off; for the edge and the
        # task service that is every caller.
        - alert: GolemTargetDown
          expr: up{namespace="golem-system"} == 0
          for: 5m
          labels:
            severity: critical
          annotations:
            summary: "{{ $labels.job }} ({{ $labels.pod }}) is not answering its scrape"

        # Most runs fail for a platform reason (tools, Git, the model gateway, deadlines),
        # not a validator: users get nothing. At least five finished runs, so one bad run at
        # night does not page.
        - alert: GolemRunsFailing
          expr: |
            sum(increase(golem_run_outcomes_total{outcome="failed"}[30m]))
              > 0.5 * sum(increase(golem_run_outcomes_total[30m]))
            and sum(increase(golem_run_outcomes_total[30m])) >= 5
          labels:
            severity: critical
          annotations:
            summary: "most runs of the last 30 minutes failed"

        # RED: the share of 5xx per process.
        - alert: GolemHttpErrors
          expr: |
            sum by (process) (rate(golem_http_requests_total{status_class="5xx"}[5m]))
              / sum by (process) (rate(golem_http_requests_total[5m])) > 0.05
          for: 10m
          labels:
            severity: warning
          annotations:
            summary: "{{ $labels.process }}: {{ $value | humanizePercentage }} of requests fail"
```

`up{job="reconciler"}` assumes the job label the `ServiceMonitor` gives by default, the
Service's name. `absent` fires when the reconciler's target disappears altogether, which a
threshold on its own metrics cannot notice. `golem_run_outcomes_total` is recorded by the
reconciler and the task service; sum it over both.

## Dashboards, not alerts

These show a guard doing its job. A spike is worth a look during working hours, never a page:
the caller already got a refusal with a reason, and the platform is fine.

| Panel | Query | Look when |
| --- | --- | --- |
| admission rejections by reason | `sum by (reason) (rate(golem_admission_rejections_total[10m]))` | rejections outnumber started runs for long: a runaway caller or chain, or limits too tight for the load ([runbooks.md](runbooks.md#admission-rejects-most-runs)) |
| rate limit refusals | `sum by (process, limit) (rate(golem_rate_limit_refusals_total[5m]))` | one limit refuses steadily: a flood, or a limit sized below the replicas' share |
| authentication failures | `sum by (process) (rate(golem_authentication_failures_total[5m]))` | a rise at the edge after an identity provider change, at `tasks` after an edge token rotation |
| policy denials | `sum by (reason) (rate(golem_policy_denials_total[10m]))` | `not_allowed` after a call registry change |
| MCP tool calls | `sum by (group, tool, decision) (rate(golem_mcp_tool_calls_total[10m]))` | `deny` or `unavailable` growing |
| runs | `sum by (agent, outcome) (increase(golem_run_outcomes_total[1h]))`, `histogram_quantile(0.9, sum by (le, agent) (rate(golem_run_duration_seconds_bucket[1h])))` | capacity and quality over time |
| RED | `golem_http_requests_total`, `golem_http_request_duration_seconds` by `process`, `route` | always, as the first view |

Earlier versions of this page had an alert for an admission rejection spike; it failed the test above
(admission refusing a caller over its quota is the control working, and the caller is told
why), so it is a panel now.
