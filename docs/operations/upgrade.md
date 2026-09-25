# Upgrade and roll back

Rolling out a new Golem image, what happens to the database schema, and how to go back.

## What changes in an upgrade

| Part | How it changes | What keeps working meanwhile |
| --- | --- | --- |
| the eight Deployments | a rolling update per Deployment when the image or the pod template changes | the edge and the UI have two replicas; the others one, with a new pod started before the old one stops |
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
  ([`ui/store.py`](../../src/golem/ui/store.py)).

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
2. **Build and push** the image under a new tag (never reuse a tag: nodes cache images):

```sh
docker build --tag registry.internal/golem:0.1.1 .
docker push registry.internal/golem:0.1.1     # environment-specific
```

3. **Change both references** in your overlay: `newTag` under `images` in
   `kustomization.yaml`, and `GOLEM_JOB_IMAGE` in `config.yaml`. kustomize does not rewrite
   environment values, so forgetting the second keeps new runs on the old image.
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

   Then start a run (UI or [A2A](install.md#10-first-run)) and check its Job's image:
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
problem, cancel them (the UI's **Cancel**, or A2A `CancelTask`) after the rollback.

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
same apply block.
