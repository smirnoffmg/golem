# Deploying Golem on Kubernetes

Kustomize manifests for the platform's processes. Decisions and their reasons:
[ADR 0009](../../docs/adr/0009-deployment-on-kubernetes.md).

```
deploy/k8s/
  base/                            namespaces, RBAC, config, workloads, network policies
  overlays/external-secrets/       optional: the base plus ExternalSecrets for every Secret
  overlays/prometheus-operator/    optional: the base plus a ServiceMonitor
  overlays/example/                an environment's overlay: every placeholder replaced
  netcheck/                        the network check to run after deploy (see below)
```

```sh
kubectl kustomize deploy/k8s/base                     # render
kubectl kustomize deploy/k8s/overlays/example         # render an environment's overlay
```

Apply an overlay of your own that replaces the placeholders below; never edit the base per
environment. `overlays/example` is such an overlay, and
[docs/operations/install.md](../../docs/operations/install.md) walks through an install with
it. The `external-secrets` and `prometheus-operator` overlays still carry the placeholders:
build your overlay on one of them instead of on `base`, never apply them alone.
`tests/test_k8s_render.py` checks that every process's settings parser accepts the
environment the manifests give it, and `tests/test_k8s_manifests.py` applies the base to k3s
and checks RBAC and network policies with real traffic; `tests/test_k8s_network_k3s.py` adds
kubelet probes, the Kubernetes API egress and the network check below. k3s is not your cluster:
run the network check after every deploy that changes a policy or the CNI.

## What runs where

| Namespace | Holds | Pod Security |
| --- | --- | --- |
| `golem-system` (`golem.dev/zone: system`) | `edge` (2 replicas), `tasks`, `reconciler`, `jira-adapter`, `mattermost-adapter`, `mcp-tracker-read`, `mcp-wiki-read`, `ui` (2 replicas) | `restricted` |
| `golem-jobs` (`golem.dev/zone: jobs`) | runs only: the Jobs the task service launches, their token Secrets, the MCP registry, a `ResourceQuota` | `restricted` |

Every process listens on 8000 and its Service exposes 8000, except the reconciler, which serves
nothing but its metrics and has no probe, and the task service, which has three listeners, one
per kind of caller (ADR 0009). Every process also serves `GET /metrics` on a port of its own,
9090 (`metrics`), which only the monitoring namespace reaches (ADR 0013, below):

| Port | Name | Routes | Admitted |
| --- | --- | --- | --- |
| 8000 | `a2a` | the agent card, `/a2a`; every request needs `GOLEM_EDGE_TOKEN` | the edge |
| 8001 | `internal-read` | `GET /internal/run-keys`, `GET /internal/runs/{run_id}` | the MCP servers |
| 8002 | `internal-write` | `POST /internal/run-outcome`: a notification naming a task; its outcome is read from `golem_runs` | the reconciler |
| 9090 | `metrics` | `GET /metrics` | the monitoring namespace |

`GOLEM_PORT`, `GOLEM_INTERNAL_READ_PORT`, `GOLEM_INTERNAL_WRITE_PORT` and `GOLEM_METRICS_PORT`
move them (a process refuses to start with two on one port); the Deployment's container ports,
the Service and the policies must move with them. Probes:
`tasks` uses `GET /internal/run-keys` on `internal-read`, since the `a2a` port answers only to
the edge; the UI's readiness probe gets its stylesheet; the edge, the adapters and the MCP servers
have no cheap unauthenticated route, so their probes are TCP.

## Image

Every container runs the one image, named `golem` in the base. `images` in kustomize does not
change environment values, so set the task service's `GOLEM_JOB_IMAGE` to the same reference:

```yaml
# overlays/prod/kustomization.yaml
resources: [../external-secrets]
images:
  - name: golem
    newName: registry.example.com/golem
    newTag: "1.4.0"
patches:
  - target: {kind: ConfigMap, name: golem-tasks-env}
    patch: |
      - op: replace
        path: /data/GOLEM_JOB_IMAGE
        value: registry.example.com/golem:1.4.0
```

## Secrets

Not in git. `overlays/external-secrets` creates each of them from a `ClusterSecretStore`
named `golem` that you provide (restrict it to these two namespaces with `spec.conditions`).
Without the operator, create the same Secrets with the same keys some other way.

| Namespace | Secret | Keys | Used by |
| --- | --- | --- | --- |
| golem-system | `golem-edge` | `GOLEM_AUDIT_DSN` (role `golem_edge`), `GOLEM_EDGE_TOKEN` | edge |
| golem-system | `golem-tasks` | `GOLEM_RUNS_DSN` (role `golem_runs`), `GOLEM_TASKS_DB_URL` (`postgresql+asyncpg://golem_tasks:...`), `GOLEM_PUSH_CONFIG_KEY`, `GOLEM_EDGE_TOKEN` | task service |
| golem-system | `golem-run-token-key` | `key.pem`: unencrypted EC P-256 private key, the run token signing key (kid `GOLEM_RUN_TOKEN_KID`) | task service, mounted as a file |
| golem-system | `golem-card-signing-key` | `key.pem`: another unencrypted EC P-256 private key, never the run token key's, that signs the agent cards (kid `GOLEM_CARD_SIGNING_KID`; public key at `/.well-known/golem-card-keys.json`) | edge, mounted as a file |
| golem-system | `golem-reconciler` | `GOLEM_RUNS_DSN`, `GOLEM_GITLAB_TOKEN` (merge requests) | reconciler |
| golem-system | `golem-jira-adapter` | `GOLEM_OIDC_CLIENT_SECRET`, `GOLEM_JIRA_TOKEN`, `GOLEM_JIRA_WEBHOOK_SECRET`, `GOLEM_PUSH_TOKEN_SECRET` | Jira adapter |
| golem-system | `golem-mattermost-adapter` | `GOLEM_OIDC_CLIENT_SECRET`, `GOLEM_MATTERMOST_BOT_TOKEN` (the bot account's access token), `GOLEM_MATTERMOST_COMMAND_TOKEN` (the slash command's token), `GOLEM_PUSH_TOKEN_SECRET` (its own, not the Jira adapter's) | Mattermost adapter |
| golem-system | `golem-ui` | `GOLEM_OIDC_CLIENT_SECRET`, `GOLEM_UI_DSN` (role `golem_ui`, database `golem_ui`), `GOLEM_UI_SESSION_KEY` (a Fernet key: `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`; rotating it signs everyone out) | web UI |
| golem-system | `golem-mcp-tracker-read`, `golem-mcp-wiki-read` | `GOLEM_MCP_UPSTREAM_TOKEN` (Jira, Confluence), `GOLEM_AUDIT_DSN` (role `golem_mcp`) | MCP servers |
| golem-jobs | `golem-run-secrets` (`GOLEM_JOB_SECRET`) | `GOLEM_MODEL_GATEWAY_URL`, `GOLEM_MODEL`, `GOLEM_MODEL_KEY`, optional `GOLEM_MODEL_TIMEOUT_SECONDS` (per call, default 120), `GOLEM_GIT_TOKEN` (branch-only), `OTEL_EXPORTER_OTLP_ENDPOINT`, `OTEL_EXPORTER_OTLP_HEADERS` | every run's Job |

`GOLEM_EDGE_TOKEN` is one value in both Secrets (the overlay reads it from one remote key): the
edge sends it with every forwarded request, and the task service trusts the principal header
only together with it. Any long random string will do, for example
`python -c "import secrets; print(secrets.token_urlsafe(32))"`; to rotate it, change both
Secrets and restart both Deployments.

The run Secret also carries the two non-secret model settings, because a Job's environment
comes only from Secrets. A key pair for the run tokens, and the same command again for the card signing key (a key of
its own, so the two rotate apart):

```sh
python -c "from golem.run_token import SigningKey; print(SigningKey.generate('golem-1').private_pem)"
```

The databases, roles and grants are `deploy/postgres/init.sql` (ADR 0003).

## Placeholders

Addresses in the TEST-NET ranges and `example.com` names are placeholders. Replace each in your
overlay.

| Placeholder | Where | Meaning |
| --- | --- | --- |
| `192.0.2.10/32:5432` | policies `edge`, `tasks`, `reconciler`, `mcp`, `ui` | Postgres. For an in-cluster Postgres, replace the `ipBlock` with a `namespaceSelector` and `podSelector`. |
| `192.0.2.1/32:443,6443` | policies `tasks`, `reconciler` | The Kubernetes API server (see below). |
| `198.51.100.10/32:443` | policies `edge`, `jira-adapter`, `mattermost-adapter`, `ui` | The identity provider: JWKS for the edge, client credentials for the adapters, sign-in and refresh for the UI. |
| `198.51.100.20/32:443` | policies `jira-adapter`, `mcp` | Jira and Confluence. |
| `198.51.100.30/32:443` | policy `reconciler`; `golem-run-egress` | GitLab: merge requests, and the runs' clone and push (HTTPS only). |
| `203.0.113.10/32:4000` | `golem-run-egress` | The model gateway. |
| `203.0.113.20/32:443` | `golem-run-egress` | The trace store's OTLP endpoint. |
| `203.0.113.30/32`, ingress `:8000`, egress `:443` | policy `mattermost-adapter` | The Mattermost server: it sends slash commands and serves the REST API the adapter posts to. If commands reach the adapter through the ingress controller, admit the controller's namespace instead and restrict the source to Mattermost at the ingress; route `/mattermost/command` to Service `mattermost-adapter`. |
| `golem.dev/monitoring: "true"` | policy `allow-metrics-scrape` | Label the namespace your Prometheus runs in, or patch the selector. It admits that namespace to port 9090 of every process and to nothing else. |
| `golem.dev/ingress-controller: "true"` | policies `edge`, `jira-adapter`, `ui` | Label your ingress controller's namespace with it, or patch the selector. The base has no Ingress objects: route the public host to Service `edge`, the Jira webhook path to `jira-adapter`, and the UI's host (TLS terminated at the ingress) to Service `ui`. |
| `https://idp.example.com/...`, `golem-edge` | `golem-edge-env`, `golem-jira-adapter-env`, `golem-mattermost-adapter-env` | Issuer, JWKS, discovery and token URLs; the edge's audience; the adapters' client ids (also in the call registry as `service:golem-jira-adapter` and `service:golem-mattermost-adapter`). |
| `https://mattermost.example.com`, `replace-with-team-id`, `discovery` | `golem-mattermost-adapter-env` | The Mattermost URL; the team ids (and optionally channel ids, `GOLEM_MATTERMOST_CHANNELS`) where `/golem` is enabled; the agents it may start. |
| `https://golem.example.com` | `golem-edge-env`, `golem-tasks-env` | `GOLEM_PUBLIC_BASE_URL`: the edge's public address. |
| `https://golem-ui.example.com`, `golem-ui` | `golem-ui-env` | The UI's public address and its redirect URL (both registered at the identity provider, with the address + `/` as the post-logout redirect) and its client id. It offers each person the agents the call registry lets them call. |
| `https://jira.example.com`, `https://confluence.example.com`, `data-center`, empty `*_USER` | adapter and MCP ConfigMaps | Atlassian hosts; for Cloud set `GOLEM_MCP_JIRA_DEPLOYMENT: cloud` and the account email in `GOLEM_JIRA_USER` / `GOLEM_MCP_UPSTREAM_USER`. |
| `https://gitlab.example.com`, `golem-config` | reconciler, `golem-config` | GitLab URL; the call registry, catalogs, agent tool grants, GitLab projects, Jira labels. |
| `GOLEM_CATALOGS_DIR: /app/examples` | `golem-edge-env` | Agent cards come from the example catalogs baked into the image; mount your catalogs instead. |
| `GOLEM_TRUSTED_PROXIES: 192.0.2.128/25` | `golem-edge-env`, `golem-ui-env`, `golem-jira-adapter-env` | The ingress controller's pod addresses, as CIDRs: only from them is `X-Forwarded-For` believed (see below). |
| `ResourceQuota golem-runs`, resources, replicas | `namespaces.yaml`, Deployments | Sizing. |

An `ipBlock` cannot name a host, and SaaS endpoints (Atlassian Cloud, a hosted model API)
change addresses. For those, send the traffic through an egress proxy with a fixed address, or
use a CNI with DNS-based policies, and put that address in the `ipBlock`.

### Metrics

Every process serves Prometheus metrics at `GET /metrics` on `GOLEM_METRICS_PORT` (9090), a
listener of its own that serves nothing else, never on a port its callers are admitted to
(ADR 0013). The `allow-metrics-scrape` policy admits the namespace labelled
`golem.dev/monitoring: "true"` to that port only; no process's own policy opens it. With the
Prometheus Operator, `overlays/prometheus-operator` adds a `ServiceMonitor` (API
`monitoring.coreos.com/v1`) that scrapes the `metrics` port of every Service labelled
`app.kubernetes.io/part-of: golem`; your `Prometheus` must select it. Without the operator,
scrape the same Service ports with Kubernetes service discovery. The metrics, their labels and
alert examples: [docs/operations/alerts.md](../../docs/operations/alerts.md).

### Rate limits and trusted proxies

Every public entry point has a token bucket rate limit ([ADR 0012](../../docs/adr/0012-rate-limits.md)).
Each is set by `<NAME>_PER_MINUTE` and `<NAME>_BURST` in the process's ConfigMap; the base sets
none, so the defaults apply:

| Process | `<NAME>` | Key | Default |
| --- | --- | --- | --- |
| edge | `GOLEM_RATE_CALLER` | authenticated principal, every `/a2a` call | 60/min, burst 20 |
| edge, MCP servers | `GOLEM_RATE_AUTH_FAILURES` | client address, failed authentications | 30/min, burst 10 |
| ui | `GOLEM_RATE_LOGIN` | client address, `GET /login` | 30/min, burst 10 |
| ui | `GOLEM_RATE_START` | session, `POST /tasks` | 10/min, burst 5 |
| jira-adapter | `GOLEM_RATE_WEBHOOK` | client address, the webhook | 300/min, burst 100 |
| mattermost-adapter | `GOLEM_RATE_COMMAND` | client address, the slash command | 120/min, burst 60 |

The buckets are in each replica's memory, so the effective limit is the limit times the
replicas (twice the table for `edge` and `ui`); the hard, shared limit on runs is the task
service's admission quota. A refused request gets 429 with `Retry-After`.

Behind the ingress controller the connection's peer is the controller, so the edge, the UI and
the Jira adapter need `GOLEM_TRUSTED_PROXIES` to name the controller's pods: the client is then
the right-most `X-Forwarded-For` address that is not a trusted proxy. The controller must put
the address it received the request from into the header, by appending it or by replacing
the header; either works, since only the right-most untrusted entry counts. List the controller's pods only, never the whole
pod network: Jobs call the edge and the MCP servers directly and must not choose their own
address with the header. If Mattermost's commands come through the ingress too, set the
variable in `golem-mattermost-adapter-env` as well.

### Reaching the Kubernetes API server

The task service and the reconciler use the in-cluster configuration, which connects to the
`kubernetes` Service's ClusterIP. NetworkPolicy is evaluated after that address is translated to
the API server's endpoint, so the egress rule must name the endpoint, not the ClusterIP:

```sh
kubectl get endpointslices -n default -l kubernetes.io/service-name=kubernetes \
  -o jsonpath='{range .items[*]}{.endpoints[*].addresses}{" "}{.ports[*].port}{"\n"}{end}'
```

Use those addresses (as `/32`) and port in the `tasks` and `reconciler` policies. On a managed
cluster whose API server sits outside the pod network, use the address and port your provider
documents for the control plane endpoint.

Tested on k3s (`tests/test_k8s_network_k3s.py`): a pod under the `tasks` policy with the
endpoint in place creates and deletes a Job in `golem-jobs` through the `kubernetes` Service;
the same pod under a policy that names the Service's ClusterIP instead cannot connect, nor can
it under the MCP servers' policy. The network check does not cover this egress (a client
wearing the task service's labels would join its Service); after deploy, a run that starts is
the proof, and `kubectl logs deploy/tasks -n golem-system` shows a connection timeout when the
address is wrong.

## Verify the network after deploy

A NetworkPolicy the API server accepts is not a policy the cluster enforces: the CNI enforces
it, some CNIs ignore it, and they differ in details that decide whether Golem works and whether
runs stay contained (ADR 0009). The tests prove the manifests on k3s (kube-router); on your
cluster, run `deploy/k8s/netcheck` after the platform is up (the edge, the task service, both
MCP servers Ready and Postgres reachable):

```yaml
# overlays/prod-netcheck/kustomization.yaml
resources: [../../netcheck]
configMapGenerator:        # the Postgres address of your policies, in both namespaces
  - {name: golem-netcheck, namespace: golem-system, behavior: merge,
     literals: [NETCHECK_POSTGRES=10.20.0.5:5432]}
  - {name: golem-netcheck, namespace: golem-jobs, behavior: merge,
     literals: [NETCHECK_POSTGRES=10.20.0.5:5432]}
# images: [{name: busybox, newName: registry.example.com/mirror/busybox}]  # if you mirror images
```

```sh
kubectl delete -k overlays/prod-netcheck --ignore-not-found   # the previous check, if any
kubectl apply -k overlays/prod-netcheck
kubectl get jobs -A -l app.kubernetes.io/component=netcheck -w  # until each client is Complete or Failed
kubectl logs -n golem-jobs job/netcheck-run
kubectl logs -n golem-system job/netcheck-edge                  # and netcheck-mcp, netcheck-reconciler
kubectl logs -n golem-netcheck-monitoring job/netcheck-monitoring
kubectl delete -k overlays/prod-netcheck
```

Each client Job wears the labels of one process, so that process's policy governs it, and
prints one `PASS` or `FAIL` line per check; the Job fails if any line is `FAIL`. A fifth
client stands in for Prometheus: it wears no process's labels and runs in a namespace of its
own, `golem-netcheck-monitoring`, labelled `golem.dev/monitoring: "true"` and without policies,
so only the processes' ingress rules decide what it reaches. All five Complete means the matrix
holds:

| Client (labels of) | Namespace | Must reach | Must not reach |
| --- | --- | --- | --- |
| a run | `golem-jobs` | DNS; `edge:8000`; `mcp-tracker-read:8000`; `mcp-wiki-read:8000` | `tasks` on 8000, 8001, 8002; Postgres; another run |
| the edge | `golem-system` | DNS; `tasks:8000` (`a2a`) | `tasks` on 8001, 8002; `mcp-tracker-read:8000` |
| an MCP server | `golem-system` | DNS; `tasks:8001` (`internal-read`) | `tasks` on 8000, 8002 |
| the reconciler | `golem-system` | DNS; `tasks:8002` (`internal-write`); Postgres | `tasks` on 8000, 8001 |
| Prometheus | `golem-netcheck-monitoring` | DNS; `metrics` (9090) of `edge`, `tasks`, `mcp-tracker-read`, `mcp-wiki-read` | `edge:8000`; `tasks` on 8000, 8001, 8002; `mcp-tracker-read:8000` |

The real Services are the targets. Stand-ins exist only where nothing real is expected: another
run (`netcheck-run-listener`, a run-labelled listener in `golem-jobs`), and a canary
(`netcheck-canary`, in a namespace `golem-netcheck` without policies). A check cannot pass by
accident:

- Every "must not reach" target is some other client's "must reach", or the run listener,
  whose name resolves only while it is Ready: a dead target fails a check, it never passes one.
- Some CNIs let a new pod's traffic through unfiltered until its rules are programmed. Each
  client first waits until it can no longer reach the canary, which only its own egress rules
  can refuse (`PASS settled:... (egress unfiltered for Ns)`); only then does it try the matrix.
  On k3s N is about 1 second. Any N above 0 on your cluster means a new pod, a run included, can
  reach anything not guarded by the destination's own ingress rules for that long.
- The Prometheus client needs no wait: nothing filters its own egress, and a new pod is not yet
  in the address sets the destinations' ingress rules admit, so early on its "must reach"
  checks are retried and its "must not reach" checks can only be refused.
- The client pods are never Ready (their readiness probe outlasts the Job's deadline), so the
  edge's Service never sends real traffic to the edge's stand-in.

Not covered: the metrics ports of the reconciler, the adapters and the UI (the same policy
admits them, and the tests prove it on k3s only for the four processes above), the ingress
controller to the edge, the UI and the Jira adapter (label your
controller's namespace and try the public URLs), the Mattermost server's address, the
destinations outside the cluster other than Postgres (identity provider, Atlassian, GitLab,
the model gateway, the trace store: their `ipBlock`s need their real endpoints, so a run that
completes is the check), and the Kubernetes API (above). Kubelet probes need no check: under
the default deny, a Deployment that becomes Ready and stays Ready shows the CNI admits them.
If the CNI does not, every Deployment stays unready and its liveness probe restarts it.

The Kubernetes project's own conformance tests measure the CNI itself, independent of Golem:
`sonobuoy run --e2e-focus=NetworkPolicy`.
