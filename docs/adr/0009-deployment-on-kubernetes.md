# 9. Deployment on Kubernetes: two namespaces, default deny, least-privilege RBAC

## Status

Accepted, 2026-09-24

## Context

Six processes run from one image: the A2A edge, the task service (with the orchestrator port),
the reconciler, the Jira adapter and one platform MCP server per tool group (`tracker.read`,
`wiki.read`). Each run is a Job. ADR 0004 puts the security boundary outside the Job: default-deny
egress to five destinations, no secrets in the Job except its own. ADR 0007 delivers the run token
in a Secret in the Jobs namespace and asks for RBAC that cannot read it back. ADR 0002 says the
task service is not reachable from outside.

Until now the runs' NetworkPolicy existed only as `build_network_policy` in
`golem/orchestrator/jobs.py`, a function no process called, and there were no manifests for the
processes at all.

Sources:

- *Mastering API Architecture*, с. 221 (PDF 259): a `default-deny-all` policy
  (`podSelector: {}`, `Egress` and `Ingress`) locks down all traffic; "For a zero trust
  architecture, this would be the default for pods", followed by controlled allowances such as
  DNS lookup.
- *k8s_изнутри*, с. 333 (PDF 335): DNS egress to `kube-system` is opened for the pods that need
  it, on top of a deny.
- *DevOps: A Software Architect's Perspective*, PDF 200: "the least possible privileges should
  be granted to a user or role".
- *Cloud Native DevOps with Kubernetes*, с. 247: anyone with RBAC read access to Secrets in a
  namespace can read them.
- k3s documentation, <https://docs.k3s.io/networking/networking-services>: "K3s includes an
  embedded network policy controller. The underlying implementation is kube-router's netpol
  controller library", on unless `--disable-network-policy`; the tests rely on it.
- External Secrets Operator, <https://external-secrets.io/latest/api/externalsecret/>:
  `ExternalSecret` is `external-secrets.io/v1`.

## Decision

Kustomize manifests in `deploy/k8s/base`, an optional overlay `deploy/k8s/overlays/external-secrets`,
the operator's guide in `deploy/k8s/README.md`.

**Two namespaces.** `golem-system` holds the six processes; `golem-jobs` holds runs only. Runs are
untrusted, so they get a namespace where nothing can read a Secret, nothing reaches a database,
and a `ResourceQuota` bounds runs alone. Both are labelled `golem.dev/zone` for namespace
selectors and enforce the `restricted` Pod Security Standard, so an unhardened pod is refused at
admission, not found in review.

**Pods hardened like the Job.** Every Deployment runs as uid 10001 with `runAsNonRoot`, a
read-only root filesystem with an `emptyDir` at `/tmp`, all capabilities dropped, no privilege
escalation, the `RuntimeDefault` seccomp profile, and resource requests and limits. Only the two
processes that call the Kubernetes API mount a service account token; the others run under their
own accounts with `automountServiceAccountToken: false`, never as `default`.

**RBAC: two accounts, one namespace, no Secret reads.** Nothing is cluster-wide.

| Service account (golem-system) | In golem-jobs | Why |
| --- | --- | --- |
| `golem-tasks` | `jobs`: create, get, delete; `secrets`: create | launch a run, read it back on a name conflict, cancel it; create its token Secret |
| `golem-reconciler` | `jobs`: get, list; `pods`: get, list | a run's status and its report (the pod's termination message) |
| `golem-edge`, `golem-jira-adapter`, `golem-mcp` | nothing, and no token | they never call the Kubernetes API |

No account has get, list or watch on Secrets anywhere, so no process in `golem-system` can read a
run token back; the kubelet delivers it to the pod without a role.

**Network: default deny in both namespaces, then allows per process.** Each namespace has a
`default-deny-all` for ingress and egress. In `golem-jobs`, `golem-run-egress` lets run pods reach
exactly ADR 0004's five destinations (the edge, the MCP servers, the model gateway, the trace
store, the Git host) plus DNS to `kube-dns`; nothing reaches a run, including another run. In
`golem-system`, `allow-dns` opens DNS for Golem's pods and one policy per process holds its whole
surface:

| Process | Ingress from | Egress to |
| --- | --- | --- |
| edge | the ingress controller's namespace, runs, the Jira adapter | task service, Postgres, identity provider |
| tasks | edge, reconciler, MCP servers | Jira adapter (push), Postgres, Kubernetes API |
| reconciler | nothing | task service, Postgres, Kubernetes API, GitLab |
| jira-adapter | the ingress controller's namespace, task service | edge, identity provider, Jira |
| MCP servers | runs only | task service, Postgres, Jira and Confluence |

The Jira adapter calls only the edge, so it has no ingress to the task service. The Kubernetes
API is reached by its endpoint address, not the `kubernetes` Service's ClusterIP, because
policies see traffic after the Service address is translated.

**The manifests are the source of truth for network policy.** `build_network_policy` and its
types (`EgressAllowList`, `Destination`, `InCluster`, `Cidr`) are removed from `jobs.py`. They
described one policy for one namespace, no process applied them, and a second definition would
drift from the one that is deployed. Their guards move to tests over the rendered manifests: no
catch-all `ipBlock`, no empty `namespaceSelector`, exactly five destinations plus DNS for runs.
Generating the YAML from Python was rejected: operators patch YAML with kustomize, and a
generator would put a build step between the reviewed file and the cluster.

**Secrets are not in git.** The base references Secrets by name; `deploy/k8s/README.md` lists
every name and key. The `external-secrets` overlay creates each one with an `ExternalSecret` from
a `ClusterSecretStore` the operator provides. The run token signing key is mounted as a file;
the Job's Secret (`golem-run-secrets`) carries the model key, the branch-only Git token and the
trace store credentials, plus the model URL and name, because a Job's environment comes only
from Secrets.

**Placeholders per environment.** Addresses outside the cluster are `ipBlock`s in the TEST-NET
ranges (Postgres, Kubernetes API, identity provider, Atlassian, GitLab, model gateway, trace
store); the ingress controller's namespace is a label, `golem.dev/ingress-controller: "true"`;
URLs are `example.com`. The image is `golem`, overridden with kustomize `images`, with
`GOLEM_JOB_IMAGE` patched to match. The operator's overlay replaces all of them.

## Consequences

- Tests prove the decision against a real API server (k3s): every rendered object is accepted,
  every pod template passes `restricted` and an unhardened pod does not, `SubjectAccessReview`
  answers the RBAC table, and real traffic shows a run reaching an MCP server by its Service name
  and resolving names, but not a Postgres listener in `golem-system` nor another run; a pod
  wearing the run label outside `golem-jobs` cannot reach the MCP server either.
  `tests/test_k8s_render.py` runs every process's settings parser over the environment the
  manifests give it.
- NetworkPolicy works on addresses and ports, not paths. The MCP servers and the reconciler can
  reach the task service for `/internal/*`, and with that they reach `/a2a` too, where the task
  service trusts the edge's principal header. A compromised MCP server could start runs as any
  principal. Closing that needs the internal routes on a separate port, or the task service
  authenticating the edge; until then it is a known gap.
- SaaS endpoints change addresses, and an `ipBlock` cannot name a host: such destinations need an
  egress proxy with a fixed address or a CNI with DNS-based policies.
- The External Secrets Operator's controller can create Secrets in both namespaces; its store's
  conditions should admit only these two.
- A second Jobs namespace (another team zone) is another copy of the `golem-jobs` objects and a
  second pair of RoleBindings, and every `golem-system` policy that admits runs needs its zone
  label.
- Every new destination for a run is a change to `network-policies-jobs.yaml` and its tests,
  reviewed like code (ADR 0004).
