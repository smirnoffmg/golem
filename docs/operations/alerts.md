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
| `golem_run_outcomes_total` | counter | `agent`, `outcome` | final statuses: `succeeded`, `idle`, `invalid`, `failed`, `canceled` |
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
        # reconciler cannot reach it. Growing for a quarter of an hour, not a burst.
        - alert: GolemOutboxGrowing
          expr: min_over_time(golem_outbox_pending[15m]) > 0 and deriv(golem_outbox_pending[15m]) > 0
          for: 15m
          labels:
            severity: warning
          annotations:
            summary: "{{ $value }} task outcomes are waiting to be delivered"

        # Succeeded runs whose merge request cannot be opened keep their tasks waiting.
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

        # Admission refusing more than it admits: a runaway caller or chain, or limits too
        # tight for the load.
        - alert: GolemAdmissionRejectionSpike
          expr: |
            sum(rate(golem_admission_rejections_total[10m]))
              > 0.5 * sum(rate(golem_runs_started_total[10m])) + 0.05
          for: 10m
          labels:
            severity: warning
          annotations:
            summary: "admission is rejecting runs: {{ $value }}/s"

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
threshold on its own metrics cannot notice.
