# Configuration reference

Every setting of every process, every Secret and every configuration file. Not meant to be
read in order: look up what you need. How to put it together the first time:
[install.md](install.md).

Each process reads `GOLEM_*` environment variables and refuses to start with a list of every
missing one (`golem edge: missing environment variables: ...`), so one restart shows every
gap. "Required" below means exactly that list; `tests/test_docs.py` checks the tables against
the parsers ([settings.py](../../src/golem/settings.py),
[mcp/settings.py](../../src/golem/mcp/settings.py),
[runtime/main.py](../../src/golem/runtime/main.py)).

Conventions that hold for every process:

- A URL setting that is a base URL (`GOLEM_PUBLIC_BASE_URL`, `GOLEM_EDGE_URL`,
  `GOLEM_GITLAB_URL`, ...) must not end with a slash.
- `GOLEM_PORT` (default `8000`) is the port callers use; `GOLEM_METRICS_PORT` (default `9090`)
  serves `GET /metrics` and nothing else, and must differ from every other port of the process
  ([ADR 0013](../adr/0013-metrics.md)).
- A rate limit `<NAME>` is two settings, `<NAME>_PER_MINUTE` and `<NAME>_BURST`, positive
  integers ([ADR 0012](../adr/0012-rate-limits.md)). The limits are per replica.
- `GOLEM_TRUSTED_PROXIES`: comma-separated CIDRs without host bits whose `X-Forwarded-For` is
  believed; none by default. List the ingress controller's pods, never the pod network.
- Files named by `*_FILE` settings are read once at start: a ConfigMap change needs a restart
  (`kubectl -n golem-system rollout restart deployment/<name>`).

## Processes

### Edge: `python -m golem.edge`

The only door: signed agent cards and the directory of agents, token checks, the call
registry, rate limits, audit, forwarding to the task service ([ADR 0002](../adr/0002-edge-and-task-service-split.md)).

<!-- settings: edge -->
| Setting | Required or default | Meaning |
| --- | --- | --- |
| `GOLEM_OIDC_ISSUER` | required | the identity provider's issuer; tokens must carry exactly this `iss` |
| `GOLEM_OIDC_AUDIENCE` | required | the audience every access token must carry (`golem-edge`) |
| `GOLEM_OIDC_JWKS_URL` | required | the provider's signing keys; fetched at start and on an unknown key id, at most once a minute (until the first fetch succeeds: after 1 s, doubling up to a minute) |
| `GOLEM_OIDC_DISCOVERY_URL` | required | named in the agent cards for clients |
| `GOLEM_CALL_REGISTRY_FILE` | required | the call registry ([format](#call-registry)) |
| `GOLEM_MAX_CHAIN_DEPTH` | required | the deepest agent-to-agent chain allowed (positive integer) |
| `GOLEM_AUDIT_DSN` | required | libpq connection string to `golem_audit` as `golem_edge` |
| `GOLEM_TASK_SERVICE_URL` | required | the task service's `a2a` listener (`http://tasks.golem-system.svc:8000`) |
| `GOLEM_CATALOGS_DIR` | required | a directory of `<agent>/agent.yaml` and `<process>/process.yaml` the cards and the agents' part of the call registry are built from ([cards](#agent-cards)) |
| `GOLEM_PUBLIC_BASE_URL` | required | the edge's public address, written into the agent cards |
| `GOLEM_EDGE_TOKEN` | required | shared secret sent with every forwarded request |
| `GOLEM_CARD_SIGNING_KEY_FILE` | required | the key that signs every agent card: an unencrypted EC P-256 private key in PEM, its own, never the run token key ([signed cards](#agent-cards)) |
| `GOLEM_CARD_SIGNING_KID` | required | the card key's id, in each signature's header and in `GET /.well-known/golem-card-keys.json`; a new key gets a new id |
| `GOLEM_TASK_SERVICE_READ_URL` | required | the task service's `internal-read` listener (`http://tasks.golem-system.svc:8001`): the orchestrator's public keys, which verify the call tokens of delegating runs (fetched like the provider's keys), and run statuses, which revoke them ([ADR 0014](../adr/0014-golem-as-an-a2a-node.md)) |
| `GOLEM_RUN_STATUS_TTL_SECONDS` | `10` | how long the edge caches a delegating run's status; a canceled or finished run's call token stops working within it |
| `GOLEM_PORT` | `8000` | A2A, agent cards, the directory, the card keys, a process owner's resolution, and the proposal and report routes ([ADR 0015](../adr/0015-proposals.md)) |
| `GOLEM_METRICS_PORT` | `9090` | metrics |
| `GOLEM_RATE_CALLER_PER_MINUTE`, `GOLEM_RATE_CALLER_BURST` | `60`, `20` | per authenticated caller, every `/a2a` call and every authenticated `GET /agents`, from one bucket |
| `GOLEM_RATE_AUTH_FAILURES_PER_MINUTE`, `GOLEM_RATE_AUTH_FAILURES_BURST` | `30`, `10` | per client address, failed authentications |
| `GOLEM_RATE_DIRECTORY_PER_MINUTE`, `GOLEM_RATE_DIRECTORY_BURST` | `120`, `60` | per client address, anonymous `GET /agents` and `GET /.well-known/golem-card-keys.json` |
| `GOLEM_TRUSTED_PROXIES` | none | see above |

### Task service: `python -m golem.tasks`

A2A tasks, run admission, Job launch, run tokens. Three listeners, one per kind of caller
([ADR 0009](../adr/0009-deployment-on-kubernetes.md)).

<!-- settings: tasks -->
| Setting | Required or default | Meaning |
| --- | --- | --- |
| `GOLEM_RUNS_DSN` | required | libpq connection string to `golem_runs` as `golem_runs` |
| `GOLEM_TASKS_DB_URL` | required | SQLAlchemy URL to `golem_tasks` as `golem_tasks`: `postgresql+asyncpg://golem_tasks:<password>@<host>:<port>/golem_tasks` |
| `GOLEM_MAX_RUNS_PER_CALLER` | required | running runs one caller may have; over it, `REJECTED` (`caller_concurrency`). A delegated run counts against the person or service its chain acts for |
| `GOLEM_MAX_RUNS_PER_ROOT` | required | running runs one call chain may have, the first run and every run delegated from it (`chain_concurrency`) |
| `GOLEM_BUDGET_PER_ROOT` | required | total estimated cost of one call chain (`chain_budget`) |
| `GOLEM_ESTIMATED_RUN_COST` | required | the flat estimate every run reserves against the budget |
| `GOLEM_JOB_IMAGE` | required | the runtime image of every run; set it to the image of your overlay |
| `GOLEM_JOB_SECRET` | required | the Secret in the Jobs namespace every run reads ([run Secret](#secrets)) |
| `GOLEM_JOB_DEADLINE_SECONDS` | required | `activeDeadlineSeconds` of a run's Job; the run token expires 60 s later |
| `GOLEM_JOB_TTL_SECONDS` | required | `ttlSecondsAfterFinished`: how long a finished Job, its pod and log stay |
| `GOLEM_JOB_CPU`, `GOLEM_JOB_MEMORY` | required | a run's requests and limits (Kubernetes quantities) |
| `GOLEM_CATALOGS_FILE` | required | where each agent's catalog is ([format](#catalogs)); an agent missing here is refused |
| `GOLEM_AGENT_TOOLS_FILE` | required | the platform's tool grant per agent ([format](#agent-tools)) |
| `GOLEM_RUN_TOKEN_KEY_FILE` | required | the run token signing key: an unencrypted EC P-256 private key in PEM |
| `GOLEM_RUN_TOKEN_KID` | required | its key id in the JWKS |
| `GOLEM_KUBERNETES_NAMESPACE` | required | where runs' Jobs are created (`golem-jobs`) |
| `GOLEM_KUBERNETES` | required | `in-cluster`, `kubeconfig` or `none` (starts, refuses every run: local development only) |
| `GOLEM_PUBLIC_BASE_URL` | required | the edge's public address, written into the service card |
| `GOLEM_EDGE_TOKEN` | required | the same value as the edge's; requests without it get 401, which the edge turns into 502 for its caller |
| `GOLEM_PUSH_ALLOWED_PREFIXES` | none: push notifications off | comma-separated URL prefixes, each ending with `/`, a push may go to (the adapters' Services) |
| `GOLEM_PUSH_CONFIG_KEY` | required with `GOLEM_PUSH_ALLOWED_PREFIXES` | Fernet key that encrypts push configs (they hold the adapters' push tokens) in `golem_tasks` |
| `GOLEM_MCP_REGISTRY_CONFIGMAP` | none | a ConfigMap in the Jobs namespace mounted into every run as the [MCP registry](#mcp-registry) |
| `GOLEM_CATALOGS_DIR` | none: no processes | the edge's directory of pinned catalogs; a task for one of its `process.yaml` is a process, not a run ([ADR 0019](../adr/0019-processes.md)), checked with its stage agents at start |
| `GOLEM_WRITE_SERVERS_FILE` | none: nothing is applied | where the [write servers](#write-servers) are; without it an accepted proposal of a kind the platform applies ends `failed` ([ADR 0015](../adr/0015-proposals.md)) |
| `GOLEM_PORT` | `8000` | `a2a`: agent card, `/a2a`, `/processes/{task}/resolution`, `/proposals` and `/reports`, for the edge |
| `GOLEM_INTERNAL_READ_PORT` | `8001` | `internal-read`: `/internal/run-keys`, `/internal/runs/{run_id}`, for the MCP servers |
| `GOLEM_INTERNAL_WRITE_PORT` | `8002` | `internal-write`: `/internal/run-outcome`, `/internal/proposal-state` and `/internal/process-state`, for the reconciler |
| `GOLEM_METRICS_PORT` | `9090` | metrics |

### Reconciler: `python -m golem.orchestrator.reconciler`

Every interval: finished Jobs to run outcomes, merge requests for succeeded runs, outcomes to
the task service, and a step for every process, whose stages it starts through the edge.

<!-- settings: reconciler -->
| Setting | Required or default | Meaning |
| --- | --- | --- |
| `GOLEM_RUNS_DSN` | required | libpq connection string to `golem_runs` as `golem_runs` |
| `GOLEM_RECONCILE_INTERVAL_SECONDS` | required | pause between passes (positive number) |
| `GOLEM_TASK_SERVICE_URL` | required | the task service's `internal-write` listener (`http://tasks.golem-system.svc:8002`) |
| `GOLEM_GITLAB_URL` | required | GitLab's base URL; the API is `<url>/api/v4` |
| `GOLEM_GITLAB_TOKEN` | required | a token with `api` scope on every context repository |
| `GOLEM_GITLAB_PROJECTS_FILE` | required | agent to context repository and target branch ([format](#gitlab-projects)) |
| `GOLEM_KUBERNETES_NAMESPACE` | required | the Jobs namespace |
| `GOLEM_KUBERNETES` | required | as for the task service |
| `GOLEM_MR_POLL_SECONDS` | `300` | how often one open merge request is read back from GitLab for its proposal's state; at most 50 per pass (positive number) |
| `GOLEM_EDGE_URL` | none: processes' stages never start | the edge's a2a port (`http://edge.golem-system.svc:8000`), where stages start as calls of their process ([ADR 0019](../adr/0019-processes.md)); needs the two below |
| `GOLEM_RUN_TOKEN_KEY_FILE` | required with `GOLEM_EDGE_URL` | the task service's run token key: stages' call tokens are signed with it |
| `GOLEM_RUN_TOKEN_KID` | required with `GOLEM_EDGE_URL` | its key id, the task service's |
| `GOLEM_METRICS_PORT` | `9090` | metrics, its only listener |

### Jira adapter: `python -m golem.adapters jira`

<!-- settings: jira-adapter -->
| Setting | Required or default | Meaning |
| --- | --- | --- |
| `GOLEM_EDGE_URL` | required | the edge's Service (`http://edge.golem-system.svc:8000`) |
| `GOLEM_OIDC_TOKEN_URL` | required | the identity provider's token endpoint (client credentials) |
| `GOLEM_OIDC_CLIENT_ID` | required | the adapter's client; the edge sees `service:<client id>` |
| `GOLEM_OIDC_CLIENT_SECRET` | required | its secret, sent in the form body |
| `GOLEM_JIRA_URL` | required | Jira's base URL, for comments |
| `GOLEM_JIRA_USER` | none | Jira Cloud: the account's email (Basic auth with `GOLEM_JIRA_TOKEN`); empty on Data Center |
| `GOLEM_JIRA_TOKEN` | required | Cloud: the API token; Data Center: a personal access token (Bearer) |
| `GOLEM_JIRA_WEBHOOK_SECRET` | required | the Jira webhook's secret; requests must carry `X-Hub-Signature: sha256=<HMAC>` |
| `GOLEM_PUSH_TOKEN_SECRET` | required | signs the per-run push tokens; its own, not the Mattermost adapter's |
| `GOLEM_JIRA_LABELS_FILE` | required | label to agent ([format](#jira-labels)) |
| `GOLEM_PUBLIC_BASE_URL` | required | where the task service pushes (`http://jira-adapter.golem-system.svc:8000`); must be under one of the task service's `GOLEM_PUSH_ALLOWED_PREFIXES` |
| `GOLEM_PORT` | `8000` | `/jira/webhook` and `/a2a/push` |
| `GOLEM_METRICS_PORT` | `9090` | metrics |
| `GOLEM_RATE_WEBHOOK_PER_MINUTE`, `GOLEM_RATE_WEBHOOK_BURST` | `300`, `100` | per client address, the webhook |
| `GOLEM_TRUSTED_PROXIES` | none | see above |

### Mattermost adapter: `python -m golem.adapters mattermost`

<!-- settings: mattermost-adapter -->
| Setting | Required or default | Meaning |
| --- | --- | --- |
| `GOLEM_EDGE_URL` | required | the edge's Service |
| `GOLEM_OIDC_TOKEN_URL` | required | the token endpoint |
| `GOLEM_OIDC_CLIENT_ID` | required | the adapter's client (`golem-mattermost-adapter`) |
| `GOLEM_OIDC_CLIENT_SECRET` | required | its secret |
| `GOLEM_PUSH_TOKEN_SECRET` | required | signs the per-run push tokens |
| `GOLEM_PUBLIC_BASE_URL` | required | where the task service pushes (`http://mattermost-adapter.golem-system.svc:8000`) |
| `GOLEM_MATTERMOST_URL` | required | Mattermost's base URL, for posts |
| `GOLEM_MATTERMOST_BOT_TOKEN` | required | a bot account's access token |
| `GOLEM_MATTERMOST_COMMAND_TOKEN` | required | the `/golem` slash command's token |
| `GOLEM_MATTERMOST_AGENTS` | required | comma-separated agents the command may start |
| `GOLEM_MATTERMOST_TEAMS` | required | comma-separated team ids where the command works |
| `GOLEM_MATTERMOST_CHANNELS` | none: every channel of the teams | comma-separated channel ids |
| `GOLEM_PORT` | `8000` | `/mattermost/command` and `/a2a/push` |
| `GOLEM_METRICS_PORT` | `9090` | metrics |
| `GOLEM_RATE_COMMAND_PER_MINUTE`, `GOLEM_RATE_COMMAND_BURST` | `120`, `60` | per client address, the command |
| `GOLEM_TRUSTED_PROXIES` | none | only if commands come through the ingress controller |

### MCP server: `python -m golem.mcp`

One process per tool group ([ADR 0008](../adr/0008-platform-mcp-servers.md)).

<!-- settings: mcp -->
| Setting | Required or default | Meaning |
| --- | --- | --- |
| `GOLEM_MCP_GROUP` | required | `tracker.read` (Jira) or `wiki.read` (Confluence) |
| `GOLEM_MCP_UPSTREAM_URL` | required | Jira's or Confluence's base URL |
| `GOLEM_MCP_UPSTREAM_TOKEN` | required | the server's own credential upstream |
| `GOLEM_MCP_UPSTREAM_USER` | none | Atlassian Cloud: the account's email (Basic); empty on Data Center (Bearer) |
| `GOLEM_MCP_JIRA_DEPLOYMENT` | required for `tracker.read` | `cloud` or `data-center`: which search endpoint to call |
| `GOLEM_TASK_SERVICE_URL` | required | the task service's `internal-read` listener (`http://tasks.golem-system.svc:8001`) |
| `GOLEM_AUDIT_DSN` | required | libpq connection string to `golem_audit` as `golem_mcp` |
| `GOLEM_MCP_RUN_STATUS_TTL_SECONDS` | `10` | how long a run's status is cached; a canceled run's calls stop within it |
| `GOLEM_MCP_KEYS_REFRESH_SECONDS` | `60` | least time between refetches of the run keys on an unknown key id; a server that has never loaded keys (it started before the task service) retries after 1 s, doubling up to this |
| `GOLEM_PORT` | `8000` | `/mcp` |
| `GOLEM_METRICS_PORT` | `9090` | metrics |
| `GOLEM_RATE_AUTH_FAILURES_PER_MINUTE`, `GOLEM_RATE_AUTH_FAILURES_BURST` | `30`, `10` | per client address, failed authentications |
| `GOLEM_TRUSTED_PROXIES` | none | runs call directly; leave empty |

### Web UI: `python -m golem.ui`

<!-- settings: ui -->
| Setting | Required or default | Meaning |
| --- | --- | --- |
| `GOLEM_OIDC_ISSUER` | required | the issuer; discovery must name exactly this one |
| `GOLEM_OIDC_DISCOVERY_URL` | required | the provider's discovery document, read on first use |
| `GOLEM_OIDC_CLIENT_ID` | required | the UI's client (`golem-ui`) |
| `GOLEM_OIDC_CLIENT_SECRET` | required | its secret (`client_secret_basic`) |
| `GOLEM_OIDC_REDIRECT_URL` | required | exactly `GOLEM_PUBLIC_BASE_URL` + `/callback` |
| `GOLEM_EDGE_URL` | required | the edge's Service |
| `GOLEM_UI_DSN` | required | libpq connection string to `golem_ui` as `golem_ui` |
| `GOLEM_UI_SESSION_KEY` | required | Fernet key that encrypts the tokens of sessions |
| `GOLEM_PUBLIC_BASE_URL` | required | the UI's public address: `https`, or `http` on localhost only |
| `GOLEM_PORT` | `8000` | the JSON API and sign-in |
| `GOLEM_METRICS_PORT` | `9090` | metrics |
| `GOLEM_RATE_LOGIN_PER_MINUTE`, `GOLEM_RATE_LOGIN_BURST` | `30`, `10` | per client address, `GET /login` |
| `GOLEM_RATE_START_PER_MINUTE`, `GOLEM_RATE_START_BURST` | `10`, `5` | per session, starting tasks |
| `GOLEM_TRUSTED_PROXIES` | none | the ingress controller's pods |

### Board: the `golem-board` image

The board's nginx has no settings: its configuration (`board/nginx.conf`) is baked into the
image, and nothing is templated at start ([ADR 0018](../adr/0018-board.md)). It needs no Secret,
no ConfigMap and no network access. What it answers is fixed by the image: `index.html` for
every path but `/assets/`, the security headers of ADR 0018 on every answer, and caching by
path. A change to either means a new image.

### Runtime Job: `python -m golem.runtime`

The task service writes the first group into every Job; the rest comes from the run Secret
(`GOLEM_JOB_SECRET`) and the run's token Secret. Nothing here is set by hand.

<!-- settings: runtime -->
| Setting | Required or default | Meaning |
| --- | --- | --- |
| `GOLEM_RUN_ID` | required | the run, set by the task service |
| `GOLEM_AGENT` | required | the agent; the catalog's `name` must match |
| `GOLEM_CATALOG_REF` | required | `<git url>#<revision>` from the catalogs file |
| `GOLEM_GOAL` | required | the caller's text |
| `GOLEM_TARGET` | none: the target is `run-<run id>` | a goal agent's target record, from the message's `golemTarget` metadata when it matches `^[a-z][a-z0-9-]{0,63}$` ([ADR 0017](../adr/0017-triggers.md)); a record agent ignores it |
| `GOLEM_RUN_TOKEN` | none | the run token, from Secret `golem-run-<run id>-token`; a role with tools fails without it |
| `GOLEM_CALL_TOKEN` | none | the call token, from the same Secret; a role naming `agents.delegate` fails without it ([ADR 0014](../adr/0014-golem-as-an-a2a-node.md)) |
| `GOLEM_MCP_REGISTRY` | none | path of the mounted MCP registry; without it a role naming tools fails |
| `GOLEM_MODEL_GATEWAY_URL` | from the run Secret | the gateway's OpenAI-compatible base URL |
| `GOLEM_MODEL` | from the run Secret | the model alias |
| `GOLEM_MODEL_KEY` | from the run Secret | the gateway key |
| `GOLEM_MODEL_TIMEOUT_SECONDS` | `120` | per model call; the client retries a timeout twice |
| `GOLEM_GIT_TOKEN` | none | GitLab token to clone and push over HTTPS; sent as a header, never in a URL |
| `GOLEM_WORKDIR` | `/workspace` | where the catalog and the context are cloned |
| `OTEL_EXPORTER_OTLP_ENDPOINT`, `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` | none: no traces | the trace store (`/v1/traces` is appended to the first) |
| `OTEL_EXPORTER_OTLP_HEADERS`, `OTEL_EXPORTER_OTLP_TRACES_HEADERS` | none | its headers, `key=value` pairs |
| `OTEL_SERVICE_NAME` | `golem-runtime` | the traces' service name |
| `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT` | `false` | `true` records prompts and answers in traces |

### Evaluation CLI: `python -m golem.evaluation`

Runs in a catalog repository's CI ([writing-an-agent.md](../guide/writing-an-agent.md#the-ci-gate)).
`run` exits 2 when one of the three gateway settings is missing.

<!-- settings: evaluation -->
| Setting | Required or default | Meaning |
| --- | --- | --- |
| `GOLEM_MODEL_GATEWAY_URL` | required | as in a run |
| `GOLEM_MODEL` | required | as in a run |
| `GOLEM_MODEL_KEY` | required | a key of its own, with a small budget |
| `GOLEM_MODEL_TIMEOUT_SECONDS` | `120` | as in a run |
| `GOLEM_MCP_REGISTRY`, `GOLEM_RUN_TOKEN` | none | tools for roles that name them; see the known gap in the guide |

## Secrets

Not in git. Create them from the files of [install.md](install.md#6-generate-the-secrets), or
with `deploy/k8s/overlays/external-secrets` from a secret store. A changed Secret reaches a
process only when its pod restarts.

| Namespace | Secret | Keys | Used by |
| --- | --- | --- | --- |
| golem-system | `golem-edge` | `GOLEM_AUDIT_DSN` (role `golem_edge`), `GOLEM_EDGE_TOKEN` | edge |
| golem-system | `golem-tasks` | `GOLEM_RUNS_DSN`, `GOLEM_TASKS_DB_URL`, `GOLEM_PUSH_CONFIG_KEY`, `GOLEM_EDGE_TOKEN` | task service |
| golem-system | `golem-run-token-key` | `key.pem`, mounted at `/var/run/golem/run-token/` | task service, reconciler |
| golem-system | `golem-card-signing-key` | `key.pem`, mounted at `/var/run/golem/card-signing/` | edge |
| golem-system | `golem-reconciler` | `GOLEM_RUNS_DSN`, `GOLEM_GITLAB_TOKEN` | reconciler |
| golem-system | `golem-jira-adapter` | `GOLEM_OIDC_CLIENT_SECRET`, `GOLEM_JIRA_TOKEN`, `GOLEM_JIRA_WEBHOOK_SECRET`, `GOLEM_PUSH_TOKEN_SECRET` | Jira adapter |
| golem-system | `golem-mattermost-adapter` | `GOLEM_OIDC_CLIENT_SECRET`, `GOLEM_MATTERMOST_BOT_TOKEN`, `GOLEM_MATTERMOST_COMMAND_TOKEN`, `GOLEM_PUSH_TOKEN_SECRET` | Mattermost adapter |
| golem-system | `golem-ui` | `GOLEM_OIDC_CLIENT_SECRET`, `GOLEM_UI_DSN`, `GOLEM_UI_SESSION_KEY` | UI |
| golem-system | `golem-mcp-tracker-read`, `golem-mcp-wiki-read` | `GOLEM_MCP_UPSTREAM_TOKEN`, `GOLEM_AUDIT_DSN` (role `golem_mcp`) | MCP servers |
| golem-jobs | `golem-run-secrets` | `GOLEM_MODEL_GATEWAY_URL`, `GOLEM_MODEL`, `GOLEM_MODEL_KEY`, optional `GOLEM_MODEL_TIMEOUT_SECONDS`, `GOLEM_GIT_TOKEN`, `OTEL_EXPORTER_OTLP_ENDPOINT`, `OTEL_EXPORTER_OTLP_HEADERS` | every run |
| golem-jobs | `golem-run-<run id>-token` | `GOLEM_RUN_TOKEN` | one run; created by the task service, deleted with its Job |

## ConfigMaps

`golem-config` in `golem-system` holds the platform's files; each process mounts only the keys
it reads at `/etc/golem/`. `golem-mcp-registry` lives in `golem-jobs`, because runs mount it.

| Key | Read by | Setting |
| --- | --- | --- |
| `call-registry.yaml` | edge | `GOLEM_CALL_REGISTRY_FILE` |
| `catalogs.yaml` | task service | `GOLEM_CATALOGS_FILE` |
| `agent-tools.yaml` | task service | `GOLEM_AGENT_TOOLS_FILE` |
| `gitlab-projects.yaml` | reconciler | `GOLEM_GITLAB_PROJECTS_FILE` |
| `jira-labels.yaml` | Jira adapter | `GOLEM_JIRA_LABELS_FILE` |
| `registry.yaml` (`golem-mcp-registry`) | every run | `GOLEM_MCP_REGISTRY_CONFIGMAP` |

Adding an agent touches five of them: the call registry, catalogs, agent tools, GitLab projects
and, if Jira should start it, the labels; plus the agent cards below, and
`GOLEM_MATTERMOST_AGENTS` if people should reach it there. The board lists what the call
registry lets each person call, from the edge's directory.

### Call registry

Callee to the people and services allowed to call it. A caller is `user:<name>` or
`service:<client id>`; `<kind>:*` allows every caller of that kind. A callee missing here is
refused at the edge (`unknown_agent`).

```yaml
discovery: ["user:*", "service:golem-jira-adapter", "service:golem-mattermost-adapter"]
```

Agents are not written here ([ADR 0019](../adr/0019-processes.md)). The edge adds them at
start from the catalogs in `GOLEM_CATALOGS_DIR`: every agent's `delegates` may be called by
that agent (`agent:<name>`), and every process's stage agents by that process. The child runs
for the person or service that started the chain, so a neighbour gives an agent no more than
that subject could start; `GOLEM_MAX_CHAIN_DEPTH` bounds how many agents one request passes
through, and a chain never calls an agent already in it.

The edge refuses to start when this file grants an `agent:` caller, names a callee no catalog
in `GOLEM_CATALOGS_DIR` defines, or, once any process is pinned there, grants a `user:` caller
(`user:*` included) anything but a process: people then start processes, and workers are the
platform's to call. With a process pinned, `GOLEM_MAX_CHAIN_DEPTH` must be at least 3 (person,
process, stage agent, neighbour). Services may still be granted agents.

### Catalogs

Agent to its catalog repository and revision (a branch, tag or commit). The task service
refuses to start runs of an agent missing here, and a run clones exactly this.

```yaml
discovery: https://gitlab.internal/agents/discovery.git#main
```

### Agent tools

The platform's grant: the tool groups a run of the agent may use, written into its run token
([ADR 0007](../adr/0007-run-tokens.md)). An agent missing here gets none. A role's `tools` in
the catalog can only narrow it.

```yaml
discovery: [tracker.read, wiki.read]
```

### GitLab projects

Agent to its context repository (GitLab path) and the branch merge requests target. An agent
missing here has its succeeded runs settled without a merge request, with the reason in the
task.

```yaml
discovery:
  project: product/discovery-context
  target_branch: main
```

### Write servers

`GOLEM_WRITE_SERVERS_FILE`, read by the task service: for each write group, the MCP endpoint it
calls and the server's canonical URI, the audience of the proposal tokens it issues for that
server ([ADR 0015](../adr/0015-proposals.md)). A group left out is not applied: its accepted
proposals end `failed` with the reason.

```yaml
wiki.write:
  url: http://wiki-write.golem-system.svc:8000/mcp
  resource: https://wiki-write.golem-system.svc/mcp
desk.write:
  url: http://desk-write.golem-system.svc:8000/mcp
  resource: https://desk-write.golem-system.svc/mcp
tracker.write:
  url: http://tracker-write.golem-system.svc:8000/mcp
  resource: https://tracker-write.golem-system.svc/mcp
```

### Jira labels

A Jira label to the agent it starts. Other labels, and removals, are ignored.

```yaml
golem:discovery: discovery
```

### MCP registry

Tool group to the MCP server that serves it and the tools the group allows. A role gets the
groups it names, and from each only these tools. `agents.delegate` is served by the runtime
itself: its URL is the edge's A2A endpoint and its only tool `delegate_to_agent`.

```yaml
tracker.read:
  url: http://mcp-tracker-read.golem-system.svc:8000/mcp
  tools: [search_issues, get_issue]
wiki.read:
  url: http://mcp-wiki-read.golem-system.svc:8000/mcp
  tools: [search_pages, get_page, get_page_source]
agents.delegate:
  url: http://edge.golem-system.svc:8000/a2a
  tools: [delegate_to_agent]
```

### Agent cards

The edge builds a public agent card for each `<GOLEM_CATALOGS_DIR>/<agent>/agent.yaml` and
each `<GOLEM_CATALOGS_DIR>/<process>/process.yaml` at start; a directory holds one of the two. The base points it at the example catalogs in the image (`/app/examples`). For your own
agents, mount the `agent.yaml` of each catalog, for example from a ConfigMap generated in your
overlay (`cards/discovery/agent.yaml` next to `kustomization.yaml`):

```yaml
configMapGenerator:
  - name: golem-agent-cards
    namespace: golem-system
    files: [discovery=cards/discovery/agent.yaml]
patches:
  - target: {kind: ConfigMap, name: golem-edge-env}
    patch: |-
      - {op: replace, path: /data/GOLEM_CATALOGS_DIR, value: /etc/golem/cards}
  - target: {kind: Deployment, name: edge}
    patch: |-
      - op: add
        path: /spec/template/spec/volumes/-
        value: {name: cards, configMap: {name: golem-agent-cards,
                items: [{key: discovery, path: discovery/agent.yaml}]}}
      - op: add
        path: /spec/template/spec/containers/0/volumeMounts/-
        value: {name: cards, mountPath: /etc/golem/cards, readOnly: true}
```

The same catalogs feed the call registry, so every callee the registry names needs its catalog
here; the edge refuses to start otherwise.

Every card is signed at start with the card key (`GOLEM_CARD_SIGNING_KEY_FILE`, kid
`GOLEM_CARD_SIGNING_KID`) as A2A 1.0 specifies (a JWS over the card's RFC 8785 canonical form),
and the edge publishes the public key at `GET /.well-known/golem-card-keys.json`. The edge also
lists the cards at `GET /agents`: every published agent to an anonymous caller, only those the
call registry lets them call to an authenticated one. How a caller uses both:
[discovering-agents.md](../guide/discovering-agents.md); rotating the key:
[security.md](security.md#rotate-the-card-signing-key).

## Ports, probes and Services

| Process | Service ports | Probes |
| --- | --- | --- |
| edge | 8000, 9090 | TCP on 8000 |
| tasks | 8000 `a2a`, 8001 `internal-read`, 8002 `internal-write`, 9090 | `GET /internal/run-keys` on 8001 |
| reconciler | 9090 | none: a crash ends the process and the kubelet restarts it |
| jira-adapter, mattermost-adapter, mcp-* | 8000, 9090 | TCP on 8000 |
| ui | 8000, 9090 | readiness `GET /healthz`, liveness TCP |
| board | 8080 | readiness `GET /`, liveness TCP |

Moving a port means changing the setting, the container port, the Service and the network
policies together ([deploy/k8s/README.md](../../deploy/k8s/README.md#what-runs-where)).
