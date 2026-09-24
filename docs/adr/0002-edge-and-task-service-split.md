# 2. Split the A2A gateway into a stateless edge and a stateful task service

## Status

Accepted, 2026-09-24

## Context

ADR 0001 introduced one A2A gateway as the only door into the platform. As first drawn, it did
two different jobs: enforcing the boundary (authentication, token exchange, chain policy, rate
limits, audit) and running the task lifecycle (task store, executor, push notifications,
resuming after a human answer).

"The more functionality you are relying on within the gateway, the bigger the risk involved and
the bigger the impact of an outage"; a security component that fails open may suit systems
where availability comes first, but for financial or government systems it "is most likely
not" desired (*Mastering API Architecture*, p. 79). The two jobs also change at different rates: the
boundary rarely, the lifecycle together with the orchestrator.

## Decision

Two containers.

- **A2A edge**: stateless, two or more replicas. Protocol at the boundary, agent cards,
  authentication, token exchange, chain policy, per-caller rate limit, audit, outbound calls to
  other platforms. It **fails closed**: if it cannot verify something (Keycloak or the call
  registry is unavailable), it refuses. Registries and cards are cached from GitLab.
- **Task service**: stateful, owns `golem_tasks`. A2A request handler, task executor, task
  store, push notifications, resuming a task after a human answer. It is not reachable from
  outside; the network policy admits only the edge.

The edge forwards an ordinary A2A request, already carrying the hop token, to the task service.

Outbound calls from the edge to other platforms have a timeout and a circuit breaker per
platform. Integration points are the main source of cascading failures; timeouts, circuit
breakers and bulkheads are the countermeasures (*Release It!*, 1st ed., p. 43).

## Consequences

- The security boundary is small, stateless and easy to reason about; it can be reviewed and
  deployed on its own cadence.
- An outage of the task service makes the edge return errors that clients retry; it never makes
  the edge let unchecked requests through.
- One more network hop and one more deployment to operate.
- The edge knows who calls, on whose behalf and whether it is allowed; it knows nothing about
  what an agent does or what state a task is in.
- Clients retry, so run start must be idempotent: a retried request with the same message id
  creates a new A2A task, and the orchestrator deduplicates by (caller, message id).
- The task service scales behind a shared task store: cancel works from any replica. Only
  streaming subscription is bound to the replica holding the live task, and Golem does not
  offer streaming.
