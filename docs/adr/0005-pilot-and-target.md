# 5. Separate the pilot from the target picture

## Status

Accepted, 2026-09-24

## Context

The architecture diagrams describe more than the first release needs: agents of other
platforms, long agent-to-agent delegation, durable workflow orchestration, confirmation before
mutating operations. Building all of it up front delays the first useful run and bets on needs
that have not been confirmed. "You build the simplest thing that can possibly work... most of
the time you aren't going to need it" (*Refactoring*, 1st ed. (Fowler), p. 58).

Quality evaluation, by contrast, cannot be deferred: evaluation is about detecting failures,
identifying failure modes and measuring how often each happens (*AI Engineering*, p. 298). An
agent that changes without that check is a regression waiting to happen.

The orchestration pattern — a central controller directing every step — fits "cases where
strict ordering, visibility, and control matter more than service autonomy" (*Building Complex
Multi-Agent Systems*, p. 220). Golem
orchestrates from the start; the open question is only which engine runs the workflows.

## Decision

**Pilot:**

- entry over A2A; Golem's own agents call each other synchronously with a timeout;
- results delivered as merge requests; the human answer is an accepted merge request;
- workflows run as plain code over `golem_runs`;
- quality evaluation on every merge request to an agent catalog:
  1. the merge request starts a GitLab CI pipeline;
  2. the CI job, an A2A client, submits "evaluate this catalog version" and waits;
  3. the orchestrator runs the branch version over the golden set, then the judge;
  4. the scores are compared with the last merged version in Langfuse experiments;
  5. below the threshold the job fails and "Pipelines must succeed" blocks the merge.

**Target** (drawn pale and dashed on the diagrams):

- agents of other platforms in both directions, with outbound circuit breakers;
- long agent-to-agent delegation: a run ends in "waiting for task X" and a new Job continues
  when X completes;
- Temporal (with its `temporal` and `temporal_visibility` databases) for history, timers,
  signals and retries;
- confirmation before a mutating operation through an A2A extension.

Temporal moves into the pilot if the first batch of scenarios includes long agent-to-agent calls.

## Consequences

- The pilot has fewer moving parts; Temporal and cross-platform trust are not operated until
  needed.
- Workflow code must keep activities (create Job, open merge request) separate from workflow
  logic so it can move onto Temporal without a redesign.
- An agent changes only through the same gate as code, from the first release.
