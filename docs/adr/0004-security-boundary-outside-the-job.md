# 4. The security boundary runs outside the Job

## Status

Accepted, 2026-09-24

## Context

A run executes in a Kubernetes Job: it clones the agent catalog and context, lets a model drive
an agent loop with shell access, and writes a result to a branch. The agent library offers path
and tool permissions, but they are not applied when the backend can execute commands, and the
Job needs a shell.

If a trust boundary crosses an element rather than a data flow, the element should be split
(*Threat Modeling* (Shostack), p. 50). A Job with a model and a shell inside is exactly such an
element: some of its code is ours, the decisions are the model's.

## Decision

Everything inside a Job is untrusted. The boundary is enforced by things the model cannot change:

- **Network policy**: default deny; a Job may reach exactly five destinations: the A2A edge, the
  model gateway, Langfuse, the platform MCP servers, and GitLab.
- **GitLab token**: scoped to the run's own branch. Merge requests are opened by the
  orchestrator, not by the Job.
- **Platform MCP servers** (Jira, Confluence, GitLab) hold their secrets themselves, accept
  calls authorized by the run token, and write every action to the audit log. The Job never
  sees those secrets.
- **A2A edge**: the Job's "ask an agent" calls go through the same authentication, chain policy
  and audit as any external caller.

Path and tool permissions inside the Job stay, as protection against model mistakes, not
attacks.

Protection from others' failures and overload is layered, since every incoming task spawns a
Job:

- rate limit per caller at the edge;
- run admission in the orchestrator with Job quotas per caller and per root chain;
- `ResourceQuota` on Job count, CPU and memory in the Jobs namespace;
- timeouts and circuit breakers on outbound calls (*Release It!*, 1st ed., p. 43).

## Consequences

- A compromised or confused run can at worst write its own branch, spend its own budget, and
  call tools and agents it was allowed to; each action is audited outside the Job.
- The Job image can use whatever agent library is convenient; security does not depend on its
  permission model.
- Every new destination a Job needs is a network policy change and a security review.
