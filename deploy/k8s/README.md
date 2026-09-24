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
| `golem-system` (`golem.dev/zone: system`) | `edge` (2 replicas), `tasks`, `reconciler`, `jira-adapter`, `mcp-tracker-read`, `mcp-wiki-read` | `restricted` |
| `golem-jobs` (`golem.dev/zone: jobs`) | runs only: the Jobs the task service launches, their token Secrets, the MCP registry, a `ResourceQuota` | `restricted` |

Every process listens on 8000 and its Service exposes 8000, except the reconciler, which has
no port and so no Service and no probe. Probes: `tasks` uses `GET /.well-known/agent-card.json`;
the edge, the Jira adapter and the MCP servers have no cheap unauthenticated route, so their
probes are TCP.

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
| golem-system | `golem-edge` | `GOLEM_AUDIT_DSN` (role `golem_edge`) | edge |
| golem-system | `golem-tasks` | `GOLEM_RUNS_DSN` (role `golem_runs`), `GOLEM_TASKS_DB_URL` (`postgresql+asyncpg://golem_tasks:...`), `GOLEM_PUSH_CONFIG_KEY` | task service |
| golem-system | `golem-run-token-key` | `key.pem`: unencrypted EC P-256 private key, the run token signing key (kid `GOLEM_RUN_TOKEN_KID`) | task service, mounted as a file |
| golem-system | `golem-reconciler` | `GOLEM_RUNS_DSN`, `GOLEM_GITLAB_TOKEN` (merge requests) | reconciler |
| golem-system | `golem-jira-adapter` | `GOLEM_OIDC_CLIENT_SECRET`, `GOLEM_JIRA_TOKEN`, `GOLEM_JIRA_WEBHOOK_SECRET`, `GOLEM_PUSH_TOKEN_SECRET` | Jira adapter |
| golem-system | `golem-mcp-tracker-read`, `golem-mcp-wiki-read` | `GOLEM_MCP_UPSTREAM_TOKEN` (Jira, Confluence), `GOLEM_AUDIT_DSN` (role `golem_mcp`) | MCP servers |
| golem-jobs | `golem-run-secrets` (`GOLEM_JOB_SECRET`) | `GOLEM_MODEL_GATEWAY_URL`, `GOLEM_MODEL`, `GOLEM_MODEL_KEY`, `GOLEM_GIT_TOKEN` (branch-only), `OTEL_EXPORTER_OTLP_ENDPOINT`, `OTEL_EXPORTER_OTLP_HEADERS` | every run's Job |

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
| `192.0.2.10/32:5432` | policies `edge`, `tasks`, `reconciler`, `mcp` | Postgres. For an in-cluster Postgres, replace the `ipBlock` with a `namespaceSelector` and `podSelector`. |
| `192.0.2.1/32:443,6443` | policies `tasks`, `reconciler` | The Kubernetes API server (see below). |
| `198.51.100.10/32:443` | policies `edge`, `jira-adapter` | The identity provider: JWKS for the edge, client credentials for the adapter. |
| `198.51.100.20/32:443` | policies `jira-adapter`, `mcp` | Jira and Confluence. |
| `198.51.100.30/32:443` | policy `reconciler`; `:443,22` in `golem-run-egress` | GitLab: merge requests, and the runs' clone and push. |
| `203.0.113.10/32:4000` | `golem-run-egress` | The model gateway. |
| `203.0.113.20/32:443` | `golem-run-egress` | The trace store's OTLP endpoint. |
| `golem.dev/ingress-controller: "true"` | policies `edge`, `jira-adapter` | Label your ingress controller's namespace with it, or patch the selector. The base has no Ingress objects: route the public host to Service `edge` and the Jira webhook path to `jira-adapter`. |
| `https://idp.example.com/...`, `golem-edge` | `golem-edge-env`, `golem-jira-adapter-env` | Issuer, JWKS, discovery and token URLs; the edge's audience; the adapter's client id (also in the call registry as `service:golem-jira-adapter`). |
| `https://golem.example.com` | `golem-edge-env`, `golem-tasks-env` | `GOLEM_PUBLIC_BASE_URL`: the edge's public address. |
| `https://jira.example.com`, `https://confluence.example.com`, `data-center`, empty `*_USER` | adapter and MCP ConfigMaps | Atlassian hosts; for Cloud set `GOLEM_MCP_JIRA_DEPLOYMENT: cloud` and the account email in `GOLEM_JIRA_USER` / `GOLEM_MCP_UPSTREAM_USER`. |
| `https://gitlab.example.com`, `golem-config` | reconciler, `golem-config` | GitLab URL; the call registry, catalogs, agent tool grants, GitLab projects, Jira labels. |
| `GOLEM_CATALOGS_DIR: /app/examples` | `golem-edge-env` | Agent cards come from the example catalogs baked into the image; mount your catalogs instead. |
| `ResourceQuota golem-runs`, resources, replicas | `namespaces.yaml`, Deployments | Sizing. |

An `ipBlock` cannot name a host, and SaaS endpoints (Atlassian Cloud, a hosted model API)
change addresses. For those, send the traffic through an egress proxy with a fixed address, or
use a CNI with DNS-based policies, and put that address in the `ipBlock`.

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
