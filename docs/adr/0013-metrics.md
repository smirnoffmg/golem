# 13. Metrics: RED everywhere, domain metrics, bounded labels, a port of their own

## Status

Accepted, 2026-09-25

## Context

The context diagram has "Grafana and Prometheus: platform metrics and alerts", and the
orchestrator's component list names "metrics", but no process exported any. Whoever is on call
could see a failing run only through a user's complaint, a stuck task or the logs of seven
processes. Several failures are silent by design: the reconciler logs a failed pass and runs
the next one; a merge request that cannot be opened leaves its run unsettled and its tasks
waiting in the outbox; the edge and the MCP servers refuse every request when the audit log is
unwritable, which from the outside looks like an outage and is really a security control doing
its job. ADR 0012 closed with "there is no metric of refusals yet".

Seven processes speak HTTP (the edge, the task service with three listeners, two MCP servers,
two adapters, the UI); the reconciler speaks HTTP to nobody. Paths carry task ids, run ids and
agent names; request bodies carry tool names chosen by an untrusted Job.

Sources:

- *Cloud Native DevOps with Kubernetes*, с. 350: the RED pattern (requests per second,
  percentage of errors, duration), measured the same way for every service, with Tom Wilkie's
  rationale that uniform monitoring reduces the cognitive load of whoever responds to an
  incident and lets repetitive tasks be automated.
- *Mastering API Architecture*, с. 140 (PDF 178): "Perhaps one of the biggest drawbacks of
  RED/golden signals is that it is easy to apply the rules and miss out on the wider context
  (or understanding) of the system."
- *Observability Engineering* (Majors, Fong-Jones, Miranda), с. 14 (PDF 38): "metrics-based
  tooling systems can deal with only low-cardinality dimensions at any reasonable scale", and
  high-cardinality information such as user or request ids is "almost always the most useful"
  for debugging, which is what traces and logs are for.
- prometheus_client 0.26 (installed): a `Counter` named `..._total` is exposed under that name;
  `make_asgi_app(registry)` serves one registry and negotiates the text and OpenMetrics formats;
  `ProcessCollector` and `PlatformCollector` register on a given registry.
- Prometheus Operator API reference: `ServiceMonitor` is `monitoring.coreos.com/v1`; a
  `Prometheus` selects ServiceMonitors by `serviceMonitorSelector` and
  `serviceMonitorNamespaceSelector`.

## Decision

**RED for every HTTP listener, one way** (`golem.metrics.Instrumented`): an ASGI middleware
around each listener's outermost app, so refusals by the guards in front of the routes (the
edge token, the MCP gate, the UI's headers) count too.

- `golem_http_requests_total{process, route, method, status_class}`
- `golem_http_request_duration_seconds{process, route, method}` (histogram, 5 ms to 30 s)

**Domain metrics**, because RED alone misses the wider context:

| Metric | Labels | Recorded by |
| --- | --- | --- |
| `golem_runs_started_total` | `agent` | task service (orchestrator port) |
| `golem_admission_rejections_total` | `reason`: `caller_concurrency`, `chain_concurrency`, `chain_budget`, `unknown_agent` | task service |
| `golem_run_reserved_cost_total` | `agent` | task service: the estimate admission reserved |
| `golem_run_outcomes_total` | `agent`, `outcome`: `succeeded`, `idle`, `invalid`, `failed`, `canceled` | reconciler (from the Job and its report), task service (cancel, failed launch) |
| `golem_run_duration_seconds` | `agent`, `outcome` (histogram, 10 s to 2 h) | the same, from `runs.created_at` to the final status |
| `golem_reconcile_pass_duration_seconds`, `golem_reconcile_pass_errors_total` | none | reconciler |
| `golem_outbox_pending` | none (gauge) | reconciler: tasks whose run has a final outcome they have not been told |
| `golem_proposals_pending`, `golem_proposals_settled_total` | none | reconciler: succeeded runs without, and with, a settled proposal |
| `golem_merge_request_failures_total` | none | reconciler |
| `golem_rate_limit_refusals_total` | `process`, `limit` (the ADR 0012 setting: `caller`, `auth_failures`, `login`, `start`, `webhook`, `command`) | edge, MCP servers, UI, adapters |
| `golem_authentication_failures_total` | `process` | edge, task service (edge token), MCP servers, UI (sign-in), adapters |
| `golem_audit_write_failures_total` | `process` | edge, MCP servers |
| `golem_policy_denials_total` | `reason` (the chain policy's `DenyReason`) | edge |
| `golem_mcp_tool_calls_total` | `group`, `tool`, `decision`: `allow`, `deny`, `unavailable` | MCP servers |

Each outcome is recorded where the status changes, by the statement that changed it
(`UPDATE ... WHERE status = 'running' RETURNING ...`), so a run is counted once however many
reconcilers or cancels race for it.

**Every label value comes from a closed set.** `route` is the route template
(`/internal/runs/{run_id}`), never the path; a path no route serves is `unmatched`. `method` is
one of the seven standard methods or `other`; `status_class` is `2xx` to `5xx`. `agent` is one
of the configured agents (the catalogs the task service registers, the agents with a GitLab
project in the reconciler) or `other`; `tool` is one of the group's tools or `other`, since a
Job writes the tool name; `outcome` takes the report's word only from `idle`, `invalid`,
`failed`, since the report is written inside an untrusted Job. Principals, task and run ids,
addresses, goals and tokens never become label values: they are in the audit log and in the
traces, which are built for high cardinality. Tests send ids, made-up agents and tool names and
check that no new label value appears.

**A metrics port of its own.** Every process serves `GET /metrics` on `GOLEM_METRICS_PORT`
(9090) from a listener that serves nothing else; its settings refuse to put it on a port its
callers use, and the reconciler, which had no listener, gets this one. Not on the public ports:
they face the ingress controller, runs and chat servers, and metrics show traffic, refusals and
agent names to whoever reads them. Not on the task service's internal read port either: the MCP
servers are admitted to it, and NetworkPolicy admits a caller to a port, not a path (ADR 0009).
One policy, `allow-metrics-scrape`, admits the namespace labelled `golem.dev/monitoring: "true"`
to port 9090 of every golem pod and to nothing else; no process's own policy opens 9090. The
network check gains a Prometheus stand-in in such a namespace that must reach the metrics ports
and must not reach the public ones, and the k3s test shows it failing when the policy is gone.

**Exposition.** Each process keeps its metrics in its own `CollectorRegistry` (with the process
and platform collectors), served by `make_asgi_app`. One registry per process instead of the
global one keeps tests independent; no multiprocess mode, since each process is one Python
process. `deploy/k8s/overlays/prometheus-operator` adds a `ServiceMonitor` for the `metrics`
port of every golem Service; `docs/operations/alerts.md` has `PrometheusRule` examples.

## Consequences

- Whoever is on call reads the same three numbers for every process, and the domain metrics
  answer what RED cannot: are runs finishing, is the outbox draining, is GitLab refusing merge
  requests, is admission turning callers away.
- An audit write failure is now visible as itself, not as a burst of 503s; alerting on any
  increase is a security signal, since every request is refused while it lasts.
- Series are bounded by configuration: a new agent adds series; a caller cannot.
- Refusal counters are per replica and are summed in queries, like the limits they count
  (ADR 0012).
- Metrics show nothing per user, run or task; for those, the audit log and the traces.
- The runtime Job exports no metrics: it lives minutes and speaks to the trace store
  (OpenTelemetry), and the orchestrator measures its outcome and duration from outside.
- Not built: alert routing and dashboards; metrics of outbound calls (the identity provider,
  GitLab, Atlassian) beyond the failures above; a probe on the reconciler's metrics port.
