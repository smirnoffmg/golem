# Backup and restore

What state Golem keeps, what to back up, and how to restore it so that runs, tasks and
proposal branches agree again.

A restore is done under pressure, often by someone who has not done it before: write the
steps down so that a person who does not know the tools can follow them, and rehearse them
monthly on a temporary cluster (*Cloud Native DevOps with Kubernetes*, с. 270). The commands
below were run against Postgres 17 (see [How this page was checked](#how-this-page-was-checked)).

## What state exists where

| State | Where | Owner | Back up? | Lost, it means |
| --- | --- | --- | --- | --- |
| runs, their tasks, the notification outbox | `golem_runs` (`runs`, `run_tasks`) | orchestrator | yes | admission forgets running runs; outcomes and merge requests of finished runs cannot reach their tasks |
| A2A tasks and push configs | `golem_tasks` (`tasks`; `push_notification_configs` once an adapter has started a run) | task service | yes | callers cannot read or cancel their tasks; outcomes cannot be pushed to Jira or Mattermost |
| audit log | `golem_audit` (`audit_log`) | `golem_audit_owner` | yes, and ship it elsewhere ([ADR 0003](../adr/0003-one-postgres-cluster-per-owner-databases.md)) | the record of who called what |
| UI sessions | `golem_ui` (`sessions`, `logins`) | UI | no | everyone signs in again |
| agent catalogs, context repositories, proposal branches, merge requests | GitLab | GitLab | yes, with GitLab's own backup | the agents and the records themselves |
| Secrets | your secret store (or the `golem-secrets` directory of the install) | you | yes, in the secret store | see below |
| ConfigMaps, overlays | your overlay in Git | you | with that repository | nothing that Git does not keep |
| run Jobs, run token Secrets | `golem-jobs` | Kubernetes | no | a run in flight fails ([below](#runs-in-flight)) |

Two Secrets must match the data they were used with: `GOLEM_PUSH_CONFIG_KEY` (it encrypts the
push configs in `golem_tasks`; with another key their outcomes are never pushed) and the
database passwords of the restored roles. Keep the key that was current when the backup was
taken.

## Back up

**Preferred: a physical backup of the whole cluster with point-in-time recovery**, with your
Postgres operator or `pg_basebackup` and WAL archiving (environment-specific). It restores all
databases to one moment, so they agree with each other.

**Logical dumps**, one database at a time, if you need them (for example a stricter regime for
one database, ADR 0003). Each dump is its own snapshot, so dump `golem_tasks` **before**
`golem_runs`: a run newer than its task is harmless (its notification finds no task and is
dropped), while a task newer than its run would stay `working` for ever.

<!-- run: backup -->
```sh
export PGHOST=10.20.0.5 PGPORT=5432 PGUSER=postgres   # a superuser
for db in golem_tasks golem_runs golem_audit; do
  pg_dump --format=custom --file="$db.dump" "$db"
done
```

`golem_ui` is left out on purpose: its sessions are useless without the tokens they hold, and
the UI recreates its tables at start.

Backups run online; nothing needs to stop. Check that each dump lists its tables:

<!-- run: backup-check -->
```sh
pg_restore --list golem_runs.dump | grep 'TABLE DATA'
```

```
3436; 0 16420 TABLE DATA public run_tasks golem_runs
3435; 0 16405 TABLE DATA public runs golem_runs
```

## Restore

In this order: stop the writers, restore the databases, start the platform, then check the
proposal branches.

1. Stop every process, so nothing writes while the data is replaced (the Deployments come back
   in step 5):

<!-- run: restore-stop -->
```sh
kubectl -n golem-system scale deployment --all --replicas=0
```

2. Decide what happens to runs still running in `golem-jobs` ([below](#runs-in-flight)). To
   start clean, delete them:

<!-- run: restore-jobs -->
```sh
kubectl -n golem-jobs delete jobs -l app.kubernetes.io/name=golem-run
```

3. On a new or emptied server, create the databases and roles with
   [install.md, step 3](install.md#3-create-the-databases-and-roles) (`init.sql` and the
   passwords the Secrets hold). Then restore each dump as its owner. The audit table already
   exists (`init.sql` created it), so only its rows are restored:

<!-- run: restore -->
```sh
pg_restore --exit-on-error --no-owner --role=golem_tasks --dbname=golem_tasks golem_tasks.dump
pg_restore --exit-on-error --no-owner --role=golem_runs --dbname=golem_runs golem_runs.dump
pg_restore --exit-on-error --data-only --dbname=golem_audit golem_audit.dump
```

4. Check the counts, and that every table belongs to its service's role (the example is the
   demo's five tasks: one run per state, the fifth refused by admission):

<!-- run: restore-check -->
```sh
psql -d golem_runs -c 'select status, count(*) from runs group by status order by status'
psql -d golem_runs -c "select tableowner, tablename from pg_tables where schemaname = 'public' order by tablename"
psql -d golem_tasks -c "select tableowner, tablename from pg_tables where schemaname = 'public' order by tablename"
```

```
  status   | count
-----------+-------
 canceled  |     1
 failed    |     1
 running   |     1
 succeeded |     1
(4 rows)

 tableowner | tablename
------------+-----------
 golem_runs | run_tasks
 golem_runs | runs
(2 rows)

 tableowner  | tablename
-------------+-----------
 golem_tasks | tasks
(1 row)
```

5. Start the platform again with your overlay (it sets the replica counts back). The order
   within does not matter: the reconciler retries a notification until the task service
   answers.

```sh
kubectl apply -k deploy/k8s/overlays/prod
kubectl -n golem-system wait --for=condition=Available deployment --all --timeout=5m
```

   Then restart the MCP servers as in [install.md, step 8](install.md#8-apply).

6. Look for proposal branches the restored `golem_runs` does not know. A run that pushed its
   branch after the backup has no row now: no merge request is opened for it, and its target
   stays pending for the lead as long as the branch exists. For each context repository:

<!-- run: orphan-branches -->
```sh
git ls-remote --heads "$CONTEXT_REPO" 'refs/heads/golem/*' | awk -F/ '{print $NF}' | sort > branch-runs
psql -At -d golem_runs -c 'select id from runs' | sort > known-runs
comm -23 branch-runs known-runs
```

   Each id printed is a branch without a run. Open its merge request by hand if the proposal
   is wanted, or delete the branch so the lead proposes the target again.

## What the reconciler does after a restore

It needs no help for the rest; within a few passes:

| Restored state | What happens |
| --- | --- |
| a run `running` whose Job is gone | the run fails: "its Job disappeared before reporting a result"; its tasks fail |
| a run `running` whose Job finished meanwhile (not deleted) | the normal outcome: merge request, then the task completes |
| a succeeded run without a settled proposal | the merge request is found or opened (idempotent per branch), then its tasks are told |
| a finished run whose task is not in `golem_tasks` | the notification gets 404 and is marked delivered |
| a task `working` whose run is not in `golem_runs` | nothing ever finishes it; the caller must cancel it. The dump order above avoids this |

## Runs in flight

A run's Job does not use the databases: it clones, runs the role, pushes a branch and exits.
What restores change is who finishes it:

- **Job deleted before the restore** (step 2): its run fails after the restore, its task tells
  the caller, and the caller starts again. Nothing was pushed if the Job was deleted before it
  pushed; if it had pushed, step 6 finds the branch.
- **Job left running** and its run in the restored `golem_runs`: it finishes normally.
- **Job left running** but its run not in the restored `golem_runs`: it pushes a branch nobody
  proposes; step 6 finds it.

## How this page was checked

The run-marked blocks were executed once, in order, by a script: Postgres 17 in Docker with
`init.sql`, the task states of `scripts/ui_demo.py` written through the real edge, task service
and reconciler, the backup blocks run with the server's own `pg_dump`, a second fresh server
prepared with `init.sql`, the restore blocks, and the orphan check against a bare Git
repository with one branch per known run and one without. The `kubectl` steps were run on the
k3s cluster of [install.md](install.md#how-this-guide-was-checked). The outputs above are from
that run.
