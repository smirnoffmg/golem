# 19. Processes: the platform chooses the next agent, a role chooses among a few neighbours

## Status

Proposed, 2026-09-28

## Context

Golem is heading for about a hundred described agents: analysts, designers, developers,
reviewers, documentation writers, investigators, each with a narrow job. Work on a system
passes through several of them in turn, and something has to decide who comes next.

Two mechanisms exist, and neither scales to that number:

- **Delegation** ([ADR 0014](0014-golem-as-an-a2a-node.md)). A role that holds
  `agents.delegate` calls `delegate_to_agent(agent, goal)` with any agent's name. Nothing tells
  the model which names exist or when each one fits; the only guidance is what the role's skill
  text happens to say. The edge's call registry decides whether a call is allowed, not whether
  it was the right one. With a hundred agents the choice is a hundred-way tool selection made
  from free text, and the registry, a file of `callee: [callers]` kept by hand
  (`GOLEM_CALL_REGISTRY_FILE`), would have to list every pair.
- **The process file** ([ADR 0001](0001-industry-standards-and-a2a.md): "the process file with
  stages and gates"; the orchestrator's "Process workflow" in
  [the architecture](../architecture.md)). It was planned as stages the platform runs, with a
  wait for acceptance and a return limit, and left for later together with waiting on a child
  task and Temporal ([ADR 0005](0005-pilot-and-target.md)). It is not built.

What changed since then makes the second one cheap. [ADR 0015](0015-proposals.md) gives every run a
proposal whose state the reconciler already follows (a merge request merged, an edit applied, a
proposal rejected), and a person's decision is exactly the gate a stage waits for. Nothing has to
wait inside a Job: a stage ends with a proposal, the person decides when they decide, and the
reconciler acts on the new state. [ADR 0017](0017-triggers.md) gives the runtime a goal mode, a
run that starts from a goal and a named target instead of a record the lead picks.

People, for their part, should not face a hundred agents either. What a person wants started is a
piece of work ("implement this change", "document this service"), not a particular worker.

Sources:

- *AI Engineering* (Huyen), с. 295 (PDF 319): "More tools give the agent more capabilities.
  However, the more tools there are, the harder it is to efficiently use them. [...] Adding
  tools also means increasing tool descriptions, which might not fit into a model's context."
  The same page recommends comparing an agent's performance with different sets of tools and
  removing a tool whose removal costs nothing.
- *AI Engineering* (Huyen), с. 298 (PDF 322), already cited in [ADR 0014](0014-golem-as-an-a2a-node.md):
  "The more complex a task an agent performs, the more possible failure points there are."
- GitLab Notes API (<https://docs.gitlab.com/api/notes/>):
  `GET /projects/:id/merge_requests/:merge_request_iid/notes` with `sort` (`asc` or `desc`,
  default `desc`) and `order_by` (`created_at` or `updated_at`, default `created_at`); a note
  has `author.username`, `created_at`, `system` ("Boolean indicating if it's a system-generated
  note") and `body`.
- GitLab Merge requests API (<https://docs.gitlab.com/api/merge_requests/>): `closed_by`,
  "Object with information about the user who closed the merge request", and `closed_at`;
  `PUT /projects/:id/merge_requests/:merge_request_iid` with `state_event`, "New state
  (close/reopen)".

## Decision

The next agent in a chain is chosen by the platform, from a process file. Inside a stage, a role
may ask for help only from a short list of neighbours its agent declares. People start
processes, never workers.

### The process file

A process lives in a catalog repository of its own, pinned like an agent's catalog
(`GOLEM_CATALOGS_FILE` for the orchestrator, `GOLEM_CATALOGS_DIR` for the edge), as
`process.yaml`. A catalog directory holds either `agent.yaml` or `process.yaml`, never both, and
the two share one namespace of names.

```yaml
name: corsar-feature
description: Takes a change request from analysis to a merged implementation.
version: 0.1.0
skills:                       # published on the card, as an agent's are
  - id: feature
    name: Implement a change
    description: Analyses, designs and implements a change to the system.
return_limit: 2               # reruns of one stage after a rejection; 0 to 5, default 2
stages:
  - name: analysis
    agent: corsar-analyst
    goal: |
      Analyse this change request and write down what must change and why:
      {input}
  - name: design
    agent: corsar-designer
    goal: Design the change the accepted analysis describes.
  - name: implementation
    agent: corsar-developer
    goal: Implement the accepted design.
```

The loader (`golem.catalog`) refuses a process, and the edge and the orchestrator refuse to
start with it, unless:

- it has 1 to 10 stages with unique slug names, and every `agent` is a pinned agent, not a
  process;
- every stage agent is a goal agent (`mode: goal`, [ADR 0017](0017-triggers.md)) whose catalog
  names a `proposal` kind ([ADR 0015](0015-proposals.md));
- every stage agent has the same context repository (`context.url` and `branch`): stages hand
  over through it, so they must read and write the same one;
- the goal template uses no placeholder but `{input}`, the person's message, and renders to 1
  to 4 000 characters.

Stages are linear. Branching, conditions, parallel stages and loops other than the rerun of a
rejected stage are out of scope; a process that needs them is two processes today.

### A process is an A2A agent

The edge builds a process's card with the same `build_public_card` as an agent's, from the
name, description, version and skills, and signs it the same way
([ADR 0014](0014-golem-as-an-a2a-node.md)). A caller cannot tell a process from an agent, and
nothing outside Golem changes. A person's `SendMessage` to `corsar-feature` is one task, the
process's task, owned by that person as any task is.

The task service's executor hands a task whose tenant is a process to the orchestrator as a
process instead of a run. The orchestrator records a **process run**: a row in `runs` with kind
`process`, the owner as caller and its own id as the root, and no Job. The row is `running` until
the process ends, so everything that already hangs off a run works for it: the root of the chain
budget, `GET /internal/runs/{id}` for revocation, the outbox for the task's updates. A process
run takes no Job slot in admission; its stages do, as runs of the owner under its root.

A new table `process_stages` in `golem_runs` holds the process's progress: `process_run_id`,
`stage` (index), `attempt` (reruns after a rejection), `stale_reruns` (reruns after a stale
proposal), `run_id` of the stage run, `task_id` of the stage task, `reason` of the last
rejection, and `state`: `running`, `needs_reason`, `completed`, `failed` or `canceled`. The reconciler owns it, as it owns `proposals`.

### Stages start through the edge

The reconciler starts a stage the way a delegating role starts a child: a `SendMessage` to the
edge with a call token. It signs the token itself with the orchestrator's run-token key, the one
call tokens are signed with, which the reconciler, an orchestrator process, now mounts too: `sub` the process run's owner, `act` `{"sub": "agent:<process>"}`, `chain`
`[<process>]`, `root` and `run` the process run's id. The edge checks it as any call token: the
registry (below), depth, cycles, the audit row with the chain, the acting agent's rate limit,
and revocation, which now asks about the process run. Canceling the process therefore stops it
starting anything more, by the mechanism that already exists.

The message:

- **id** `process-<process run id>-<stage index>-<attempt>`, so a reconciler pass that dies
  after sending and runs again reaches the same stage run;
- **tenant** the stage's agent;
- **text** the rendered goal, followed by a block the platform writes: the process, the stage
  (`2 of 3, design`), the targets of the stages already applied, which the role finds in the
  context repository's main branch, and on a rerun the rejection:
  `A person rejected the previous attempt (<decided_by>): <reason>`, fenced and marked as text
  to consider, not instructions;
- **metadata** `golemTarget` = `p<first 12 hex of SHA-256(process run id)>-<stage name>`, cut
  to 64 characters, so each stage works on a record of its own and a rerun works on the same
  one.

The stage task's owner is the person, since a delegated child runs as the subject
([ADR 0014](0014-golem-as-an-a2a-node.md)). The person may therefore decide its proposal as its
owner, and the stage agent's `reviewers` may decide it too ([ADR 0015](0015-proposals.md)).

### Transitions follow proposal states

The handoff is the context repository: what a stage produced reaches the next one only by being
accepted into the main branch, where the next role reads it like any record. A reconciler pass
takes each running process and looks at its current stage:

| Current stage | Next |
| --- | --- |
| run running, or proposal `pending`, `accepted` or `failed` | wait: a `failed` apply waits for a person to accept again or reject |
| proposal `applied` (merge request merged, change applied) | start the next stage, attempt 0; after the last stage the process is `completed` |
| proposal `rejected` with a reason, attempts left | start the same stage again, attempt + 1, with the reason in the brief |
| proposal `rejected` with a reason, no attempts left | the process is `failed`, reason `return_limit` |
| merge request closed without a reason, attempts left | the process goes to `needs_reason` and waits for its owner (below) |
| merge request closed without a reason, no attempts left | the process is `failed`, reason `return_limit` |
| proposal `stale` ([ADR 0015](0015-proposals.md)), fewer than 3 stale reruns | start the same stage again, `stale_reruns` + 1, attempt unchanged, with "the page changed since you read it; read it again" |
| proposal `stale`, 3 stale reruns done | the process is `failed`, reason `stale_limit` |
| run `failed`, `invalid`, refused by admission, or `reported` (no proposal) | the process is `failed`, with the run's outcome as the reason |

Two limits bound a stage, because two different things make it rerun. A rejection is a
verdict on the agent's work: at most `1 + return_limit` attempts. A stale proposal is someone
else changing the target while the proposal waited, not the agent's fault, so it does not spend
the return limit; it has its own cap of 3 reruns per stage, which still stops a page that never
holds still from running the stage forever. A run failing for infrastructure is retried inside
the run workflow as today and never reaches this table.

Every transition is one compare-and-set on the `process_stages` row, as proposals' are, and the
next stage's message id makes a repeated start the same start.

**The reason.** Rejecting a proposal of a stage requires a reason. `POST
/proposals/{id}/decision` with `reject` and no `reason`, or one over 4 000 characters, is 400
`reason_required` when the proposal's run is a stage ([ADR 0015](0015-proposals.md) as amended).
A `merge_request` stage is decided in GitLab, where a close carries no reason. The reconciler
takes the newest note (`sort=desc`, `order_by=created_at`) that is not a `system` note, whose
author is `closed_by` and which was created no later than a minute after `closed_at`; its body,
cut at 4 000 characters, is the reason.

**Needs reason.** Without such a note, and with attempts left, the process does not fail: its
stage row goes to `needs_reason`, and the process waits for its owner. The edge serves one more
route outside A2A, next to [ADR 0015](0015-proposals.md)'s:

- `POST /processes/{taskId}/resolution` with `{"action": "rerun", "reason"}` or
  `{"action": "end"}`, where `taskId` is the process task's id. `rerun` needs a `reason` of 1 to
  4 000 characters (400 `reason_required`) and starts the stage again as a rejection with that
  reason would, attempt + 1. `end` ends the process `failed`, reason `ended_by_owner`.

It takes only identity provider tokens (a call token gets 403 `agents_do_not_decide`), a token
from the caller's `/a2a` bucket, and an audit row before it is served (`operation`
`ResolveProcess`, `target_system` `processes`, the task id and the action in `request`), failing
closed like the decision route. The edge forwards it to the task service with the principal, and
only the process task's owner may resolve: any other caller gets 404, as for a task they cannot
read. Reviewers of the stage agent cannot: they decide proposals, and what to do with a process
after a closed merge request is its owner's call. The transition is one compare-and-set,
`UPDATE process_stages ... WHERE state = 'needs_reason'`, through the orchestrator's runs module
as decisions are; a process no longer waiting answers 409 `not_waiting`.

### The process's own task

The process task stays `working` while the process runs, including while it needs a reason, and
ends `completed` after the last
stage is applied, `failed` with the reason otherwise, `canceled` when canceled. Its metadata,
rewritten through the outbox on every transition, carries `golemProcess`:

```json
{"state": "running", "stage": "design", "index": 1, "count": 3, "attempt": 0,
 "maxAttempts": 3, "staleReruns": 0, "stageTaskId": "...",
 "proposal": {"id", "kind", "state", "url"}}
```

`state` is the stage row's (`running` or `needs_reason` while the task is `working`).
`golemProposal` mirrors the current stage's proposal, so a client that knows only
[ADR 0015](0015-proposals.md) still sees what waits.

**Cancel.** `CancelTask` on the process task cancels the process run. The reconciler then
cancels the current stage's run, as the task service cancels any run, and withdraws its open
proposal: a process in `needs_reason` simply ends `canceled`; a `pending` or `failed` row becomes `rejected` with `decided_by` the person and
`detail` `process_canceled`, and an open merge request is closed with `state_event=close`. The
children a stage role delegated to are not canceled, as [ADR 0014](0014-golem-as-an-a2a-node.md)
records for delegation in general.

### Neighbours: a role chooses among a few

An agent that delegates declares whom, in its catalog:

```yaml
delegates:
  - agent: corsar-contract-checker
    when: The change touches a Kafka topic or an HTTP contract between components.
  - agent: corsar-docs-writer
    when: A Confluence page describes behaviour the change alters.
```

- At most seven entries, each an agent (never a process) with a `when` of 1 to 300 characters.
  A catalog with `delegates` must give some role `agents.delegate`, and one with a role holding
  `agents.delegate` must have `delegates`: the list is what the tool offers.
- **The tool sees only the list.** `delegate_to_agent`'s description lists the neighbours with
  their `when`, and its `agent` argument is an enumeration of their names. A name outside the
  list is a tool error before any call, so the model's mistake costs no edge round trip and no
  audit row of a refusal.
- **The registry for agent callers is derived, not written.** The edge already loads every
  pinned catalog for cards. At start it builds the registry as the file's entries plus, for every
  agent X and every neighbour N of X, `agent:X` among N's callers, and for every process P and
  every stage agent S of P, `agent:P` among S's callers. `evaluate` in `edge/policy.py` is
  unchanged; only what feeds `Registry.allowed_callers` is.
- **The file keeps the rest and loses agents.** `GOLEM_CALL_REGISTRY_FILE` still names people
  and services, and the edge refuses to start when it names an `agent:` caller (agents come
  from catalogs only), grants a `user:` caller, `user:*` included, to anything but a process,
  or names a callee, or a catalog names a neighbour, that no pinned catalog defines. Services
  may still be granted agents: [ADR 0017](0017-triggers.md)'s adapters start goal agents
  directly.

People see only processes as a result, with no rule of its own: the directory
([ADR 0014](0014-golem-as-an-a2a-node.md)) lists what the registry lets the caller call, and a
person may call only processes. Workers keep their cards, for Golem's own use and for partners
the registry may admit later.

A process adds a hop: person → process → stage agent → neighbour is depth 3.
`GOLEM_MAX_CHAIN_DEPTH` must be at least 3; the edge refuses to start below that when a process
is pinned.

### Routing is evaluated

A delegation choice is a behaviour of the agent, so it goes through the agent's gate
([ADR 0006](0006-evaluation-in-ci-first.md)). A golden-set case may state
`delegates: [<agent>, ...]`, the neighbours the role must call, or `delegates: []`, none. The
evaluation gives the runtime a `Delegation` whose transport records the calls instead of sending
them (the transport already exists for tests), and the check compares the agents called with the
case. A change to `delegates` or to a `when` is a catalog change like any other, and a case that
routed right in the baseline and routes wrong now fails the merge request.

A process has no golden set of its own in this step. Its stages are evaluated as agents, and the
loader's checks above are what a process's merge request must pass.

### As first built

- **People keep agents until the first process.** The edge refuses a `user:` grant on anything
  but a process only once `GOLEM_CATALOGS_DIR` pins at least one process, and asks for
  `GOLEM_MAX_CHAIN_DEPTH` of at least 3 under the same condition. A deployment without a process
  has nothing else for a person to start; the example catalogs pin none yet, because a process
  cannot run before its stages can ([ADR 0017](0017-triggers.md)'s goal mode and the process run
  above). `agent:` entries in the file and callees without a catalog are refused always.
- **A case states routing under `expect`**: `expect.delegates`, next to the outcome, role and
  target it is checked with. The evaluation's delegation tool is the runtime's, with a recorder
  as its transport and a placeholder call token, since no edge checks it.
- **A stage agent names its proposal kind explicitly.** `proposal` defaults to
  `merge_request` for today's catalogs; the loader's check reads whether the catalog wrote it.
- **One `process_stages` row per process, not per stage.** It holds where the process stands:
  the current stage, its attempt, its stale reruns, its run and task, the last rejection. The
  stages already run are the runs under the process run as their root. The row also pins the
  process file as it was at the start (`definition`) and the person's message (`input`), so a
  process that takes days runs the stages it started with, and the reconciler needs no catalog.
- **The task service reads processes from `GOLEM_CATALOGS_DIR`,** the edge's directory of
  pinned catalogs, not from `GOLEM_CATALOGS_FILE`: that file holds only git references for the
  Jobs to clone, and a process has no Job. Unset, the task service knows no process.
- **The message id counts stale reruns too:** `process-<process run id>-<stage index>-<attempt>-<stale
  reruns>`. A stale rerun keeps its attempt, and with the ADR's id it would reach the run that
  went stale instead of starting a new one. The reconciler finds a stage's run by that id and
  the owner, the run's caller, before it sends anything, so a pass that dies after sending
  adopts the run instead of starting another.
- **Refused or unavailable.** A stage the edge refuses (the registry, a malformed call) or
  admission rejects for good (the task comes back `REJECTED`, with the reason in its status)
  fails the process with that reason, as the table says for a refused run. A rejection whose
  `golemRefusal` is `caller_concurrency` or `chain_concurrency` passes as the owner's or the
  chain's other runs end, so it is tried again on the next pass; a spent budget stays spent,
  so `chain_budget` fails the process. An edge that does not answer, answers
  429 or 5xx, or answers without a task is tried again on the next pass with the same message
  id, and so is a stage task that ended as it started (`FAILED`, `CANCELED`): no run stands
  behind it, and its id is never kept.
- **The input is checked at the start.** A person's message that would make any stage's goal
  empty or longer than 4 000 characters refuses the process at once, with the stage named. A
  goal that still cannot be written at a stage's start fails the process with `invalid_goal`.
- **A qualifying comment** is the closer's newest non-system note written from ten minutes
  before the close to a minute after it. A comment from the review days earlier is not a reason,
  and the process waits for one.
- **A withdrawn stage run is told to its task.** Canceling the process cancels the current
  stage's run with `outcome` `withdrawn` and deletes its Job; the outbox delivers `withdrawn`
  like a final outcome, and the stage's task ends `canceled`, since nobody canceled it through
  A2A. A stage run found by its message id counts too, when the pass that would link it to the
  process did not get there. A stage run that succeeded but has no merge request yet (GitLab
  was down) is settled with nothing proposed, so no later pass opens one that nobody would
  close.
- **An accepted stage proposal is not withdrawn.** Its apply may be writing already, and
  rejecting the row would not stop the write, only hide it. It ends as its apply says, and the
  canceled process leaves it to that ([ADR 0015](0015-proposals.md), one apply at a time).
- **The reason of a rejection that is not a merge request** is the proposal row's `detail`,
  where the decision route of [ADR 0015](0015-proposals.md) will put it; a merge request's is the
  closer's comment as above.
- **The process's task learns through an outbox of its own.** `process_stages.view` is the
  `golemProcess` its task should show, recomputed every pass; `notified_view` is what the task
  service was last told. The reconciler posts the process run's id to
  `/internal/process-state` on the task service's internal-write port until the two match,
  and the task service reads the view from `golem_runs`, as for proposal states. A process that
  ended shows `state` `completed`, `failed` (with `reason`) or `canceled`.
- **The resolution route** takes a body of at most 16 KiB. The edge checks it before the audit
  row (a bad one is audited as `deny: malformed` or `deny: reason_required`), audits the task
  and the action but never the reason, a person's free text, and forwards it to the task
  service's a2a port with the principal. The task service checks the body again, the owner
  through its task store, and makes the compare-and-set.
- **The reconciler starts stages only when configured.** `GOLEM_EDGE_URL`,
  `GOLEM_RUN_TOKEN_KEY_FILE` and `GOLEM_RUN_TOKEN_KID` come together or not at all; without them
  it runs no process. The reconciler's NetworkPolicy gains egress to the edge's port 8000 and
  the edge's ingress admits it. In compose the two processes share the dev run token key through
  a volume.
- **A process is started by a person or a service,** never by an agent: a delegated call to a
  process is refused. Process runs are not in the run metrics.
- **No example process yet.** Pinning one in `examples/` would, by the rule above, forbid people
  the `discovery` agent that compose, the demo and the board's end-to-end tests start directly,
  and a process needs goal agents with a shared context repository that the examples do not
  have. `tests/test_process_runs.py` runs a three-stage process end to end instead.

## Consequences

- No model chooses among a hundred agents. The platform picks the stage, and a role picks among
  at most seven neighbours it is told when to use. Huyen's advice to remove tools that cost
  nothing becomes a catalog merge request that the golden set checks.
- The registry of who may call whom stops being a separate file to keep in step with the
  catalogs: an agent's neighbours and a process's stages are the registry, reviewed where they
  are written. The file shrinks to people and services.
- A person's decision is the gate between stages, and nothing waits for it: no Job idles, no
  Temporal is needed for a linear process. What still needs Temporal is a role that waits for a
  child's result inside its own run ([ADR 0014](0014-golem-as-an-a2a-node.md)).
- A process can take days. Its process run stays `running` that long; it holds no Job and no
  admission slot, but its call token's lifetime cannot be a Job deadline. The reconciler signs a
  fresh token for each start with a lifetime of five minutes, and revocation still asks about the
  process run.
- Stages hand over only through the context repository, so a process's agents share one. Work
  that spans repositories is several processes, or waits for a handoff this ADR does not define.
- A rejected stage costs a rerun and a person's reason. A merge request closed without a comment
  does not end the process: GitLab cannot say why, so the process asks its owner on the board,
  in the column for what waits for them. A stale proposal costs a rerun but not the return
  limit.
- Canceling a process cancels its current stage and withdraws its proposal; delegated children
  already started keep running, as for any delegation.
- The board shows processes, not agents ([ADR 0018](0018-board.md) as amended): a card is a
  process task with its current stage and that stage's proposal.
- The runtime, the catalog schema, the edge's registry loading, the task service's executor, the
  reconciler, the evaluation and the board each change. The process file's schema is Golem's own,
  as [ADR 0001](0001-industry-standards-and-a2a.md) says it would be.
