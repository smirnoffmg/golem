# Install Golem

From an empty cluster to a first run that opens a merge request. Follow the steps in order;
each ends with a check. Commands run from the root of a Golem checkout unless a step says
otherwise.

Some steps happen in systems only you administer: the identity provider, GitLab, the model
gateway. Those steps say what Golem needs and how to check it; the clicks depend on your
products and are marked **environment-specific**. Every other command on this page was run on
k3s (see [How this guide was checked](#how-this-guide-was-checked)).

Decisions behind the layout: [ADR 0009](../adr/0009-deployment-on-kubernetes.md). The full
settings reference: [configuration.md](configuration.md).

## 1. Prerequisites

| What | Why | Check |
| --- | --- | --- |
| Kubernetes (tested on k3s v1.33) with a CNI that enforces NetworkPolicy | runs are contained by network policy only (ADR 0004) | `kubectl version`; the network check in step 9 |
| Pod Security admission (built in) | both namespaces enforce `restricted` | `kubectl get ns golem-system --show-labels` after step 8 |
| Postgres 17, reachable from the cluster | one cluster, four databases (ADR 0003) | `psql -c 'select version()'` |
| An OpenID Connect provider | user sign-in, service tokens for the adapters, token checks at the edge | step 4 |
| GitLab | agent catalogs, context repositories, merge requests | step 5 |
| An OpenAI-compatible model gateway | every role's model calls | step 5 |
| An OTLP trace store (optional) | one trace per run | step 5 |
| Prometheus (optional, recommended) | metrics and alerts ([alerts.md](alerts.md)) | step 8 |
| An ingress controller with TLS | the only way in for people, Jira and Mattermost | step 7 |
| `kubectl` with kustomize, `psql`, `openssl`, Docker | the commands below | `kubectl version --client` |

Check the CNI before anything else. The Kubernetes project's own conformance tests do it
independently of Golem (environment-specific, needs `sonobuoy`):

```sh
sonobuoy run --e2e-focus=NetworkPolicy --wait
```

## 2. Build and publish the images

One image runs every process and every run; a second one, the board, serves the web UI's
static files from nginx ([ADR 0018](../adr/0018-board.md)). Build both with the same tag and
push them to a registry your cluster pulls from (`registry.internal` stands for yours):

```sh
docker build --tag registry.internal/golem:0.1.0 .
docker build --tag registry.internal/golem-board:0.1.0 board
docker push registry.internal/golem:0.1.0           # environment-specific
docker push registry.internal/golem-board:0.1.0     # environment-specific
```

## 3. Create the databases and roles

`deploy/postgres/init.sql` creates the four databases (`golem_tasks`, `golem_runs`,
`golem_audit`, `golem_ui`), one login role per service, the insert-only audit table and the
grants (ADR 0003). Run it once as a superuser:

<!-- run: postgres-init -->
```sh
export PGHOST=10.20.0.5 PGPORT=5432 PGUSER=postgres   # your server and a superuser
psql -v ON_ERROR_STOP=1 -d postgres -f deploy/postgres/init.sql
```

The script sets development passwords (`dev-only-...`). Replace every one with a random value,
kept in a private directory that later steps read from:

<!-- run: postgres-passwords -->
```sh
umask 077
mkdir -p golem-secrets
for role in golem_tasks golem_runs golem_edge golem_mcp golem_ui; do
  openssl rand -hex 24 > "golem-secrets/db-$role"
  echo "ALTER ROLE $role PASSWORD :'password';" |
    psql -v ON_ERROR_STOP=1 -d postgres -v password="$(cat "golem-secrets/db-$role")"
done
```

Check a role, and check that the audit log is write-only for the services:

<!-- run: postgres-check -->
```sh
psql "host=$PGHOST port=$PGPORT dbname=golem_runs user=golem_runs password=$(cat golem-secrets/db-golem_runs)" \
  -c 'select current_user'
psql "host=$PGHOST port=$PGPORT dbname=golem_audit user=golem_edge password=$(cat golem-secrets/db-golem_edge)" \
  -c 'select count(*) from audit_log'
```

The first prints `golem_runs`. The second must fail with
`ERROR:  permission denied for table audit_log`: the edge may insert rows, never read them.

The tables of `golem_runs`, `golem_tasks` and `golem_ui` are created by the processes when they
start ([upgrade.md](upgrade.md#schema-changes)); nothing else is needed. Add your TLS settings
to the connection strings in step 6 (`sslmode` for libpq and psycopg, the driver's own options
for the asyncpg URL); check them against your server's requirements.

## 4. Set up the identity provider

**Environment-specific.** The examples use Keycloak's names; any OpenID Connect provider works
if its tokens have the claims in the table.

What the edge checks in every access token ([edge/auth.py](../../src/golem/edge/auth.py)):

| Claim or header | Must be |
| --- | --- |
| `alg`, `kid` (header) | `RS256` or `ES256`, and a key id present in the provider's JWKS |
| `iss` | exactly `GOLEM_OIDC_ISSUER` |
| `aud` | contains `GOLEM_OIDC_AUDIENCE` (`golem-edge`) |
| `exp` | in the future (`nbf`, if present, in the past) |
| `preferred_username` | a person: the name, and the caller is `user:<name>` |
| `preferred_username` and `azp` | a service: `service-account-...` and the client id, and the caller is `service:<client id>` (Keycloak's service accounts look like this) |

Create, in one realm:

1. **The edge's audience** `golem-edge`: a client scope (or an audience mapper on each client
   below) that adds `golem-edge` to the `aud` of access tokens.
2. **Client `golem-ui`** for the web UI: confidential (client authentication on,
   `client_secret_basic`), standard flow (authorization code) only, PKCE required with method
   `S256`, valid redirect URI `https://golem-ui.internal/callback`, valid post-logout redirect
   URI `https://golem-ui.internal/`, the `golem-edge` audience, refresh tokens on. The UI asks
   for `openid profile`; `preferred_username` must be in the access token. The UI refuses a
   provider whose discovery does not list `S256`.
3. **Clients `golem-jira-adapter` and `golem-mattermost-adapter`**: confidential, client
   credentials grant only (service accounts on, standard flow off), the `golem-edge`
   audience. The adapters send the client id and secret in the form body
   (`client_secret_post`).
4. **Users.** Anyone who can sign in to `golem-ui` can start the agents whose call registry
   entry allows `user:*` (step 7). Narrow it to named users (`user:alice`) where needed.
   The username is the identity: tasks, the call registry and the audit log all key on
   `user:<name>`. Keep the realm's *Edit username* off (`editUsernameAllowed`, off by
   default), never give a new user a deleted user's name (the new one would see the old
   one's tasks), and never a name starting with `service-account-` (it would be read as that
   client's service).

Keep the three client secrets for step 6. Check a service token (the adapter's secret in
`JIRA_ADAPTER_CLIENT_SECRET`):

```sh
TOKEN=$(curl -s https://idp.internal/realms/golem/protocol/openid-connect/token \
  -d grant_type=client_credentials -d client_id=golem-jira-adapter \
  -d client_secret="$JIRA_ADAPTER_CLIENT_SECRET" | jq -r .access_token)
```

<!-- run: decode-token -->
```sh
echo "$TOKEN" | cut -d. -f2 | python3 -c 'import base64, json, sys
p = sys.stdin.read().strip()
print(json.dumps(json.loads(base64.urlsafe_b64decode(p + "=" * (-len(p) % 4))), indent=2))'
```

Look for `"aud"` containing `golem-edge`, `"azp": "golem-jira-adapter"` and a
`preferred_username` starting with `service-account-`.

## 5. Set up GitLab, the model gateway and the trace store

**Environment-specific.** What Golem needs:

**A bot account** (for example `golem-bot`) that owns every token below.

**A context repository per agent**, the Git repository of records the agent works on (the
example agent's is `product/discovery-context`, format in
[examples/context/README.md](../../examples/context/README.md)):

- protect the default branch: nobody may push, Maintainers (the gate owners) may merge;
- the bot is a Developer: it can push the unprotected `golem/<target>/<run>` branches and open
  merge requests, and cannot touch the default branch;
- set merge request approvals so that the gate owners of the records approve (for example with
  a `CODEOWNERS` file per directory).

**A catalog repository per agent** (the example's is `agents/discovery`, the content of
[examples/discovery](../../examples/discovery)); the bot is a Reporter there. Turn on
"Pipelines must succeed" and set the CI/CD variables of
[writing-an-agent.md](../guide/writing-an-agent.md#the-ci-gate).

**Two tokens of the bot:**

| Token | Scopes | Goes to | Why separate |
| --- | --- | --- | --- |
| Git token | `read_repository`, `write_repository` | `GOLEM_GIT_TOKEN` in the run Secret | runs are untrusted (ADR 0004): this one can only clone and push branches |
| API token | `api` | `GOLEM_GITLAB_TOKEN` of the reconciler | opens merge requests; never enters a run |

Check them (environment-specific):

```sh
git -c http.extraHeader="Authorization: Basic $(printf 'oauth2:%s' "$GIT_TOKEN" | base64)" \
  ls-remote https://gitlab.internal/product/discovery-context.git
curl -s -H "PRIVATE-TOKEN: $GITLAB_API_TOKEN" \
  "https://gitlab.internal/api/v4/projects/product%2Fdiscovery-context/merge_requests?state=opened"
```

**The model gateway**: its OpenAI-compatible base URL (with `/v1`), a model alias and a key for
runs. The CI gate needs a second key of its own
([ADR 0006](../adr/0006-evaluation-in-ci-first.md)). Check (environment-specific):

```sh
curl -s -H "Authorization: Bearer $MODEL_KEY" "$MODEL_GATEWAY_URL/models"
```

**The trace store** (optional): an OTLP/HTTP endpoint and its headers, for example
`OTEL_EXPORTER_OTLP_ENDPOINT=https://<langfuse>/api/public/otel` and
`OTEL_EXPORTER_OTLP_HEADERS=Authorization=Basic%20<base64 of public key:secret key>`. Leave
both empty to run without traces.

## 6. Generate the secrets

Golem's own secrets are random values. Generate them next to the database passwords:

<!-- run: secrets -->
```sh
umask 077
mkdir -p golem-secrets
openssl genpkey -algorithm EC -pkeyopt ec_paramgen_curve:P-256 -out golem-secrets/run-token-key.pem
openssl genpkey -algorithm EC -pkeyopt ec_paramgen_curve:P-256 -out golem-secrets/card-signing-key.pem
openssl rand -hex 32 > golem-secrets/edge-token
openssl rand -base64 32 | tr '+/' '-_' > golem-secrets/push-config-key
openssl rand -base64 32 | tr '+/' '-_' > golem-secrets/ui-session-key
openssl rand -hex 32 > golem-secrets/jira-webhook-secret
openssl rand -hex 32 > golem-secrets/jira-push-token-secret
openssl rand -hex 32 > golem-secrets/mattermost-push-token-secret
```

| File | Is | Used as |
| --- | --- | --- |
| `run-token-key.pem` | an unencrypted EC P-256 private key (PKCS#8) | the run token signing key ([ADR 0007](../adr/0007-run-tokens.md)) |
| `card-signing-key.pem` | another one, never the same file | the agent card signing key of the edge ([ADR 0014](../adr/0014-golem-as-an-a2a-node.md)) |
| `edge-token` | 32 random bytes, hex | `GOLEM_EDGE_TOKEN`, shared by the edge and the task service |
| `push-config-key`, `ui-session-key` | Fernet keys: 32 random bytes, URL-safe base64 | `GOLEM_PUSH_CONFIG_KEY`, `GOLEM_UI_SESSION_KEY` |
| `jira-webhook-secret` | 32 random bytes, hex | `GOLEM_JIRA_WEBHOOK_SECRET`; also the secret of the Jira webhook |
| `*-push-token-secret` | 32 random bytes, hex, one per adapter | `GOLEM_PUSH_TOKEN_SECRET` |

The same values can come from Python where `openssl` is missing, for example the image's:
`python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`
for a Fernet key and
`python -c "from golem.run_token import SigningKey; print(SigningKey.generate('golem-1').private_pem)"`
for either signing key.

Then write one environment file per Secret. Set the values that come from other systems first:

```sh
export JIRA_ADAPTER_CLIENT_SECRET=... MATTERMOST_ADAPTER_CLIENT_SECRET=... UI_CLIENT_SECRET=...
export JIRA_TOKEN=... CONFLUENCE_TOKEN=... MATTERMOST_BOT_TOKEN=... MATTERMOST_COMMAND_TOKEN=...
export GIT_TOKEN=... GITLAB_API_TOKEN=...
export MODEL_GATEWAY_URL=https://llm.internal/v1 MODEL=discovery-default MODEL_KEY=...
export OTLP_ENDPOINT= OTLP_HEADERS=
```

(`MATTERMOST_COMMAND_TOKEN` is the token Mattermost shows when you create the `/golem` slash
command, [channels.md](../guide/channels.md#mattermost); `MATTERMOST_BOT_TOKEN` a bot account's
access token.)

<!-- run: env-files -->
```sh
db() { printf 'host=%s port=%s dbname=%s user=%s password=%s' \
  "$PGHOST" "$PGPORT" "$1" "$2" "$(cat "golem-secrets/db-$2")"; }
cat > golem-secrets/golem-edge.env <<EOF
GOLEM_AUDIT_DSN=$(db golem_audit golem_edge)
GOLEM_EDGE_TOKEN=$(cat golem-secrets/edge-token)
EOF
cat > golem-secrets/golem-tasks.env <<EOF
GOLEM_RUNS_DSN=$(db golem_runs golem_runs)
GOLEM_TASKS_DB_URL=postgresql+asyncpg://golem_tasks:$(cat golem-secrets/db-golem_tasks)@$PGHOST:$PGPORT/golem_tasks
GOLEM_PUSH_CONFIG_KEY=$(cat golem-secrets/push-config-key)
GOLEM_EDGE_TOKEN=$(cat golem-secrets/edge-token)
EOF
cat > golem-secrets/golem-reconciler.env <<EOF
GOLEM_RUNS_DSN=$(db golem_runs golem_runs)
GOLEM_GITLAB_TOKEN=$GITLAB_API_TOKEN
EOF
cat > golem-secrets/golem-jira-adapter.env <<EOF
GOLEM_OIDC_CLIENT_SECRET=$JIRA_ADAPTER_CLIENT_SECRET
GOLEM_JIRA_TOKEN=$JIRA_TOKEN
GOLEM_JIRA_WEBHOOK_SECRET=$(cat golem-secrets/jira-webhook-secret)
GOLEM_PUSH_TOKEN_SECRET=$(cat golem-secrets/jira-push-token-secret)
EOF
cat > golem-secrets/golem-mattermost-adapter.env <<EOF
GOLEM_OIDC_CLIENT_SECRET=$MATTERMOST_ADAPTER_CLIENT_SECRET
GOLEM_MATTERMOST_BOT_TOKEN=$MATTERMOST_BOT_TOKEN
GOLEM_MATTERMOST_COMMAND_TOKEN=$MATTERMOST_COMMAND_TOKEN
GOLEM_PUSH_TOKEN_SECRET=$(cat golem-secrets/mattermost-push-token-secret)
EOF
cat > golem-secrets/golem-ui.env <<EOF
GOLEM_OIDC_CLIENT_SECRET=$UI_CLIENT_SECRET
GOLEM_UI_DSN=$(db golem_ui golem_ui)
GOLEM_UI_SESSION_KEY=$(cat golem-secrets/ui-session-key)
EOF
cat > golem-secrets/golem-mcp-tracker-read.env <<EOF
GOLEM_MCP_UPSTREAM_TOKEN=$JIRA_TOKEN
GOLEM_AUDIT_DSN=$(db golem_audit golem_mcp)
EOF
cat > golem-secrets/golem-mcp-wiki-read.env <<EOF
GOLEM_MCP_UPSTREAM_TOKEN=$CONFLUENCE_TOKEN
GOLEM_AUDIT_DSN=$(db golem_audit golem_mcp)
EOF
cat > golem-secrets/golem-run-secrets.env <<EOF
GOLEM_MODEL_GATEWAY_URL=$MODEL_GATEWAY_URL
GOLEM_MODEL=$MODEL
GOLEM_MODEL_KEY=$MODEL_KEY
GOLEM_GIT_TOKEN=$GIT_TOKEN
OTEL_EXPORTER_OTLP_ENDPOINT=$OTLP_ENDPOINT
OTEL_EXPORTER_OTLP_HEADERS=$OTLP_HEADERS
EOF
```

Every Secret, its keys and its consumer: [configuration.md](configuration.md#secrets).
`tests/test_docs.py` runs the two blocks above and feeds the files, with the example overlay's
ConfigMaps, to every process's settings parser.

Store the directory's content in your secret store and delete the directory once the
Secrets exist. With the External Secrets Operator, put each value under the remote keys of
`deploy/k8s/overlays/external-secrets/external-secrets.yaml` instead of the next command.

## 7. Write your overlay

Never edit `deploy/k8s/base`. Copy the example overlay and change its values:

```sh
cp -R deploy/k8s/overlays/example deploy/k8s/overlays/prod
```

[`deploy/k8s/overlays/example`](../../deploy/k8s/overlays/example) replaces every placeholder
of the base: the image, the URLs of the identity provider, GitLab, Jira, Confluence, Mattermost
and the public hosts in `config.yaml`, and the addresses outside the cluster in the network
policies. Each network patch tests the placeholder before it replaces it, so a changed base
fails the build rather than opening the wrong rule.

| Value in the example | Replace with | Where |
| --- | --- | --- |
| `registry.internal/golem:0.1.0` | your image (twice: `images` and `GOLEM_JOB_IMAGE`) | `kustomization.yaml`, `config.yaml` |
| `registry.internal/golem-board:0.1.0` | your board image | `kustomization.yaml` |
| `idp.internal`, realm `golem` | your identity provider's issuer and endpoints | `config.yaml` |
| `golem.internal`, `golem-ui.internal` | the public hosts of the edge and the UI | `config.yaml` |
| `gitlab.internal`, `jira.internal`, `confluence.internal`, `mattermost.internal` | your hosts | `config.yaml` |
| the Mattermost team id | where `/golem` is enabled | `config.yaml` |
| `10.40.0.0/24` | the ingress controller's pod addresses | `GOLEM_TRUSTED_PROXIES` in `config.yaml` |
| `10.20.0.5` | Postgres | network patches |
| `10.0.0.1` | the Kubernetes API server's endpoint (command below) | network patches |
| `10.30.0.10` / `.20` / `.30` / `.40` / `.50` / `.60` | identity provider, Atlassian, GitLab, model gateway (port 4000), trace store, Mattermost | network patches |
| `call-registry.yaml`, `catalogs.yaml`, `agent-tools.yaml`, `gitlab-projects.yaml`, `jira-labels.yaml` | your agents | `golem-config` in `config.yaml` ([configuration.md](configuration.md#configmaps)) |

The API server's endpoint, not the `kubernetes` Service's ClusterIP (why:
[deploy/k8s/README.md](../../deploy/k8s/README.md#reaching-the-kubernetes-api-server)):

<!-- run: api-endpoint -->
```sh
kubectl get endpointslices -n default -l kubernetes.io/service-name=kubernetes \
  -o jsonpath='{range .items[*]}{.endpoints[*].addresses}{" "}{.ports[*].port}{"\n"}{end}'
```

A destination with a port other than the example's (Postgres on 6432, a gateway on 443) needs
the rule's `ports` patched as well. A destination inside the cluster is a `namespaceSelector`
and `podSelector` instead of an `ipBlock`; replace the whole peer (`/spec/egress/1/to/0`).

Render and read the result before applying it:

<!-- run: render -->
```sh
kubectl kustomize deploy/k8s/overlays/prod > golem-rendered.yaml
grep -nE '192\.0\.2\.|198\.51\.100\.|203\.0\.113\.|example\.com|replace-with' golem-rendered.yaml \
  || echo "no placeholders left"
```

Label the ingress controller's namespace and Prometheus's namespace (names are yours):

```sh
kubectl label namespace ingress-nginx golem.dev/ingress-controller=true
kubectl label namespace monitoring golem.dev/monitoring=true
```

The base has no Ingress objects: routes and TLS depend on your controller. Route
`https://golem.internal` to Service `edge` port 8000 (A2A, agent cards, `/agents` and
`/.well-known/golem-card-keys.json`), the Jira webhook
path `/jira/webhook` to `jira-adapter` port 8000, and the UI's host by path, since the board and
its backend share one origin ([ADR 0018](../adr/0018-board.md)):

| Path on `https://golem-ui.internal` | Service |
| --- | --- |
| `/api/` (prefix) | `ui` port 8000 |
| `/login`, `/callback`, `/logout`, `/healthz` (exact) | `ui` port 8000 |
| everything else | `board` port 8080 |

Never route `/a2a/push` of an adapter from outside: only the task service may call it.

## 8. Apply

Create the namespaces, then the Secrets, then everything else:

<!-- run: apply -->
```sh
kubectl apply -f deploy/k8s/base/namespaces.yaml
for name in golem-edge golem-tasks golem-reconciler golem-jira-adapter golem-mattermost-adapter \
    golem-ui golem-mcp-tracker-read golem-mcp-wiki-read; do
  kubectl -n golem-system create secret generic "$name" --from-env-file="golem-secrets/$name.env"
done
kubectl -n golem-system create secret generic golem-run-token-key \
  --from-file=key.pem=golem-secrets/run-token-key.pem
kubectl -n golem-system create secret generic golem-card-signing-key \
  --from-file=key.pem=golem-secrets/card-signing-key.pem
kubectl -n golem-jobs create secret generic golem-run-secrets \
  --from-env-file=golem-secrets/golem-run-secrets.env
kubectl apply -k deploy/k8s/overlays/prod
kubectl -n golem-system wait --for=condition=Available deployment --all --timeout=5m
```

Expected:

```
$ kubectl -n golem-system get deployments
NAME                 READY   UP-TO-DATE   AVAILABLE   AGE
board                2/2     2            2           7s
edge                 2/2     2            2           7s
jira-adapter         1/1     1            1           7s
mattermost-adapter   1/1     1            1           7s
mcp-tracker-read     1/1     1            1           7s
mcp-wiki-read        1/1     1            1           7s
reconciler           1/1     1            1           7s
tasks                1/1     1            1           7s
ui                   2/2     2            2           7s
```

The MCP servers fetch the run token keys from the task service when they start. On a first
install they usually start before it is listening, and then refuse every run token for up to a
minute (`GOLEM_MCP_KEYS_REFRESH_SECONDS`), which fails the runs that start meanwhile. Restart
them once the task service is available:

<!-- run: mcp-restart -->
```sh
kubectl -n golem-system rollout restart deployment/mcp-tracker-read deployment/mcp-wiki-read
kubectl -n golem-system rollout status deployment/mcp-tracker-read --timeout=5m
kubectl -n golem-system rollout status deployment/mcp-wiki-read --timeout=5m
```

A Deployment that stays at `0/1`: [troubleshooting.md](troubleshooting.md#a-process-does-not-become-ready).
A process that is missing a setting exits at once and names every missing variable in its
log, for example `kubectl -n golem-system logs deploy/tasks`.

With the Prometheus Operator, base your overlay on `../prometheus-operator` instead of
`../../base` (it is the base plus a `ServiceMonitor`), apply it again, and load the rules of
[alerts.md](alerts.md). With the External Secrets Operator as well, keep `../external-secrets`
and copy `deploy/k8s/overlays/prometheus-operator/service-monitor.yaml` into your overlay's
`resources` (kustomize reads files only below the overlay's directory). Never apply either
overlay by itself: both carry the base's placeholders.

## 9. Check the network

A policy the API server accepts is not a policy your CNI enforces. Run the network check from
[deploy/k8s/README.md](../../deploy/k8s/README.md#verify-the-network-after-deploy) now, and
after every change to a policy or the CNI. Its overlay needs your Postgres address:

```yaml
# deploy/k8s/overlays/prod-netcheck/kustomization.yaml
resources: [../../netcheck]
configMapGenerator:
  - {name: golem-netcheck, namespace: golem-system, behavior: merge,
     literals: [NETCHECK_POSTGRES=10.20.0.5:5432]}
  - {name: golem-netcheck, namespace: golem-jobs, behavior: merge,
     literals: [NETCHECK_POSTGRES=10.20.0.5:5432]}
```

<!-- run: netcheck -->
```sh
kubectl apply -k deploy/k8s/overlays/prod-netcheck
kubectl -n golem-jobs wait --for=condition=Complete job/netcheck-run --timeout=5m
kubectl -n golem-system wait --for=condition=Complete \
  job/netcheck-edge job/netcheck-mcp job/netcheck-reconciler --timeout=5m
kubectl -n golem-netcheck-monitoring wait --for=condition=Complete job/netcheck-monitoring --timeout=5m
kubectl logs -n golem-jobs job/netcheck-run
kubectl delete -k deploy/k8s/overlays/prod-netcheck
```

All five clients Complete means the traffic matrix holds (the listener and the canary are Jobs
too, but they only serve and never complete). A client with a `FAIL` line fails, and its `wait`
runs into the timeout; read its log (`kubectl logs -n golem-system job/netcheck-edge`, and so
on). What a `FAIL` means:
[troubleshooting.md](troubleshooting.md#netcheck-fails). The first line of each client shows
how long a new pod's traffic goes unfiltered on your CNI (`egress unfiltered for Ns`).

## 10. First run

The example agent `discovery` works on a context repository of hypotheses and solutions. For
this run, push the content of [examples/context](../../examples/context) to
`product/discovery-context` and of [examples/discovery](../../examples/discovery) to
`agents/discovery` (environment-specific). Its roles name the tool groups `tracker.read` and
`wiki.read`, so the run also exercises the run token and both MCP servers.

**From the board.** Open `https://golem-ui.internal`, sign in, choose `discovery` on the left,
write a goal in **New task for discovery**, **Start**. The walkthrough with screenshots is
[getting-started.md](../guide/getting-started.md).

**Over A2A.** The agent card is public:

<!-- run: first-run-card -->
```sh
curl -s https://golem.internal/agents/discovery/.well-known/agent-card.json | jq '.name, .skills[].id'
```

```
"discovery"
"research"
"design"
"review"
```

Start a run with a user's access token for the `golem-edge` audience (how to get one is
environment-specific; your provider's CLI or a test client):

<!-- run: first-run-send -->
```sh
curl -s https://golem.internal/a2a \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' -H 'A2A-Version: 1.0' \
  -d '{"jsonrpc": "2.0", "id": 1, "method": "SendMessage", "params": {"tenant": "discovery",
       "message": {"messageId": "first-run-1", "role": "ROLE_USER",
                   "parts": [{"text": "Work the discovery backlog"}]}}}' | jq .
```

The answer is a task in `TASK_STATE_WORKING` with the run's id in its metadata (ids differ):

```json
{
  "result": {
    "task": {
      "id": "02f15a50-92ef-4d88-b2e5-f2a844165053",
      "contextId": "f3813e14-4478-4381-a406-99df6bec903a",
      "status": {
        "state": "TASK_STATE_WORKING",
        "timestamp": "2026-09-25T12:54:34.375049Z"
      },
      "history": [
        {
          "messageId": "first-run-1",
          "contextId": "f3813e14-4478-4381-a406-99df6bec903a",
          "taskId": "02f15a50-92ef-4d88-b2e5-f2a844165053",
          "role": "ROLE_USER",
          "parts": [
            {
              "text": "Work the discovery backlog"
            }
          ]
        }
      ],
      "metadata": {
        "runId": "93ebcf70-752f-4ed5-b347-723c0928354e"
      }
    }
  },
  "id": 1,
  "jsonrpc": "2.0"
}
```

Watch the run's Job and read its report, which the runtime writes to the pod's termination
message and to its log:

<!-- run: first-run-watch -->
```sh
kubectl -n golem-jobs get jobs -l app.kubernetes.io/name=golem-run
kubectl -n golem-jobs logs job/golem-run-$RUN_ID | tail -1 | jq .
```

```json
{
  "run_id": "93ebcf70-752f-4ed5-b347-723c0928354e",
  "agent": "discovery",
  "outcome": "proposed",
  "role": "researcher",
  "target_id": "H-2",
  "branch": "golem/H-2/93ebcf70-752f-4ed5-b347-723c0928354e",
  "reasons": [],
  "summary": "Filled the Evidence section with one finding from interviews."
}
```

Within one reconciler pass (10 s) the reconciler opens the merge request and the task
completes. Ask for it with the task id from the first answer:

<!-- run: first-run-get -->
```sh
curl -s https://golem.internal/a2a \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' -H 'A2A-Version: 1.0' \
  -d '{"jsonrpc": "2.0", "id": 2, "method": "GetTask",
       "params": {"tenant": "discovery", "id": "'"$TASK_ID"'"}}' | jq '.result.status'
```

```json
{
  "state": "TASK_STATE_COMPLETED",
  "message": {
    "messageId": "145a1749-3d97-497d-9b16-30e58d32220e",
    "contextId": "f3813e14-4478-4381-a406-99df6bec903a",
    "taskId": "02f15a50-92ef-4d88-b2e5-f2a844165053",
    "role": "ROLE_AGENT",
    "parts": [
      {
        "text": "Run 93ebcf70-752f-4ed5-b347-723c0928354e succeeded; merge request: https://gitlab.internal/product/discovery-context/-/merge_requests/1"
      }
    ]
  },
  "timestamp": "2026-09-25T12:54:45.617282Z"
}
```

The merge request is in `product/discovery-context`, from `golem/H-2/<run id>` to `main`, and
changes only the `Evidence` section of `hypotheses/H-2.md`. What a gate owner does with it:
[reviewing-proposals.md](../guide/reviewing-proposals.md). If the task stays working or fails:
[troubleshooting.md](troubleshooting.md).

## How this guide was checked

- `tests/test_docs.py` (default suite) runs the secret and environment file blocks of step 6
  and gives the result, with the rendered example overlay, to every process's settings parser;
  it also renders the example overlay and fails on any placeholder left.
- The steps marked with a run comment in this file's source were executed once, in order, on
  k3s v1.33 (kube-router network policies) by a script that runs the blocks as written: the
  image built and imported, Postgres 17 in the cluster reached through a port forward,
  the example overlay plus a thin layer that points the external addresses at in-cluster
  stand-ins (a static JWKS for the identity provider, a `git daemon` for GitLab's repositories,
  a GitLab API stand-in that answers the reconciler's two calls, the scripted model server of
  `tests/e2e`). The public URLs were replaced by the in-cluster Service addresses, since k3s
  had no ingress. Outputs above are from that run.
- Not executed: `sonobuoy`, `docker push`, the identity provider, GitLab, gateway and trace
  store set-up and their checks, the namespace labels and the Ingress routes.
- Added after that run: the board ([ADR 0018](../adr/0018-board.md)): its image, Deployment,
  Service and policy, and the UI host's path routing. The manifests are checked by rendering
  them (`tests/test_k8s_render.py`), the image and the routing by the browser test
  (`tests/e2e/test_ui_browser.py`, the real image behind a front that routes like the
  ingress); the `board` line of step 8's expected output was not seen on that cluster.
- Added after that run: the card signing key (its line in step 6, its Secret in step 8). Step
  6's line runs in `tests/test_docs.py`, which also checks that the edge's parser accepts the
  key; the Secret and its mount are checked by rendering the manifests
  (`tests/test_k8s_render.py`), not on the cluster.
