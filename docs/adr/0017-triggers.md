# 17. Triggers: runs the system starts, on an alert or on a schedule

## Status

Proposed, 2026-09-28

## Context

Every run so far starts because someone asked: a person in the web UI, a label on a Jira
issue, a command in chat, another agent. An agent that watches the running system ([ADR 0016](0016-observability-tools.md))
has to start without anyone asking. Two occasions matter. An alert fires, and someone should
know what is behind it before a person opens Grafana. And there is a slow drift nobody alerts on:
a trend, a new kind of error in the logs. That needs a regular look.

Both starters are channel adapters in the sense of [ADR 0010](0010-mattermost-adapter.md): A2A clients of the edge,
authenticated as a service. What is new is that no person is behind the request, even as a
claim. Five questions follow:

- how Alertmanager proves a notification is its own;
- what counts as the same alert, so a repeated notification starts nothing new;
- how an alert reaches the agent for its environment, since grants are per agent ([ADR 0016](0016-observability-tools.md));
- how an alert storm is kept from turning into a run storm;
- how the runtime runs an agent that has a goal but no record to work on. Today the lead
  picks a target from the records of the context repository (`runtime/lead.py`). An alert
  is not a record ([ADR 0015](0015-proposals.md), Consequences).

Who sees the result is already settled. The owner of such a task is a service, so the agent's
`reviewers` decide its proposals ([ADR 0015](0015-proposals.md)).

Sources:

- Alertmanager configuration (<https://prometheus.io/docs/alerting/latest/configuration/>):
  - `webhook_config`: `url`, `send_resolved` (default `true`), `http_config`, `max_alerts`
    ("0 means all alerts included"), `timeout`.
  - The payload: `version` `"4"`, `groupKey`, `truncatedAlerts`, `status`, `receiver`,
    `groupLabels`, `commonLabels`, `commonAnnotations`, `externalURL`, and `alerts` with
    `status`, `labels`, `annotations`, `startsAt`, `endsAt`, `generatorURL`, `fingerprint`.
  - `http_config`: `authorization` (`type`, default `Bearer`, `credentials`,
    `credentials_file`), `basic_auth`, `oauth2` (`client_id`, `client_secret`,
    `client_secret_file`, `scopes`, `token_url`; exclusive with the other two) and
    `tls_config`.
  - The route's `group_by`, `group_wait` (30s), `group_interval` (5m, "between subsequent
    notifications for existing alert group"), `repeat_interval` (4h) and `continue`.
- Alertmanager source:
  - `notify/webhook/webhook.go`: the message is the template data plus `version`,
    `groupKey` (`notify.ExtractGroupKey(ctx)`) and `truncatedAlerts`. The response goes
    through `retrier.Check`: a retryable status gives `notify.Retry`, anything else
    `notify.Unrecoverable`.
  - `notify/util.go`, `Retrier.Check`: "2xx responses are considered to be always
    successful"; `retry := statusCode/100 == 5 || slices.Contains(r.RetryCodes,
    statusCode)`.
  - `dispatch/dispatch.go`: a group's flush runs under `context.WithTimeout(ag.ctx,
    ag.timeout(ag.opts.GroupInterval))`, and alerts whose notification failed stay in the
    group for the next flush.
- Kubernetes, CronJob (<https://kubernetes.io/docs/concepts/workloads/controllers/cron-jobs/>):
  - `concurrencyPolicy` `Forbid` "skips the new Job run" while the previous one runs.
  - `startingDeadlineSeconds`: a start later than this is skipped.
  - `timeZone` takes an IANA name.
  - "A CronJob may create zero, one, or two Jobs for a single scheduled time", so "design
    your Job tasks to be idempotent".
  - The `batch.kubernetes.io/cronjob-scheduled-timestamp` annotation (v1.32 and later)
    belongs to the Job, not to its Pods.
- *Site Reliability Engineering* (Beyer et al.), с. 64 (PDF 90): "Every page should be
  actionable"; "Pages should be about a novel problem or an event that hasn't been seen
  before"; and the question to ask of each alert: "Are other people getting paged for this
  issue, therefore rendering at least one of the pages unnecessary?"
- [ADR 0010](0010-mattermost-adapter.md) (a channel adapter's identity and replay rule),
  [ADR 0012](0012-rate-limits.md) (rate limits at entry points), [ADR 0015](0015-proposals.md)
  (proposals, reviewers), [ADR 0016](0016-observability-tools.md) (tool groups per
  environment); `orchestrator/runs.py`: one run per (caller, message id), and a refused
  admission records no run.

## Decision

Two new starters: `python -m golem.adapters alertmanager`, a Deployment, and
`python -m golem.adapters schedule`, the command of a CronJob per schedule. Both send
`SendMessage` to the edge with their own client credentials, as `service:<client id>`, and
reuse `adapters/common.py`. Neither holds state or a database.

### Goal agents: a run without a record to pick

An agent whose work starts from a goal says so in its catalog:

```yaml
mode: goal                 # default: records
goal:
  role: investigator       # the role every run executes
  kind: investigation      # the record kind the runtime creates for the run
proposal: tracker_issue    # [ADR 0015](0015-proposals.md)
```

A `goal` agent still has a context repository. The branch carries the result
([ADR 0015](0015-proposals.md)), and past investigations are the records the role reads to recognise a problem it
has seen before. But the lead does not choose. The runtime changes in `runtime/main.py` only;
`lead.py` stays a pure function of a snapshot and is not called:

1. **The target is named by the starter, not found.** The message's metadata carries
   `golemTarget`, a record id matching `^[a-z0-9][a-z0-9-]{0,63}$`: for an alert,
   `alert-<first 12 hex of SHA-256(groupKey)>`; for a schedule, `schedule-<name>`. The task
   service passes it to the orchestrator, and the Job gets it as `GOLEM_TARGET`, next to
   `GOLEM_GOAL`. A missing or malformed value makes the target the run id. The same alert
   group always lands on the same record, so its history accumulates there.
2. **The runtime writes the record before the role runs.** If the target record does not
   exist, the runtime creates it on the run's branch, with the catalog's `goal.kind`, status
   `open`, and the goal text as its body. If it exists, the run works on it as it is. From
   there the brief, the role and the validator run exactly as for a record agent, with
   `Command(role=goal.role, target_id=<target>)`.
3. **Pending does not block.** A record agent skips a target with an open proposal branch
   (`pending_ids`). A goal agent does not: the alert fired again, and that is new
   information. The brief lists the open proposals on the target, so the role can extend an
   issue it already proposed rather than propose another.
4. **The proposal is optional.** A goal agent's run writes `golem-proposal.json` only when it
   found something to act on. Without one, the run's outcome is `reported`: exit 0, the
   branch pushed, no proposal row. In both cases the reconciler reads the target record at
   the branch's head commit (Repository files API, as [ADR 0015](0015-proposals.md) reads the proposal) and puts
   it into the task's outcome as an A2A artifact `report`: a text part, cut at 20 000
   characters. The termination message keeps its 4 KiB report as today. A `reported` run's
   branch is then deleted, since there is nothing to decide. The investigation's record
   lands in the repository only through an applied proposal, per [ADR 0015](0015-proposals.md).

`reported` is a success for the run's status (`succeeded`) and a separate outcome in metrics
(`golem_runs_total{outcome="reported"}`). A goal agent's golden set ([ADR 0006](0006-evaluation-in-ci-first.md)) holds cases
with and without a finding, so the evaluation measures both false alarms and misses.

### Goal agents as first built

- **A target starts with a letter.** The runtime and the task service accept
  `^[a-z][a-z0-9-]{0,63}$`, not the leading digit allowed above: the target becomes a record's
  id, and a record id starts with a letter. Both alert and schedule targets fit. A missing or
  malformed target becomes `run-<run id>`, since a run id may start with a digit. The task
  service drops a malformed `golemTarget` before the orchestrator sees it; the Job gets
  `GOLEM_TARGET` only when there is one.
- **The record the runtime opens** lies at `<the goal role's writes>/<target>.md`, in the
  kind's `initial` status (the step above says `open`; a record born in another status would
  skip a human decision, the validator's rule), with the goal quoted (`> `) so a `## ` line in
  an alert's text cannot become a section, and the kind's sections empty. It is committed on the
  run's branch before the role runs, so the validators judge the role's change alone.
- **The role says it found something** by calling `submit_proposal(reason)`, a tool the runner
  serves to goal runs only and that no MCP tool may shadow. Without the call the run is
  `reported`. The runtime's report names the record (`record`), and the reconciler reads only a
  relative `.md` path without `..` from it.
- **Only `merge_request` goal agents may propose.** A goal run that proposes another kind is
  `invalid` until the platform applies that kind ([ADR 0015](0015-proposals.md)); otherwise it
  would land as a merge request nobody asked for. A `merge_request` goal run writes no
  `golem-proposal.json`: its report's outcome, `proposed` or `reported`, tells the reconciler
  whether to open a merge request.
- **`golem_runs.runs` gains `outcome`, `record` and `report`.** The reconciler stores the report
  (cut at 20 000 characters) before it deletes the branch, so a failed delete is retried without
  reading again, and settles the run only after the delete. A branch or record already gone
  leaves a report that says so. The task gets `golemOutcome: reported` and one artifact
  `report` (id `report-<run id>`), and no `golemProposal`.
- **A starter names the target in the message.** `golemTarget` in the message's metadata is
  the one hook: the Alertmanager adapter, the scheduler and a process stage
  ([ADR 0019](0019-processes.md)) each set it on their `SendMessage`.
- **A vanished Job is not read as a report.** When a Job is gone before the reconciler saw it
  (its TTL passed while the reconciler was down), the run is judged by its branch as before, so
  a goal run that reported gets a merge request a person closes. Keeping the report's outcome
  past the Job's TTL is left for later.

### The Alertmanager adapter

**Routing stays in Alertmanager.** Operators already route alerts by labels there. The
adapter serves `POST /alertmanager/webhook/<agent>`, and each Golem agent is its own receiver
whose URL names it. A route sends to it with `continue: true`, so the people's receivers
still get the alert: Golem adds an investigation and replaces no one. The adapter checks the
path against `GOLEM_ALERTMANAGER_AGENTS`, for example
`investigator-prod=prod,investigator-test=test`: the agents it may start, and the environment
each one's grants cover. The edge's call registry must also allow the adapter's principal
for each of them ([ADR 0010](0010-mattermost-adapter.md): either list can narrow, neither can widen).

**The environment must match.** The investigation reads with the agent's grants, which are
per environment ([ADR 0016](0016-observability-tools.md)). An alert about production must therefore reach the agent granted
`*.prod`, and nothing else. The adapter reads `commonLabels[GOLEM_ALERTMANAGER_ENV_LABEL]`
(default `environment`). If it is missing, or is not the agent's environment, the adapter
answers 400 and starts nothing. Alertmanager does not retry a 4xx, and logs it as
unrecoverable. Such a route is a configuration error: its `group_by` must include the
environment label, or a group could mix environments. The error shows at the first alert,
not after an investigation has read the wrong system.

**Authentication: an OAuth 2.0 token from the identity provider, verified like any other.**
Alertmanager's `http_config.oauth2` fetches a client credentials token for its own client at
the identity provider and sends it as `Authorization: Bearer`. The adapter verifies it:

- the signature, with the provider's keys through `golem.jwks`;
- the issuer and `exp`;
- the audience `golem-alertmanager-adapter`, set by an audience mapper on Alertmanager's
  client, as for the UI in [ADR 0011](0011-web-ui.md);
- `azp` equal to `GOLEM_ALERTMANAGER_CLIENT_ID`.

Anything else is 401 before the body is parsed. Golem stores no shared secret with
Alertmanager, and the provider revokes or rotates the client. A static bearer token, as in
[ADR 0010](0010-mattermost-adapter.md), would sit in two places and could be replayed forever. The payload is not signed
by any of Alertmanager's options, so, as for Mattermost, the network is the second control:
the adapter's NetworkPolicy admits port 8000 only from Alertmanager's pods or addresses. A
flood before authentication is limited per address as for the other webhooks
(`GOLEM_RATE_WEBHOOK`, [ADR 0012](0012-rate-limits.md)).

**What is the same alert.** A notification carries a group: `groupKey` identifies the route
and the values of its `group_by` labels, and each alert has its own `startsAt`. The message id
is

```
alertmanager:<SHA-256 hex of groupKey + "\n" + the earliest startsAt of the firing alerts>
```

- Alertmanager resends a firing group every `group_interval` while it changes, and every
  `repeat_interval` while it does not. Each resend has the same group and the same earliest
  start, so the same message id. The orchestrator starts one run per (caller, message id),
  and the resend starts nothing.
- A new alert joining a group that is already firing does not change the earliest start.
  It is part of the incident already being investigated.
- A group that resolved and fires again has a new earliest start, so a new id and a new run,
  on the same target record. The record shows the history.
- `status: resolved` notifications start nothing (204). Receivers for Golem set
  `send_resolved: false`, and the adapter ignores them anyway.

**The goal** is built from the payload only:

- the alert names and the `commonLabels`;
- each alert's `summary` and `description` annotations, each cut at 1 000 characters;
- `startsAt`, the `generatorURL`s and `truncatedAlerts`.

Everything is quoted as data: "Alertmanager reports: ...". Annotations are written by
whoever wrote the alerting rule, and to Golem they are untrusted text, like a Jira summary.
At most 20 alerts of a group are listed. Receivers set `max_alerts: 20`, and the rest is
counted, not listed.

**No push, no reply.** Alertmanager has nowhere to show an outcome. The adapter registers no
push config. The result is where [ADR 0015](0015-proposals.md) puts it: a `tracker_issue` proposal the agent's
reviewers see on their board. A run that found nothing leaves its report in the task only.

### Storms: admission refuses, Alertmanager retries

An incident fires many groups at once. Every one of them is worth investigating, but not all
at the same time, and not twice.

- **A cap per starter.** `admission.Limits` gains per-caller overrides:
  `GOLEM_CALLER_MAX_RUNS`, for example
  `service:golem-alertmanager-adapter=3,service:golem-scheduler=2`. A caller not named
  there keeps `GOLEM_MAX_RUNS_PER_CALLER`. An alert storm then holds at most three Jobs.
  People and other adapters keep their own quota, and the namespace quota keeps what is left.
- **One investigation per group at a time.** A message may carry
  `metadata.golemConcurrencyKey`. The adapter sets it to the target, `alert-<hash>`. The
  orchestrator stores it on the run (`runs.concurrency_key`) and admits no second running
  run with the same caller and key: the new reject reason is `key_concurrency`. This catches
  a group that resolved and fired again while its first investigation is still running.
  That is a new message id, but the same problem.
- **A refusal is retried by Alertmanager, not by Golem.** A refused admission records no run,
  and the task ends `rejected`. The task service now also names the reason in the task's
  metadata: `golemRefusal` is `caller_concurrency`, `chain_concurrency`, `chain_budget`,
  `key_concurrency`, `already_ended` or `unknown_agent`, instead of the reason being only in
  the text. The adapter answers:
  - **503** when the refusal is `caller_concurrency` or `key_concurrency`. Alertmanager
    retries a 5xx with backoff until the flush's timeout (`group_interval`), and a group
    whose notification failed is flushed again at the next `group_interval`. The retry
    carries the same message id and is admitted once a slot frees up. A group that resolves
    in the meantime is sent as `resolved` and is dropped: an incident that ended by itself
    costs no run.
  - **200** when the refusal is `already_ended`: the same firing was already investigated.
  - **400** for any other refusal: retrying will not help.

  Each refused attempt leaves one rejected task. That is the audit trail of the storm, and
  its size is bounded by Alertmanager's backoff.
- **Novel problems only.** A goal agent's brief tells the role to search `tracker.read` for an
  open issue on the same group (the target id is in the issue's labels, `golem-alert-<hash>`,
  set by `tracker.write` from the proposal) before proposing, and to propose a comment on it
  rather than a new issue. Reviewers see one issue per problem, not one per notification.

### Scheduled runs

A schedule is a Kubernetes CronJob per schedule, running
`python -m golem.adapters schedule` with:

- `GOLEM_SCHEDULE_NAME` (`[a-z0-9-]{1,40}`);
- `GOLEM_SCHEDULE_AGENT`;
- `GOLEM_SCHEDULE_GOAL`;
- `GOLEM_SCHEDULE_CRON` and `GOLEM_SCHEDULE_TIMEZONE`. The manifest writes the same values
  into the CronJob's `schedule` and `timeZone`, from one list in the deploy overlay.

The Job sends one `SendMessage` and exits: 0 when the task was accepted, or when it was
refused as `already_ended`; 1 otherwise.

**Idempotency per slot.** The message id is `schedule:<name>:<slot>`, where the slot is the
latest time the cron expression matches at or before now, in the schedule's time zone, in
RFC 3339. The CronJob may create two Jobs for one scheduled time. Both compute the same slot,
and the orchestrator starts one run. The scheduled-timestamp annotation would be the exact
value, but it belongs to the Job, and a Pod cannot read its Job's annotations through the
downward API. Computing the slot is correct as long as the Job starts before the next match.
That holds with:

- `startingDeadlineSeconds: 300`;
- schedules at least an hour apart. The adapter refuses a shorter interval at start, with
  exit code 2.

A late Job past the deadline is skipped by Kubernetes, not run for the wrong slot.

The CronJob uses:

- `concurrencyPolicy: Forbid`: the Job only sends a message, so this bounds duplicate
  senders, not runs;
- `backoffLimit: 2`: a failed send is retried with the same slot, so the same message id;
- `restartPolicy: Never`.

The run itself is bounded by the scheduler's caller cap and the target `schedule-<name>`
(also its concurrency key). A daily review therefore never overlaps with yesterday's, if that
one is still running.

## Consequences

- An alert now gets an investigation within minutes, and the result reaches people only as
  a proposal their reviewers accept. Golem never pages anyone and never files an issue on
  its own. People keep their receivers; Golem is one more receiver with `continue: true`.
- A run that found nothing leaves its report in a task owned by the service, which [ADR 0015](0015-proposals.md)'s
  routes do not show. The agent's reviewers read it through the edge's `/reports` routes, which
  find it by the `golemOutcome: reported` key the task service writes into the task's metadata
  ([ADR 0018](0018-board.md)). Operators also see these runs in metrics
  (`outcome="reported"`).
- Alert routing is Alertmanager's configuration, reviewed where the rest of it is. Golem's
  side is a short allowlist with environments. A route that sends a production alert to the
  test agent fails loudly at the first notification.
- Alertmanager depends on the identity provider to reach Golem. A provider outage makes the
  notifications fail and Alertmanager retry them. The people's receivers are unaffected.
- The runtime gains a second mode. Goal agents skip the lead, create their own target record
  and may end without a proposal. Record agents are unchanged. The validator's rules apply to
  both, and a goal agent's role still writes only under its directory.
- Admission gains per-caller caps and a concurrency key. Both are checked under the same
  advisory locks as today; the key adds one to the sorted list.
- The task service's rejected tasks gain a machine-readable reason (`golemRefusal`). The UI
  and every adapter can use it; today only the Alertmanager adapter does.
- A storm produces rejected tasks and 503s in Alertmanager's logs, bounded by its backoff and
  `group_interval`. An operator reads the queue there. Golem keeps no queue of its own.
- A schedule's slot is computed, not read. A schedule more frequent than hourly is refused,
  and an operator who needs one has to change this decision.
- The deploy overlay gains the Alertmanager adapter's Deployment, Service, NetworkPolicy and
  identity provider client, plus one CronJob per schedule. The alerting side (receivers,
  routes, `http_config.oauth2`) lives in the Alertmanager configuration, outside this
  repository.
