# Golem

Golem is a runtime for autonomous agents. An agent is a catalog of text in Git: roles, skills
(`SKILL.md`), permissions and a process file. A run is an A2A task that the platform executes in
a short-lived Kubernetes Job, and the result is a merge request that a human accepts or rejects.
The name comes from Borges' poem: a clay figure brought to life by a written word. Here the word
is the catalog, and the figure is stopped by the platform, not by the model.

## Principles

- **A standard at every boundary.** A2A 1.0 is the only entry and the way agents call each other;
  MCP (2026-07-28) for tools; Agent Skills for agent descriptions; OIDC/OAuth 2.0 with RFC 8693
  token exchange for identity; OpenTelemetry GenAI conventions and W3C Trace Context for traces;
  an OpenAI-compatible model API through a LiteLLM gateway.
- **Everything inside a Job is untrusted.** The security boundary is outside it: default-deny
  egress, a branch-only Git token, platform MCP servers that hold the secrets.
- **The edge fails closed.** What it could not verify, it does not pass.
- **One owner per database.** One Postgres cluster, a database per owning service, an
  insert-only audit log.
- **Others' failures stay theirs.** Rate limits per caller, admission quotas per caller and per
  root chain, namespace quotas, timeouts and circuit breakers on outbound calls.
- **An agent changes through the same gate as code.** Every merge request to a catalog is
  evaluated against a golden set; below the threshold the pipeline fails.

## Containers

| Container | State | Responsibility |
| --- | --- | --- |
| A2A edge | none | the only door: agent cards, authentication, token exchange, chain policy, per-caller rate limit, audit, outbound calls |
| Task service | `golem_tasks` | A2A task lifecycle: executor, store, push notifications, resume after a human answer |
| Orchestrator | `golem_runs` | run admission and quotas; run, process and evaluation workflows; Jobs; merge requests |
| Runtime Job | none (ephemeral) | one image for every agent: loads the catalog, runs the lead and roles, writes a branch, emits traces |
| Channel adapters | none | UI, Jira and GitLab webhooks, chat bot, all as A2A clients |
| Platform MCP servers | none | Jira, Confluence and GitLab tools; hold their own secrets; audit every action |
| Postgres cluster | `golem_tasks`, `golem_runs`, `golem_audit` | one cluster, one owner per database |

Details and diagrams: [docs/architecture.md](docs/architecture.md). Decisions:
[docs/adr](docs/adr).

## Pilot and target

The pilot covers entry over A2A, Golem's own agents calling each other synchronously, results as
merge requests, and quality evaluation on catalog merge requests. The target adds agents of other
platforms in both directions, long agent-to-agent delegation with waiting on a child task,
Temporal for durable workflows, and confirmation before mutating operations through an A2A
extension. See [ADR 0005](docs/adr/0005-pilot-and-target.md).

## Repository layout

```
src/golem/
  catalog.py               agent catalog schema and loading
  runtime/lead.py          the lead: a pure function from a snapshot to the next role command
  runtime/snapshot.py      records of a context repository (Markdown + front matter) as a snapshot
  runtime/main.py          one Job = one run: clone, decide, brief, run the role, validate, push
  runtime/validate.py      checks a role's change before anything leaves the Job
  runtime/workspace.py     git operations (the token never appears in a URL or command line)
  runtime/deepagents_runner.py  a role on deepagents: no shell, writes only under its directory
  orchestrator/admission.py  run admission: Job quotas per caller and per root chain
  orchestrator/runs.py       idempotent run start on Postgres (schema.sql)
  orchestrator/service.py    the task service's orchestrator port: record a run, launch its Job
  orchestrator/jobs.py       hardened Job manifests, egress NetworkPolicy, the Kubernetes launcher
  orchestrator/reconcile.py  finished Jobs to run outcomes; the task notification outbox
  orchestrator/merge_requests.py  merge requests for succeeded runs (GitLab REST API)
  orchestrator/reconciler.py the reconciler process: a reconcile pass every interval
  settings.py              process settings parsed from the environment
  edge/policy.py           chain policy: allowed calls, depth, cycles, budget
  edge/cards.py            A2A Agent Cards generated from the catalog
  edge/auth.py, audit.py, app.py  the A2A edge: JWT check, audit row, forwarding; fails closed
  tasks/                   A2A task service: tasks in golem_tasks, run outcomes via an internal route
  evaluation/              the quality gate of catalog merge requests: golden set, runs, gate, CLI
  adapters/jira.py         Jira adapter: signed label webhook to SendMessage, task push to a comment
deploy/
  compose.yaml             local Postgres, edge, task service and reconciler
  postgres/init.sql        databases, roles and grants
examples/
  discovery/               an example agent catalog: kinds, roles, rules, role instructions
  discovery/evals/         its golden set; discovery/.gitlab-ci.yml runs the gate on merge requests
  context/                 an example context repository the discovery agent works on
docs/
  architecture.md          C4 diagrams (PlantUML)
  adr/                     architecture decision records
Dockerfile                 one image; the container role is chosen by the command
```

## Development

Requires [uv](https://docs.astral.sh/uv/) and Docker.

```sh
uv sync
uv run pytest               # needs Docker: Postgres tests run in testcontainers
uv run ruff check .
docker compose -f deploy/compose.yaml up --build
GOLEM_SMOKE=1 uv run pytest tests/test_compose_smoke.py   # builds, starts and removes compose
```

One image, four processes: `python -m golem.edge`, `python -m golem.tasks`,
`python -m golem.orchestrator.reconciler` and the Jira adapter `python -m golem.adapters`.
Each reads its settings from `GOLEM_*` environment variables (`src/golem/settings.py`) and
refuses to start with a list of every missing one; `deploy/compose.yaml` sets them all for the
first three. The Jira adapter is not in compose: it needs Jira and an identity provider.
Compose has no Kubernetes, GitLab or identity provider:
the task service runs with `GOLEM_KUBERNETES=none` and refuses every run with that reason, and
the edge cannot fetch signing keys, so it serves public agent cards on
`http://127.0.0.1:8480` (`GOLEM_EDGE_PORT`) and answers 401 to every call.

Passwords in `deploy/` are placeholders for local development only.

## A run, end to end

1. A client sends an A2A `SendMessage` to the edge with a bearer token and the agent as `tenant`.
2. The edge verifies the token, checks the chain policy, writes an audit row and forwards the call.
3. The task service creates the A2A task; the orchestrator records the run once per
   (caller, message id), admits it within the caller's and the chain's limits and launches a
   Kubernetes Job.
4. In the Job, the runtime clones the agent catalog and the context repository, asks the lead
   for the next role and target, runs the role, validates its change and pushes
   `golem/<target>/<run>`. The report goes to the pod's termination message.
5. The reconciler sees the Job finish, opens a merge request for the branch and tells the task
   service, which completes or fails the task with the reason.

A target stays pending while its proposal branch exists. Merged merge requests delete their
branch. A merge request closed without merging keeps it on purpose: deleting it would make the
lead propose the same target again. A rejected proposal is recorded as a status change on the
record, which is a human decision.

## Jira adapter

Putting a label on an issue starts an agent; the outcome comes back as a comment.

- **Jira webhook** (`POST /jira/webhook`): register a `jira:issue_updated` webhook with a
  secret. Requests without a valid `X-Hub-Signature: sha256=<hex HMAC-SHA256 of the body>` are
  refused with 401. `GOLEM_JIRA_LABELS_FILE` maps labels to agents (`golem:discovery: discovery`);
  other labels and removals are ignored.
- **To the edge**: `SendMessage` with the agent as `tenant`, message id
  `jira:<issue>:<label>:<webhook timestamp>` (a Jira retry starts no second run) and a push
  config pointing at `GOLEM_PUBLIC_BASE_URL/a2a/push` with a per-run token. The adapter
  authenticates with the client credentials grant against `GOLEM_OIDC_TOKEN_URL`; the edge sees
  it as `service:<GOLEM_OIDC_CLIENT_ID>`, which the call registry must allow for each mapped
  agent, and the token must carry the edge's audience.
- **Back to Jira** (`POST /a2a/push`): a terminal task state becomes one comment on the issue
  through the REST API v2 (`/rest/api/2/issue/{key}/comment`). Jira Cloud: set
  `GOLEM_JIRA_USER` to the account email and `GOLEM_JIRA_TOKEN` to its API token (Basic auth).
  Jira Data Center: leave `GOLEM_JIRA_USER` unset and put a personal access token in
  `GOLEM_JIRA_TOKEN` (Bearer).

The task service does not send push notifications yet (its request handler has no push
config store or sender), so until it does, a run started from Jira ends without a comment.

Other settings: `GOLEM_EDGE_URL`, `GOLEM_OIDC_CLIENT_SECRET`, `GOLEM_JIRA_URL`,
`GOLEM_JIRA_WEBHOOK_SECRET`, `GOLEM_PUSH_TOKEN_SECRET`, `GOLEM_PORT`.

## Evaluation of a catalog change

A merge request to an agent catalog runs `python -m golem.evaluation run` in the catalog
repository's CI ([ADR 0006](docs/adr/0006-evaluation-in-ci-first.md)). Each case of the golden
set in `evals/` is a context repository and a goal with what the run must produce: the outcome,
the role and target the lead picks, and checks on the proposal branch (sections filled, files
changed only under a directory, phrases present or absent). Every case runs through the same
`golem.runtime.main.run` a Job runs, against local bare repositories. The gate passes when the
pass rate reaches the threshold (0.8 by default) and no case that passed in `evals/baseline.json`
fails now; the exit code fails the pipeline, and "Pipelines must succeed" blocks the merge. The
format and the commands: [examples/discovery/evals/README.md](examples/discovery/evals/README.md).

## Status

Not deployed. The whole run lifecycle above is implemented and tested: Postgres and Kubernetes
parts against real Postgres 17 and k3s in testcontainers, git against real repositories, the
model and GitLab through fakes at their boundaries. The Jira adapter is tested against the real
edge and task service, with Jira and the identity provider faked at their HTTP boundaries.
The evaluation of catalog merge requests runs in the catalog's CI job, tested with fake roles
over the example golden set. Not built yet: the Mattermost and GitLab adapters, a UI, the
orchestrator's evaluation workflow with a model judge and trace store, and Temporal (target).
