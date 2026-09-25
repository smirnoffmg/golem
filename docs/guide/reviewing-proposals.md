# Reviewing proposals

For gate owners: the people who decide whether an agent's proposal becomes part of the record.
Golem only proposes; nothing reaches the default branch of a context repository without a
person merging it ([ADR 0004](../adr/0004-security-boundary-outside-the-job.md)).

## What a proposal is

One run, one merge request in the agent's context repository:

| Part | Looks like |
| --- | --- |
| source branch | `golem/<record id>/<run id>`, for example `golem/H-2/93ebcf70-752f-4ed5-b347-723c0928354e` |
| target branch | the one named for the agent in the platform's configuration (usually `main`) |
| title | `<agent>: <record id>`, for example `discovery: H-2` |
| description | `Proposed by agent <agent> in run <run id> for <record id>.` |
| author | the platform's bot account |
| commits | one: `golem: <role> on <record id>`, the role's summary, and the trailers `Run:`, `Role:`, `Target:` |
| "delete source branch" | set: merging removes the branch |

The run id leads to the run's task, trace and audit rows (ask the platform team). The role's
summary in the commit message is its own account of what it did.

## What validation already guarantees

Before the branch was pushed, the change passed every check of
[writing-an-agent.md](writing-an-agent.md#what-validation-checks):

- every changed file is in the role's directory (the researcher's in `hypotheses/`);
- the repository still parses: front matter, unique ids, links to existing records;
- no record's `status` changed;
- the target record, or a new record linking to it, changed.

What it does **not** check is whether the content is right: whether the evidence exists, the
sources are real, the reasoning holds. That is the review. Read the linked sources; a role is
told not to invent them, but a model can.

## Accept

Merge the merge request (after your project's approvals). The branch is deleted, so the record
is no longer pending, and the next run works from the new content: a hypothesis with its
evidence filled is no longer the researcher's target.

Then make the human decision the proposal prepares, if there is one: a researcher's evidence
may justify `status: validated` or `status: rejected` on the hypothesis, a reviewer's review
`accepted` or `rejected` on the solution. Change the `status` in the record, in a merge request
of your own or with a commit of your own on the proposal's branch before merging. Only people
change statuses; a run that tries fails validation.

## Reject

Record the decision on the record, then get rid of the proposal:

1. **Change the record's status** to what your decision means (`rejected` for a refuted
   hypothesis or a solution you will not build), in a merge request of your own. This is what
   stops the agent: the rules no longer match the record.
2. **Close the proposal's merge request and delete its branch** (the "Delete source branch"
   button on the closed merge request, or `git push origin --delete golem/<record id>/<run id>`).

Why both: the agent treats a record as pending while a `golem/<record id>/...` branch exists
([runtime/workspace.py](../../src/golem/runtime/workspace.py)). **Closing the merge request
alone keeps the branch**, so the record stays pending for ever: no decision is recorded and
the agent never looks at it again. Deleting the branch alone makes the record free again, and
the next run proposes the same step once more. Only the status change records a decision.

## A wrong proposal

Wrong content, and you want another attempt:

- close the merge request and delete the branch; the next run takes the record again;
- write a comment on what was wrong for the agent's author; a repeated mistake belongs in the
  agent's golden set as a case, so the gate catches it
  ([writing-an-agent.md](writing-an-agent.md#the-golden-set)).

Wrong content, and nobody should work on the record: change its status as in
[Reject](#reject).

A proposal that should not exist at all (another directory, a changed status, secrets in the
text): validation should have stopped the first two, so tell the platform team with the run id.

## Rules of thumb

- One merge request per record at a time: while one is open, the agent works on other records.
  An old open proposal blocks its record; decide it or close it.
- Edit the proposal's branch freely before merging (fix a phrase, add a source); the agent
  never touches a branch after pushing it.
- Merge conflicts mean the record changed since the run; close the proposal and delete its
  branch, and a new run starts from the current record.
