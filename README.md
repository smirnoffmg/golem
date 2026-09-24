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
  orchestrator/admission.py  run admission: Job quotas per caller and per root chain
  edge/policy.py           chain policy: allowed calls, depth, cycles, budget
  edge/cards.py            A2A Agent Cards generated from the catalog
  tasks/                   A2A task service
  adapters/                channel adapters (A2A clients)
deploy/
  compose.yaml             local Postgres (the single cluster)
  postgres/init.sql        databases, roles and grants
docs/
  architecture.md          C4 diagrams (PlantUML)
  adr/                     architecture decision records
Dockerfile                 one image; the container role is chosen by the command
```

## Development

Requires [uv](https://docs.astral.sh/uv/) and Docker.

```sh
uv sync
uv run pytest
uv run ruff check .
docker compose -f deploy/compose.yaml up
```

Passwords in `deploy/` are placeholders for local development only.

## Status

Early scaffold, nothing deployed.
