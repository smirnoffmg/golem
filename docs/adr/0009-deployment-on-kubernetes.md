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
- *Threat Modeling* (Shostack), с. 50 (PDF 86): when a trust boundary crosses an element rather
  than a data flow, "break that element into two". The task service was one element behind one
  port, crossed by three boundaries (the edge, the MCP servers, the reconciler).
- *Mastering API Architecture*, с. 221 (PDF 259): "Platform security underpins any assumptions
  that you make at the application level"; the task service's edge token is an application
  check that does not rest on the NetworkPolicy alone.
- *Designing Data-Intensive Applications* (Kleppmann), с. 376 (PDF 398): "A system of record,
  also known as source of truth, holds the authoritative version of your data ... If there is
  any discrepancy between another system and the system of record, then the value in the system
  of record is (by definition) the correct one." For a run's outcome that is `golem_runs`, not
  the body of a request that reports it.
- *Изучаем DDD — предметно-ориентированное проектирование*, с. 260: information carried by an
  event notification may already be stale when a subscriber gets it; to avoid races, the
  subscriber gets the current state with an explicit query.
- *k8s_изнутри*, с. 334 (PDF 336): "Мы настоятельно рекомендуем запускать тесты для проверки
  соответствия NetworkPolicy при оценке вашего провайдера CNI на совместимость со спецификациями
  сетевой безопасности Kubernetes." What k3s enforces says nothing about the operator's CNI.
- *Release It!*, с. 137: "A good test harness should be devious"; a socket "can be refused" or
  "can sit in a listen queue until the caller times out". kube-router rejects a denied
  connection, other CNIs drop it: the network check counts both as closed, and its negative
  tests break policies to show that it can fail.
- *Cloud Native DevOps with Kubernetes*, с. 114: when a readiness check fails, "Kubernetes уберет
  контейнер из списка подходящих сервисов". The network check's clients wear the labels of the
  processes they stand in for and are never Ready, so a Service never sends them real traffic.
- kube-router's rules in k3s v1.33.4, read with `iptables-save` in the test cluster: every pod's
  chain accepts traffic "when source is the pod's local node" (`--src-type LOCAL`), and a pod's
  chain exists only once the controller has handled the new pod; before that, nothing matches
  the pod's own traffic.
- uvicorn 0.53 (`uvicorn/server.py`): `Server.serve` wraps itself in `capture_signals`, which
  replaces the SIGINT and SIGTERM handlers with `signal.signal` and restores the previous ones
  on return.

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
| tasks | edge (`a2a`), MCP servers (`internal-read`), reconciler (`internal-write`) | Jira adapter (push), Postgres, Kubernetes API |
| reconciler | nothing | task service (`internal-write`), Postgres, Kubernetes API, GitLab |
| jira-adapter | the ingress controller's namespace, task service | edge, identity provider, Jira |
| MCP servers | runs only | task service (`internal-read`), Postgres, Jira and Confluence |

The Jira adapter calls only the edge, so it has no ingress to the task service. The Kubernetes
API is reached by its endpoint address, not the `kubernetes` Service's ClusterIP, because
policies see traffic after the Service address is translated.

**The task service has one listener per kind of caller.** NetworkPolicy admits a caller to an
address and port, never to a path, so behind one port the MCP servers and the reconciler could
reach `/a2a` too, where the task service trusts the edge's principal header, and the MCP
servers could post run outcomes. The process now serves three Starlette apps on three ports,
each with only its routes (any other path is a 404):

| Port | Name | Routes | Admitted |
| --- | --- | --- | --- |
| 8000 | `a2a` | agent card, `/a2a` | the edge |
| 8001 | `internal-read` | `GET /internal/run-keys`, `GET /internal/runs/{run_id}` | the MCP servers |
| 8002 | `internal-write` | `POST /internal/run-outcome` (a notification: the task id) | the reconciler |

The public and write apps share one A2A request handler, so an outcome reaches the task that
`/a2a` created. Three `uvicorn.Server`s run in one event loop under `asyncio.gather`; their
`capture_signals` is a no-op, and one loop signal handler stops all three, as does any one of
them stopping, so a half-served process never lingers. Three processes were rejected: the
in-memory state of a live task (ADR 0002) would have to be shared across them.

On top of the policy, the `a2a` port authenticates the edge: every request must carry
`X-Golem-Edge-Token` equal to `GOLEM_EDGE_TOKEN` (constant-time comparison), a secret only the
edge and the task service hold; anything else is a 401 with a JSON-RPC error, before the
principal header is read. The edge builds its forwarded headers from nothing, so a client's own
`X-Golem-Edge-Token` or `X-Golem-Principal` never passes. A missing or misapplied policy then
no longer lets a pod in the namespace act as any principal.

**The write port is a notification; the outcome is read from `golem_runs`.** The reconciler's
body names a task (`task_id`, plus its `run_id` as a hint for logs). The task service ignores
anything else in it and asks the orchestrator port for that task's run (`run_of_task`:
`run_tasks` joined with `runs`). The run's status and detail are the outcome; its `caller` and
`agent` are the owner and tenant that address the task in the task store, so a body cannot point
a delivery at another caller's task either. An outcome is final when the run failed, or
succeeded and its proposal is settled; that rule is one SQL predicate, `FINAL_OUTCOME` in
`golem/orchestrator/runs.py`, used both by the outbox to choose what to notify and by the read,
so the task service never accepts an outcome the outbox would not send. The answers: 200 when
the outcome is delivered or the task had already ended (at-least-once delivery stays
idempotent), 404 for a task with no run or no longer in the task store (final for the outbox, as
before), 409 when the run has no final outcome yet, which the outbox retries. A `canceled` run
has no outcome to deliver: its task was canceled through A2A, and a notification for it is a 409
that changes nothing.

**The manifests are the source of truth for network policy.** `build_network_policy` and its
types (`EgressAllowList`, `Destination`, `InCluster`, `Cidr`) are removed from `jobs.py`. They
described one policy for one namespace, no process applied them, and a second definition would
drift from the one that is deployed. Their guards move to tests over the rendered manifests: no
catch-all `ipBlock`, no empty `namespaceSelector`, exactly five destinations plus DNS for runs.
Generating the YAML from Python was rejected: operators patch YAML with kustomize, and a
generator would put a build step between the reviewed file and the cluster.

**What k3s proves, and a network check for the rest.** Three things the manifests only
rendered are now exercised on k3s (`tests/test_k8s_network_k3s.py`), in an environment built
the way an operator's overlay builds it: the Kubernetes API placeholder replaced by the address
in the `kubernetes` EndpointSlice, Postgres by an in-cluster Postgres, and the edge, the task
service and both MCP servers by busybox listening on their ports inside their own Deployments,
labels and probes untouched.

- Kubelet probes: every stand-in becomes Ready under the default deny and stays Ready past its
  liveness probe's first run, with no restart and no failed liveness probe, on the task
  service's HTTP probes and the others' TCP probes.
- The Kubernetes API: a pod under the `tasks` policy and the task service's account creates
  and deletes a suspended Job in `golem-jobs` through the `kubernetes` Service; the same pod
  under the MCP servers' policy cannot connect, and neither can it under a policy that names the
  Service's ClusterIP. The placeholder's place (the endpoint, not the ClusterIP) was right; no
  manifest changed.
- `deploy/k8s/netcheck`, the operator's check: Jobs wearing the labels of a run, the edge, an
  MCP server and the reconciler try the traffic matrix above against the real Services and
  print PASS or FAIL per check; a Job fails on any FAIL. It passes against the manifests, and it
  fails, naming the checks, when `golem-run-egress` is deleted (the run's names and opens) and
  when that policy also admits Postgres (the run's closed Postgres check, which nothing else
  guards). Every closed target is another check's open target, or a listener whose headless
  name resolves only while it is Ready, so a dead target cannot pass a closed check.

The operator runs the network check after every deploy that touches a policy or the CNI
(`deploy/k8s/README.md`, "Verify the network after deploy").

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
  wearing the run label outside `golem-jobs` cannot reach the MCP server either. Stand-ins for
  the edge, an MCP server and the reconciler each reach their own task service port through the
  `tasks` Service and are blocked on the other two. `tests/test_k8s_render.py` runs every process's settings parser over the environment the
  manifests give it.
- Verified on k3s only, and only for kube-router: kubelet probes are admitted because
  kube-router accepts everything from a pod's own node, which also means a `hostNetwork` pod on
  that node passes every ingress policy. Another CNI may admit probes differently or not at all
  (the Deployments then never become Ready, which the operator sees at once); the network check
  does not test probes, a rollout that ends Ready does.
- On k3s a new pod's egress is unfiltered for about a second after it starts, until kube-router
  has written the pod's rules: the test pods reached the API server in that window under a
  policy that denies it. A run is such a pod, so for that second it can reach whatever is not
  guarded by the destination's own ingress rules: the API server, Postgres outside the cluster,
  any host. In that second the Job runs only the platform's image, not anything a model
  directed, so the untrusted part of a run cannot use it; the operator's check measures the
  window on their CNI (`egress unfiltered for Ns`), and a CNI that programs rules before the
  pod starts closes it. The tests and the check wait it out on a canary no rule admits before
  they trust an open connection. The positive checks in `tests/test_k8s_manifests.py` do not,
  so they prove the ingress side of each allowed path; the egress side is proven by the network
  check's tests.
- Still unverified by any test: the ingress controller's namespace label, the Mattermost
  `ipBlock`, egress to the hosts outside the cluster other than Postgres (identity provider,
  Atlassian, GitLab, model gateway, trace store), and the API egress on a managed control plane
  outside the pod network. Each depends on addresses only the operator has.
- The gap of the first version is closed: behind one port, a compromised MCP server could start
  runs as any principal through `/a2a` and forge run outcomes. Now it reaches only run keys and
  run status, and the reconciler only run outcomes.
- The reconciler's port no longer trusts what it is told. A forged "succeeded" for a running run
  leaves the task working (409); a forged "failed" for a settled run completes the task with its
  real merge request; a body naming another caller or another run still reaches only the named
  task, with its own run's outcome. What a compromised reconciler, or anything else admitted to
  `internal-write`, can still do: make a true, final outcome reach its task before the outbox
  would have sent it (for a settled run that is only earlier, never different), and withhold
  notifications, so tasks stay working until a healthy reconciler's outbox catches up. Writing
  false outcomes now takes write access to `golem_runs`, which the reconciler has by design, so
  the reconciler's database role, not its port, is what bounds it.
- Every notification costs the task service one query to `golem_runs`; the write port depends
  on Postgres being reachable, and when it is not, the request fails and the outbox retries.
- `GOLEM_EDGE_TOKEN` is one more Secret key, shared by two Deployments; rotating it means
  changing both and restarting both, with a short window of refused forwards. It proves only
  that the caller holds the secret, not that it is the edge pod; a leaked token plus a policy
  gap would again allow forged principals.
- The task service's probes read the run keys on `internal-read`: they show the process and its
  loop are alive, not the `a2a` listener itself, which answers only to the edge.
- SaaS endpoints change addresses, and an `ipBlock` cannot name a host: such destinations need an
  egress proxy with a fixed address or a CNI with DNS-based policies.
- The External Secrets Operator's controller can create Secrets in both namespaces; its store's
  conditions should admit only these two.
- A second Jobs namespace (another team zone) is another copy of the `golem-jobs` objects and a
  second pair of RoleBindings, and every `golem-system` policy that admits runs needs its zone
  label.
- Every new destination for a run is a change to `network-policies-jobs.yaml` and its tests,
  reviewed like code (ADR 0004).
