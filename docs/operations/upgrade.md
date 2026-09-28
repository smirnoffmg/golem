# Upgrade and roll back

Rolling out a new Golem image, what happens to the database schema, and how to go back.

## What changes in an upgrade

| Part | How it changes | What keeps working meanwhile |
| --- | --- | --- |
| the nine Deployments | a rolling update per Deployment when the image or the pod template changes | the edge, the UI and the board have two replicas; the others one, with a new pod started before the old one stops |
| runs in flight | not at all: a Job keeps the image it was created with | they finish on the old image; the new reconciler reads their report |
| new runs | use `GOLEM_JOB_IMAGE` of the task service that launches them | nothing |
| `golem_runs`, `golem_ui` tables | additive changes applied by the new processes at start | old processes ignore new columns |
| `golem_tasks` tables | created by the A2A SDK when missing, never altered | see [the SDK's tables](#the-a2a-sdks-tables) |
| ConfigMaps | replaced by `kubectl apply`, read by a process only at start | a changed ConfigMap needs a restart |

## Schema changes

Golem has no migration tool of its own. Each process that owns tables applies its schema when
it starts:

- the task service and the reconciler run
  [`orchestrator/schema.sql`](../../src/golem/orchestrator/schema.sql) under a Postgres
  advisory lock (`golem_runs:schema`), so two starting processes do not collide;
- the UI runs its `CREATE TABLE IF NOT EXISTS` statements
  ([`ui/store.py`](../../src/golem/ui/store.py)) under its own lock (`golem_ui:schema`), since
  its two replicas start together.

Every statement is idempotent (`CREATE ... IF NOT EXISTS`, `ALTER TABLE ... ADD COLUMN IF NOT
EXISTS`), so a restart applies nothing twice. This is safe only while schema changes are
**additive**: new tables, new nullable columns, new indexes. The old version, still running
during the rollout and after a rollback, must not notice them. Removing or renaming a column,
or adding a constraint, is a second release after no old version runs any more: the
"expansion" and "cleanup" phases of *Release It!* (с. 332–333), where columns that will
eventually be `NOT NULL` "are added as nullable, because the old version doesn't know how to
fill these in". A pull request that changes `schema.sql` in any other way needs its own
migration plan.

To see what a database holds now:

<!-- run: schema-show -->
```sh
psql "$GOLEM_RUNS_DSN" -c '\d runs'
```

### The A2A SDK's tables

`golem_tasks` belongs to the `a2a-sdk` package: it creates `tasks` and
`push_notification_configs` when they are missing and never alters them. When `uv.lock` moves
`a2a-sdk` to a version whose release notes change these tables, its migrations must run first
(`a2a-db`, from its `db-cli` extra, with `DATABASE_URL` set to `GOLEM_TASKS_DB_URL`). The
Golem image does not include that extra; run it from a checkout with the extra installed, as
the database owner, after a backup.

## Roll out a new image

1. **Back up** ([backup-and-restore.md](backup-and-restore.md#back-up)), and read the changes
   since your version: new settings (a process refuses to start without a required one,
   [configuration.md](configuration.md)), changed policies, new ADRs.
2. **Build and push** both images under a new tag (never reuse a tag: nodes cache images):

```sh
docker build --tag registry.internal/golem:0.1.1 .
docker build --tag registry.internal/golem-board:0.1.1 board
docker push registry.internal/golem:0.1.1           # environment-specific
docker push registry.internal/golem-board:0.1.1     # environment-specific
```

3. **Change every reference** in your overlay: both `newTag`s under `images` in
   `kustomization.yaml`, and `GOLEM_JOB_IMAGE` in `config.yaml`. kustomize does not rewrite
   environment values, so forgetting the last keeps new runs on the old image.
4. **Review** what will change. `kubectl diff` exits with 1 when there are differences:

<!-- run: upgrade-diff -->
```sh
kubectl diff -k deploy/k8s/overlays/prod | grep -E '^[-+] .*(image|GOLEM_JOB_IMAGE)'
```

5. **Apply** and wait for every rollout:

<!-- run: upgrade-apply -->
```sh
kubectl apply -k deploy/k8s/overlays/prod
for deployment in $(kubectl -n golem-system get deployments -o name); do
  kubectl -n golem-system rollout status "$deployment" --timeout=5m
done
```

   The order does not matter: every process tolerates the others' old or new version during a
   rollout, and whichever of the task service and the reconciler starts first applies the
   schema. A process that fails to start keeps its old pod serving (the rollout waits);
   `kubectl -n golem-system logs deploy/<name>` names the missing setting.

6. **Restart what reads changed ConfigMaps.** The base's ConfigMaps have fixed names, so a
   changed value does not roll the pods by itself:

```sh
kubectl -n golem-system rollout restart deployment/tasks   # for example, after golem-config changed
```

7. **Check**: every Deployment on the new image, new runs on the new image, a run end to end:

<!-- run: upgrade-check -->
```sh
kubectl -n golem-system get deployments \
  -o custom-columns='NAME:.metadata.name,IMAGE:.spec.template.spec.containers[0].image'
kubectl -n golem-system get configmap golem-tasks-env -o jsonpath='{.data.GOLEM_JOB_IMAGE}{"\n"}'
```

```
NAME                 IMAGE
board                registry.internal/golem-board:0.1.1
edge                 registry.internal/golem:0.1.1
jira-adapter         registry.internal/golem:0.1.1
mattermost-adapter   registry.internal/golem:0.1.1
mcp-tracker-read     registry.internal/golem:0.1.1
mcp-wiki-read        registry.internal/golem:0.1.1
reconciler           registry.internal/golem:0.1.1
tasks                registry.internal/golem:0.1.1
ui                   registry.internal/golem:0.1.1
registry.internal/golem:0.1.1
```

   Then start a run (the board or [A2A](install.md#10-first-run)) and check its Job's image:
   `kubectl -n golem-jobs get jobs -l app.kubernetes.io/name=golem-run -o custom-columns='NAME:.metadata.name,IMAGE:.spec.template.spec.containers[0].image'`.
   Watch `golem_http_requests_total{status_class="5xx"}` and the alerts of
   [alerts.md](alerts.md) for the next hour.

8. If the upgrade touched a NetworkPolicy, run the network check
   ([install.md, step 9](install.md#9-check-the-network)).

## Roll back

Put the previous image back in both places of the overlay and apply it with step 5. Because
schema changes are additive, the previous version runs on the newer schema; do not restore a
database backup to roll back code. Restore data only when data is wrong, and then follow
[backup-and-restore.md](backup-and-restore.md#restore).

`kubectl rollout undo` also works for one Deployment in a hurry, but the next `kubectl apply`
of the overlay brings the new image back; fix the overlay as soon as possible.

Runs started on the new image keep it until they end. If the new runtime itself is the
problem, cancel them (**Cancel** on the board, or A2A `CancelTask`) after the rollback.

## Upgrading to the board

The release that brings the board ([ADR 0018](../adr/0018-board.md)) replaces the UI's pages
with a JSON API and a second image. Roll it out as one change:

1. **Build and push both images** (step 2 above) and add the board's image to `images` in
   your overlay; the base adds the `board` Deployment, Service, service account and network
   policy.
2. **Change the ingress in the same change window** as the new `ui` image: the UI's host is
   now routed by path, `/api/` and `/login`, `/callback`, `/logout`, `/healthz` to `ui`, the
   rest to `board` ([install.md, step 7](install.md#7-write-your-overlay)). The new `ui` serves
   no pages, so the old routing shows people errors until it changes; the old `ui` knows none
   of the board's `/api/` paths, so routing first breaks the pages the other way.
3. **Remove `GOLEM_UI_AGENTS`** from `golem-ui-env`. The board lists the agents the edge's
   directory lets each person call; the setting is no longer read.
4. **Nothing to run for the tasks' agents.** The task service records each task's agent and,
   when it starts, fills it in for existing tasks from `golem_runs`. Tasks without a run (never
   admitted before the upgrade) appear on no board; they are still readable over A2A.

Rolling back this release means rolling back the ingress change with it.

## Upgrading Kubernetes, the CNI, Postgres

- **Kubernetes or the CNI**: run the network check afterwards; a CNI change can silently stop
  enforcing policies ([ADR 0009](../adr/0009-deployment-on-kubernetes.md)). The Kubernetes
  API endpoint may change: check it as in
  [install.md, step 7](install.md#7-write-your-overlay).
- **Postgres**: Golem is tested on Postgres 17 only. Upgrade with your usual procedure after a
  backup; the connection strings do not change if the address stays. Afterwards, check the
  processes' logs for connection errors and start a run.

## How this page was checked

The run-marked blocks were executed on the k3s cluster of
[install.md](install.md#how-this-guide-was-checked) after the first run: a second tag of the
same build imported into the cluster, both references changed in the overlay, the diff, the
apply and the check above, a run on the new image, and the rollback to the first tag with the
same apply block. The board's lines (its image in steps 2, 3 and 7, and
[Upgrading to the board](#upgrading-to-the-board)) were added after that run and were not
executed on a cluster.
