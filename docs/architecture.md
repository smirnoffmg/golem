# Architecture

The intended architecture of Golem in C4 notation: context, containers, and components of the
four containers that matter most. There is no code level on purpose. Nothing here is deployed;
the run lifecycle is implemented (see the README), the channel adapters, UI and evaluation
workflow are not.

**Pilot and target on the same diagrams.** Pale elements and dashed relationships are the
target picture and are not part of the pilot. Everything else is the pilot: entry over A2A,
Golem's own agents calling each other, results delivered as merge requests, and quality
evaluation on every merge request to an agent catalog. The target adds agents of other
platforms in both directions, long agent-to-agent delegation with waiting on a child task,
Temporal, and confirmation before a mutating operation through an A2A extension. See
[ADR 0005](adr/0005-pilot-and-target.md).

Diagrams use the C4 library bundled with PlantUML (`!include <C4/...>`).

## Level 1. Context

Five human roles, the organization's existing systems, and agents of other platforms. Golem
sits between them: it accepts tasks over A2A, reads agent catalogs and context from GitLab,
writes only branches and merge requests, calls models through a gateway, and leaves traces in
Langfuse.

```plantuml
@startuml
!include <C4/C4_Context>
AddElementTag("target", $bgColor="#C9D3DE", $fontColor="#333333", $borderColor="#8C9BAB")
AddRelTag("target", $lineStyle=DashedLine())
title Golem - context

Person(customer, "Run requester", "PM, analyst, tech lead: gives an agent a task, receives the result")
Person(gate, "Gate owner", "accepts or rejects the result in the merge request")
Person(author, "Agent author", "creates and changes an agent catalog through merge requests")
Person(platform_eng, "Platform engineer", "runtime, onboarding, incidents")
Person(sec, "Security and audit", "who did what, on whose behalf, through which agents - for any run")

System(platform, "Golem", "one runtime; an agent is a catalog of text; entry is A2A; a run is an A2A task and a Job; the result is a merge request")

System_Ext(ext_agents, "Agents of other platforms", "anything that speaks A2A", $tags="target")
System_Ext(gitlab, "GitLab and GitLab CI", "agent catalogs, MCP and call registries, process files, business context repositories, merge requests, evaluation pipeline")
System_Ext(jira, "Jira", "epics and issues: input and events")
System_Ext(confluence, "Confluence", "domain documents: read only")
System_Ext(gateway, "LiteLLM model gateway", "internal and external models, keys and budgets")
System_Ext(langfuse, "Langfuse", "OpenTelemetry traces, cost, golden sets and experiments")
System_Ext(keycloak, "Keycloak", "OIDC; RFC 8693 token exchange")
System_Ext(vault, "Vault", "secrets and service account tokens")
System_Ext(mattermost, "Mattermost", "bot commands, notifications")
System_Ext(monitoring, "Grafana and Prometheus", "platform metrics and alerts")

Rel(customer, platform, "gives a task, watches status", "A2A via UI, Mattermost, Jira label")
Rel(platform, customer, "result and notification", "merge request, Jira comment, Mattermost")
Rel(gate, gitlab, "accepts or rejects", "merge request, command")
Rel(author, gitlab, "agent catalog", "merge request")
Rel(platform_eng, platform, "onboarding, upgrades, stopping")
Rel(sec, platform, "audit log: initiator, agent chain, actions")
Rel(sec, langfuse, "traces")

BiRel(ext_agents, platform, "tasks in both directions", "A2A 1.0, OIDC", $tags="target")
Rel(platform, gitlab, "reads catalogs and context; writes branches; opens merge requests")
Rel(gitlab, platform, "merge request events; evaluation on catalog merge requests", "webhook; A2A from CI")
Rel(jira, platform, "events: label, status change", "webhook")
Rel(platform, confluence, "reads through a platform MCP server", "read only")
Rel(platform, gateway, "model calls", "key per agent")
Rel(platform, langfuse, "traces, cost, experiments", "OTLP")
Rel(platform, keycloak, "token validation, token exchange")
Rel(platform, vault, "service account secrets")
Rel(platform, mattermost, "notifications; bot commands")
Rel(platform, monitoring, "metrics and alerts")
@enduml
```

## Level 2. Containers

Two trust zones: the orchestrator namespace, the shared part that holds no team data, and the
team's Jobs namespace, where a run lives.

**Everything inside a Job is untrusted.** A model drives it, and the agent library's path
permissions do not apply once the backend can execute shell commands. The security boundary
therefore runs outside the Job ([ADR 0004](adr/0004-security-boundary-outside-the-job.md)).
A default-deny network policy opens exactly five destinations to a Job: the A2A edge, the
model gateway, Langfuse, the platform MCP servers, and GitLab. The Job's GitLab token can
write only its own branch. Platform MCP servers (Jira, Confluence, GitLab) hold their secrets
themselves and accept calls authorized by the run token; the diagram folds them into one
container. Metrics in Grafana and Prometheus are omitted.

**The A2A edge and the task service are two containers, not one gateway**
([ADR 0002](adr/0002-edge-and-task-service-split.md)).

| | A2A edge | Task service |
| --- | --- | --- |
| Does | protocol at the boundary, agent cards, authentication, token exchange, chain policy, per-caller rate limit, audit, outbound calls | task lifecycle: executor, task store, push notifications, resuming a task after a human answers |
| State | none; registries and cards are cached from GitLab | `golem_tasks` |
| On failure | **fails closed**: what it could not check, it does not pass. Keycloak or the call registry unavailable means rejection | tasks wait; the edge returns an error and clients retry |
| Changes | rarely; it is the security boundary | often, together with the orchestrator |

**One Postgres cluster, several databases, one owner each**
([ADR 0003](adr/0003-one-postgres-cluster-per-owner-databases.md)).

| Database | Owner | Other writers | Notes |
| --- | --- | --- | --- |
| `golem_tasks` | task service | none | receives input from agents of other platforms |
| `golem_runs` | orchestrator | none | cost, chain and quota accounting |
| `temporal`, `temporal_visibility` | Temporal | none | target only |
| `golem_audit` | a dedicated owner role that no service uses | A2A edge and MCP servers, `INSERT` only | `UPDATE`, `DELETE`, `TRUNCATE` are held by no service role |

**Protection from other parties' failures and overload** sits in four places, because every
incoming task spawns a Job in the team namespace:

- A2A edge: rate limit per caller;
- orchestrator: run admission with a limit on concurrent Jobs per caller and per root chain;
  refusal is `REJECTED` with a reason;
- Jobs namespace: `ResourceQuota` on Job count, CPU and memory as the last line;
- outbound calls to other platforms: timeout and circuit breaker, so a platform that stopped
  answering cannot hold Golem's Jobs.

```plantuml
@startuml
!include <C4/C4_Container>
AddElementTag("target", $bgColor="#C9D3DE", $fontColor="#333333", $borderColor="#8C9BAB")
AddRelTag("target", $lineStyle=DashedLine())
LAYOUT_TOP_DOWN()
title Golem - containers

Person(customer, "Run requester")
Person(gate, "Gate owner")
Person(author, "Agent author")
System_Ext(ext_agents, "Agents of other platforms", "A2A", $tags="target")
System_Ext(ci, "Agent catalog GitLab CI", "evaluation job is an A2A client; Pipelines must succeed")

System_Boundary(platform, "Golem (orchestrator namespace)") {
  Container(ui, "UI", "web, Keycloak login; A2A client", "agents and their cards, task submission, runs, decision queue")
  Container(adapters, "Channel adapters", "Python; A2A clients", "Jira and GitLab webhooks, Mattermost bot: event to A2A task; accepted merge request to task continuation; push to comment or message")
  Container(edge, "A2A edge", "Python; stateless; 2+ replicas", "the only door; Agent Card; authentication, token exchange; chain policy; per-caller rate limit; audit; outbound calls; fails closed")
  Container(tasks, "Task service", "Python, a2a-sdk, A2A 1.0", "task executor; task store; push notifications; resume after a human answer")
  Container(orch, "Orchestrator", "Python", "run admission and quotas; run, process and evaluation workflows; create Job, wait for accept, open merge request; run records; metrics")
  Container(temporal, "Temporal", "server", "history, timers, signals, retries - once long agent-to-agent calls appear", $tags="target")
  Boundary(pgc, "Postgres cluster (one)") {
    ContainerDb(db_tasks, "golem_tasks", "database; owner: task service", "A2A tasks, context, state history, push settings")
    ContainerDb(db_runs, "golem_runs", "database; owner: orchestrator", "run records: initiator, chain, version, attempts, cost; quotas")
    ContainerDb(db_temporal, "temporal, temporal_visibility", "two databases; owner: Temporal", "workflow history and search", $tags="target")
    ContainerDb(audit, "golem_audit", "database; INSERT only", "time, account, request, target, operation, result, source, chain")
  }
  Container(mcp, "Platform MCP servers", "Jira and Confluence read only; GitLab read", "own their secrets from Vault; called with the run token; every action audited")
}

System_Boundary(team, "Team zone (Jobs namespace, ResourceQuota)") {
  Container(job, "Runtime Job", "Kubernetes Job, Python agent runtime", "clones catalog and context; lead and roles; ask-an-agent; result to a branch; traces; in evaluation mode runs golden set and judge; dies after the run")
}

System_Ext(gitlab, "GitLab", "catalogs, MCP and call registries, context, merge requests")
System_Ext(atlassian, "Jira and Confluence")
System_Ext(mattermost, "Mattermost")
System_Ext(gateway, "LiteLLM model gateway")
System_Ext(langfuse, "Langfuse", "traces; golden sets and experiments")
System_Ext(keycloak, "Keycloak")
System_Ext(vault, "Vault")

Rel(customer, ui, "task, status")
Rel(gate, gitlab, "merge request review; accept")
Rel(author, gitlab, "merge request to an agent catalog")
Rel(gitlab, ci, "pipeline on catalog merge request")

Rel(ui, edge, "SendMessage, GetTask, CancelTask", "A2A")
Rel(adapters, edge, "task on behalf of the initiator", "A2A")
Rel(ci, edge, "task: evaluate catalog version; waits for the verdict", "A2A")
Rel(tasks, adapters, "state change", "push")
BiRel(ext_agents, edge, "tasks both ways; outbound behind a circuit breaker", "A2A 1.0", $tags="target")
BiRel(adapters, mattermost, "commands, notifications")
Rel(atlassian, adapters, "Jira webhook")

Rel(edge, keycloak, "token validation and exchange", "OIDC, RFC 8693")
Rel(edge, gitlab, "catalogs for cards; call registry", "cached")
Rel(edge, tasks, "authorized request with a hop token", "A2A")
Rel(edge, audit, "every call")
Rel(tasks, db_tasks, "tasks")
Rel(tasks, orch, "task to run; cancel; human answer")
Rel(orch, tasks, "state, artifacts")

Rel(orch, temporal, "workflows, signals", $tags="target")
Rel(orch, db_runs, "run records, quotas")
Rel(temporal, db_temporal, "history", $tags="target")
Rel(orch, job, "creates; stops", "Kubernetes API")
Rel(orch, gitlab, "opens merge requests")

Rel(job, edge, "ask an agent", "A2A, run token")
Rel(job, mcp, "tools from the role's list", "MCP, run token")
Rel(job, gitlab, "clone; write own branch", "narrow token")
Rel(job, gateway, "model calls", "agent key")
Rel(job, langfuse, "traces; golden sets and evaluation results", "OTLP, API")
Rel(mcp, atlassian, "read")
Rel(mcp, gitlab, "read")
Rel(mcp, audit, "every action")
Rel(vault, mcp, "secrets")
Rel(vault, job, "gateway key, branch token", "secrets operator")

Lay_R(ui, adapters)
Lay_D(edge, tasks)
Lay_R(tasks, orch)
Lay_R(orch, job)
Lay_D(orch, pgc)
@enduml
```

## Level 3. A2A edge

Everything about the protocol at the boundary and trust between agents. The edge knows who is
calling, on whose behalf, and whether that is allowed; it does not know what an agent does or
what state a task is in. Any component that could not verify something refuses.

```plantuml
@startuml
!include <C4/C4_Component>
AddElementTag("target", $bgColor="#C9D3DE", $fontColor="#333333", $borderColor="#8C9BAB")
AddRelTag("target", $lineStyle=DashedLine())
title A2A edge - components

Container(clients, "UI, channel adapters", "A2A clients")
System_Ext(ci, "GitLab CI", "evaluation job")
Container(job, "Runtime Job", "ask-an-agent")
System_Ext(ext_agents, "Agents of other platforms", "A2A", $tags="target")
Container(tasks, "Task service", "A2A 1.0")
ContainerDb(audit, "golem_audit", "INSERT only")
System_Ext(keycloak, "Keycloak")
System_Ext(gitlab, "GitLab", "catalogs, call registry")

Container_Boundary(edge, "A2A edge (stateless)") {
  Component(proxy, "A2A intake", "JSON-RPC and HTTP+JSON bindings", "parses the request; routes to an agent by tenant; forwards to the task service")
  Component(cards, "Agent Cards", "", "card built from the catalog: skills from SKILL.md; public card is minimal, extended card after authentication")
  Component(authn, "Authentication", "OIDC", "validates the Keycloak token on every inbound call")
  Component(rate, "Per-caller rate limit", "", "limit per account and per external platform; over the limit means refusal")
  Component(policy, "Chain policy", "", "allowed-call registry; depth limit; no cycles; agents and tools per request; budget per root run; refusal is REJECTED")
  Component(exchange, "Token exchange", "RFC 8693", "per hop: subject is the human initiator, actor is the agent; fallback is a signed run token")
  Component(outbound, "Outbound client", "a2a-sdk", "calls agents of other platforms from the registry; timeout; circuit breaker per platform", $tags="target")
  Component(auditor, "Audit", "", "records every call and refusal; agent and tool sequence in the response")
}

Rel(clients, proxy, "A2A")
Rel(ci, proxy, "A2A")
Rel(job, proxy, "A2A, run token")
Rel(ext_agents, proxy, "A2A", $tags="target")
Rel(proxy, cards, "agent card")
Rel(cards, gitlab, "agent catalog", "cached")
Rel(proxy, authn, "every call")
Rel(authn, keycloak, "token validation")
Rel(authn, rate, "account")
Rel(rate, policy, "within limit")
Rel(policy, gitlab, "call registry", "cached")
Rel(policy, exchange, "allowed call")
Rel(exchange, keycloak, "token exchange")
Rel(exchange, tasks, "request with hop token", "A2A")
Rel(policy, outbound, "call to an external agent", $tags="target")
Rel(outbound, ext_agents, "A2A", $tags="target")
Rel(proxy, auditor, "call and outcome")
Rel(policy, auditor, "refusal and reason")
Rel(auditor, audit, "insert")
@enduml
```

## Level 3. Task service

The A2A task lifecycle. An A2A task is the run: task states map onto run states, the result
comes back as a task artifact, and long runs answer with a push notification. The task service
is reachable only from the edge. In the pilot the human answer is an accepted merge request: an
adapter receives the webhook and sends a continuation as an ordinary A2A message in the same
task.

```plantuml
@startuml
!include <C4/C4_Component>
AddElementTag("target", $bgColor="#C9D3DE", $fontColor="#333333", $borderColor="#8C9BAB")
AddRelTag("target", $lineStyle=DashedLine())
title Task service - components

Container(edge, "A2A edge", "authorized requests")
Container(orch, "Orchestrator")
ContainerDb(db, "golem_tasks", "database in the shared cluster")
Container(adapters, "Channel adapters", "push receivers")
System_Ext(ext_agents, "Agents of other platforms", "push receivers", $tags="target")

Container_Boundary(tasks, "Task service") {
  Component(handler, "A2A handler", "a2a-sdk", "SendMessage, GetTask, CancelTask; a message to an existing task is a continuation")
  Component(executor, "Task executor", "AgentExecutor", "task to run, process or evaluation in the orchestrator; cancel; run states and artifacts to task events")
  Component(store, "Task store", "TaskStore on Postgres", "tasks, context, state history")
  Component(push, "Push notifications", "", "webhook to the caller on state change; retry with backoff")
  Component(resume, "Resume after human", "", "INPUT_REQUIRED, then the human answer in the same task, then accept or reject signal to the orchestrator")
  Component(hitl, "Operation confirmation", "A2A extension", "list of actions before a mutating operation; answer is allow or deny", $tags="target")
}

Rel(edge, handler, "A2A")
Rel(handler, executor, "new task, cancel")
Rel(handler, resume, "message to an INPUT_REQUIRED task")
Rel(executor, orch, "start and cancel")
Rel(orch, executor, "state, artifacts")
Rel(executor, store, "task events")
Rel(store, db, "persists")
Rel(resume, orch, "accept or reject signal")
Rel(handler, hitl, "confirmation answer", $tags="target")
Rel(hitl, orch, "operation allowed", $tags="target")
Rel(store, push, "state change")
Rel(push, adapters, "webhook")
Rel(push, ext_agents, "webhook", $tags="target")
@enduml
```

## Level 3. Orchestrator

The orchestrator has no launch API, event receiver or token issuer of its own; those live in
the edge, the task service and the adapters. It owns execution: admission, workflows, Jobs,
merge requests, run records, metrics. In the pilot, workflows run as plain code over
`golem_runs`; in the target they move onto Temporal.

**Evaluation workflow.** A merge request to an agent catalog starts a GitLab CI pipeline. Its
job is an A2A client: it submits "evaluate this catalog version" and waits for the verdict. The
orchestrator runs the version from the merge request branch over the golden set, runs the judge
in the same runtime image, compares the scores with the last merged version in Langfuse
experiments, and returns the verdict as a task artifact. Below the threshold the CI job fails
and "Pipelines must succeed" blocks the merge. An agent changes only through the same gate as
code.

```plantuml
@startuml
!include <C4/C4_Component>
AddElementTag("target", $bgColor="#C9D3DE", $fontColor="#333333", $borderColor="#8C9BAB")
AddRelTag("target", $lineStyle=DashedLine())
title Orchestrator - components

Container(tasks, "Task service", "task executor")
Container(temporal, "Temporal", "server", $tags="target")
ContainerDb(db, "golem_runs", "database in the shared cluster")
Container(job, "Runtime Job", "Kubernetes Job")
System_Ext(gitlab, "GitLab")
System_Ext(langfuse, "Langfuse", "experiments")

Container_Boundary(orch, "Orchestrator") {
  Component(admit, "Run admission", "", "concurrent Jobs per caller and per root chain; estimate against budget; refusal is REJECTED with a reason")
  Component(wf_run, "Run workflow", "", "catalog version and image; limits; terminal state with a reason; retry only for infrastructure")
  Component(wf_flow, "Process workflow", "", "stages from the process file; waits for accept; return limit; child runs")
  Component(wf_eval, "Evaluation workflow", "", "branch version over the golden set; judge; compare with merged version; verdict and threshold")
  Component(wait_child, "Child task wait", "", "run ends in waiting-for-task-X; on its completion a new Job continues", $tags="target")
  Component(act_job, "Job activity", "", "create Job with image, catalog ref and hop token; watch; clean up on timeout")
  Component(act_mr, "Merge request activity", "", "open merge request from the agent account with version and trace link")
  Component(runs, "Run records", "", "initiator, chain, team, versions, attempts, cost")
}

Rel(tasks, admit, "task to run, process or evaluation")
Rel(admit, wf_run, "admitted")
Rel(admit, wf_flow, "admitted")
Rel(admit, wf_eval, "admitted")
Rel(admit, runs, "quotas")
Rel(wf_flow, wf_run, "child run per stage")
Rel(wf_eval, act_job, "golden set runs and judge")
Rel(wf_eval, langfuse, "compare with merged version")
Rel(wf_eval, tasks, "verdict")
Rel(wf_run, act_job, "create and await Job")
Rel(wf_run, act_mr, "open merge request after validators")
Rel(wf_run, wait_child, "waiting for task X", $tags="target")
Rel(wf_run, tasks, "run state and artifacts")
Rel(act_job, job, "Kubernetes API")
Rel(act_mr, gitlab, "API")
Rel(wf_run, runs, "terminal state, cost")
Rel(wf_run, temporal, "history, timers, signals", $tags="target")
Rel(runs, db, "persists")
@enduml
```

## Level 3. Runtime Job

One image for every agent and every mode: run, golden-set run, judge. The loader clones the
catalog named in the environment; the lead is a pure function over a snapshot; path and tool
permissions protect against model mistakes but are not a security boundary; validators let only
a green result into the branch.

```plantuml
@startuml
!include <C4/C4_Component>
AddElementTag("target", $bgColor="#C9D3DE", $fontColor="#333333", $borderColor="#8C9BAB")
AddRelTag("target", $lineStyle=DashedLine())
title Runtime Job - components

Container(orch, "Orchestrator", "creates the Job, issues the hop token")
Container(edge, "A2A edge", "calls to other agents")
System_Ext(gitlab, "GitLab", "agent catalog, context, branch")
System_Ext(gateway, "LiteLLM model gateway")
System_Ext(langfuse, "Langfuse", "traces; golden sets")
Container(mcp, "Platform MCP servers", "Jira, Confluence, GitLab")

Container_Boundary(job, "Runtime Job (Python agent runtime image)") {
  Component(loader, "Catalog loader", "", "clones agents and context; validates catalog schema and version; in evaluation mode the merge request branch and golden set")
  Component(lead, "Lead", "pure function", "repository snapshot and open merge requests to zero or one run-role command")
  Component(role, "Role", "agent loop", "role prompt, skills, memory from the catalog; model and tool calls; step and token limits")
  Component(perms, "Path and tool permissions", "", "role writes one directory; tools are registry intersected with the role list; default deny; guards mistakes, not attacks")
  Component(tools, "MCP client", "", "platform server calls with the run token; result size limit")
  Component(ask, "Ask an agent", "A2A client, a2a-sdk", "synchronous consultation with a timeout; traceparent in metadata")
  Component(ask_long, "Delegate long work", "A2A client", "task with push notification; run ends waiting", $tags="target")
  Component(model, "Gateway client", "OpenAI-compatible", "model per role and data class; agent key")
  Component(trace, "Tracing", "OpenTelemetry GenAI", "invoke_agent, chat, execute_tool; catalog, image and model versions; masking")
  Component(validate, "Validators", "", "schema, links, immutability, empty sections, structural checks")
  Component(git, "Branch client", "", "unique branch per run; commits the result")
}

Rel(orch, loader, "catalog ref, target, mode, hop token", "Job environment")
Rel(loader, gitlab, "clone")
Rel(loader, lead, "snapshot")
Rel(lead, role, "command with target")
Rel(role, perms, "every write and tool call")
Rel(perms, tools, "allowed tools")
Rel(perms, ask, "allowed agents")
Rel(perms, ask_long, "allowed agents", $tags="target")
Rel(tools, mcp, "call", "MCP, run token")
Rel(ask, edge, "task to another agent", "A2A, run token")
Rel(ask_long, edge, "task to another agent", "A2A, run token", $tags="target")
Rel(role, model, "model call")
Rel(model, gateway, "request", "agent key")
Rel(role, trace, "steps, tokens, cost")
Rel(trace, langfuse, "trace", "OTLP")
Rel(role, validate, "result before commit")
Rel(validate, git, "green result only")
Rel(git, gitlab, "branch")
Rel(git, orch, "done: branch, version, cost", "Job exit")
@enduml
```

## Not shown

- A second team zone: scaling out is another Jobs namespace with its own accounts, sharing the
  orchestrator namespace.
- Network policies as separate objects; they are described in text at level 2.
- How golden sets are collected and labelled: a process question, not an architectural one.

## Open questions

- **Delegation in the identity provider.** RFC 8693 delegation (the `act` claim) in Keycloak
  is a preview feature with open defects. If the deployed version cannot do it, the token
  exchange component uses the fallback: a signed run token carrying the chain.
- **`a2a-sdk` in the internal package mirror**, and whether the available version implements
  A2A 1.0. The A2A handler, the outbound client and ask-an-agent depend on it.
- **OTLP ingestion in the trace store.** Whether the deployed Langfuse accepts traces over OTLP,
  and whether its experiments are enough to compare a branch version with the merged one.
- **How a CI job obtains an identity token** for its A2A call: the GitLab CI job ID token
  exchanged at Keycloak, or a pipeline service account. Neither has been tried.
- **Backup regime for agent-produced data.** Physical backup and point-in-time recovery apply to
  the whole cluster; a stricter regime for one database needs an additional logical backup. Run
  artifacts live in GitLab, so the requirement may belong there instead.
- **Audit log immutability versus cluster admins.** `golem_audit` is insert-only through role
  grants; a cluster administrator can bypass that. If the organization's security requirements
  do not accept this, the log must also be shipped to a central log collection system.
- **Task service replicas.** In `a2a-sdk` 1.1.5 a live task is held in the memory of the replica
  that created it. With a shared task store, cancel from another replica works (probed with two
  app instances over one store: the task becomes CANCELED and the cancel reaches the
  orchestrator); what stays replica-bound is subscribing to the event stream, which Golem does
  not offer. Not yet checked: what happens to the stale live task left on the first replica.
- **Idempotent run start — implemented in `orchestrator/runs.py`.** A client that retries after
  a timeout sends the same `messageId`, and every retry becomes a new A2A task. `start_run`
  returns the existing run for the same (caller, message id) and never starts a second Job.
  Transaction-scoped advisory locks on the caller and the root chain serialize the check and the
  insert, and a unique key in `runs` backs them up. Tested against Postgres 17 in
  testcontainers, including concurrent retries and a burst at the caller limit; with the locks
  removed exactly those two tests fail. Idempotent handling is "the correct first step in dealing
  with repeat messages" (*Cloud Architecture Patterns*, p. 34). Still open: wiring the task
  service to `start_run`, and where the cost estimate comes from.
- **Merge requests and the notification outbox — implemented in `orchestrator/reconcile.py` and
  `orchestrator/merge_requests.py`.** A succeeded run's result is branch
  `golem/<target_id>/<run_id>` in the agent's context repository. The reconciler, not knowing
  the target, finds the branch through the GitLab branches API (`search=/<run_id>$`, the exact
  shape checked locally) and opens the merge request idempotently: an open merge request for
  the source branch is reused, so a crash between opening it and recording it opens nothing
  twice. No branch means the run proposed nothing, and the task hears so. A succeeded run's
  tasks are notified only after its proposal is settled (`runs.proposal_settled_at`); while
  GitLab fails, the run stays unsettled, is retried every pass, and its notifications wait.
  Still open: a run whose agent has no configured GitLab project is retried forever (logged
  every pass), and the edge refetches signing keys synchronously, blocking its event loop for
  at most two seconds once a minute.
