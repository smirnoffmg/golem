# Deploying Golem on Kubernetes

Kustomize manifests for the platform's processes. Decisions and their reasons:
[ADR 0009](../../docs/adr/0009-deployment-on-kubernetes.md).

```
deploy/k8s/
  base/                            namespaces, RBAC, config, workloads, network policies
  overlays/external-secrets/       optional: the base plus ExternalSecrets for every Secret
```

```sh
kubectl kustomize deploy/k8s/base                     # render
kubectl apply -k deploy/k8s/overlays/external-secrets # with External Secrets Operator
```

Apply an overlay of your own that replaces the placeholders below; never edit the base per
environment. `tests/test_k8s_render.py` checks that every process's settings parser accepts the
environment the manifests give it, and `tests/test_k8s_manifests.py` applies the base to k3s
and checks RBAC and network policies with real traffic.

## What runs where

| Namespace | Holds | Pod Security |
| --- | --- | --- |
| `golem-system` (`golem.dev/zone: system`) | `edge` (2 replicas), `tasks`, `reconciler`, `jira-adapter`, `mattermost-adapter`, `mcp-tracker-read`, `mcp-wiki-read`, `ui` (2 replicas) | `restricted` |
| `golem-jobs` (`golem.dev/zone: jobs`) | runs only: the Jobs the task service launches, their token Secrets, the MCP registry, a `ResourceQuota` | `restricted` |

Every process listens on 8000 and its Service exposes 8000, except the reconciler, which has
no port and so no Service and no probe, and the task service, which has three listeners, one
per kind of caller (ADR 0009):

| Port | Name | Routes | Admitted |
| --- | --- | --- | --- |
| 8000 | `a2a` | the agent card, `/a2a`; every request needs `GOLEM_EDGE_TOKEN` | the edge |
| 8001 | `internal-read` | `GET /internal/run-keys`, `GET /internal/runs/{run_id}` | the MCP servers |
| 8002 | `internal-write` | `POST /internal/run-outcome`: a notification naming a task; its outcome is read from `golem_runs` | the reconciler |

`GOLEM_PORT`, `GOLEM_INTERNAL_READ_PORT` and `GOLEM_INTERNAL_WRITE_PORT` move them; the
Deployment's container ports, the Service and the `tasks` policy must move with them. Probes:
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
comes only from Secrets. A key pair for the run tokens:

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
| `198.51.100.30/32:443` | policy `reconciler`; `:443,22` in `golem-run-egress` | GitLab: merge requests, and the runs' clone and push. |
| `203.0.113.10/32:4000` | `golem-run-egress` | The model gateway. |
| `203.0.113.20/32:443` | `golem-run-egress` | The trace store's OTLP endpoint. |
| `203.0.113.30/32`, ingress `:8000`, egress `:443` | policy `mattermost-adapter` | The Mattermost server: it sends slash commands and serves the REST API the adapter posts to. If commands reach the adapter through the ingress controller, admit the controller's namespace instead and restrict the source to Mattermost at the ingress; route `/mattermost/command` to Service `mattermost-adapter`. |
| `golem.dev/ingress-controller: "true"` | policies `edge`, `jira-adapter`, `ui` | Label your ingress controller's namespace with it, or patch the selector. The base has no Ingress objects: route the public host to Service `edge`, the Jira webhook path to `jira-adapter`, and the UI's host (TLS terminated at the ingress) to Service `ui`. |
| `https://idp.example.com/...`, `golem-edge` | `golem-edge-env`, `golem-jira-adapter-env`, `golem-mattermost-adapter-env` | Issuer, JWKS, discovery and token URLs; the edge's audience; the adapters' client ids (also in the call registry as `service:golem-jira-adapter` and `service:golem-mattermost-adapter`). |
| `https://mattermost.example.com`, `replace-with-team-id`, `discovery` | `golem-mattermost-adapter-env` | The Mattermost URL; the team ids (and optionally channel ids, `GOLEM_MATTERMOST_CHANNELS`) where `/golem` is enabled; the agents it may start. |
| `https://golem.example.com` | `golem-edge-env`, `golem-tasks-env` | `GOLEM_PUBLIC_BASE_URL`: the edge's public address. |
| `https://golem-ui.example.com`, `golem-ui`, `discovery` | `golem-ui-env` | The UI's public address and its redirect URL (both registered at the identity provider, with the address + `/` as the post-logout redirect), its client id, and the agents it offers (each needs `user:*` in the call registry). |
| `https://jira.example.com`, `https://confluence.example.com`, `data-center`, empty `*_USER` | adapter and MCP ConfigMaps | Atlassian hosts; for Cloud set `GOLEM_MCP_JIRA_DEPLOYMENT: cloud` and the account email in `GOLEM_JIRA_USER` / `GOLEM_MCP_UPSTREAM_USER`. |
| `https://gitlab.example.com`, `golem-config` | reconciler, `golem-config` | GitLab URL; the call registry, catalogs, agent tool grants, GitLab projects, Jira labels. |
| `GOLEM_CATALOGS_DIR: /app/examples` | `golem-edge-env` | Agent cards come from the example catalogs baked into the image; mount your catalogs instead. |
| `GOLEM_TRUSTED_PROXIES: 192.0.2.128/25` | `golem-edge-env`, `golem-ui-env`, `golem-jira-adapter-env` | The ingress controller's pod addresses, as CIDRs: only from them is `X-Forwarded-For` believed (see below). |
| `ResourceQuota golem-runs`, resources, replicas | `namespaces.yaml`, Deployments | Sizing. |

An `ipBlock` cannot name a host, and SaaS endpoints (Atlassian Cloud, a hosted model API)
change addresses. For those, send the traffic through an egress proxy with a fixed address, or
use a CNI with DNS-based policies, and put that address in the `ipBlock`.

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
