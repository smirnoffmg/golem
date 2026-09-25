# Getting started

The fastest way to your first result: give an agent a goal in the web UI, watch its task, and
open the merge request it proposes. You need an account at your organization's identity
provider that may use Golem, and the UI's address from your platform team (below,
`https://golem-ui.internal`). No other set-up.

The screenshots come from `scripts/ui_demo.py`, the real UI with an example agent; yours shows
your agents and your tasks.

## 1. Sign in

Open the UI's address and choose **Sign in**. You sign in at the identity provider as you do
for other internal tools, and come back to the list of your tasks.

![The sign-in page](../images/ui/sign-in.png)

The UI never shows or stores a password; it keeps your session for twelve hours at most, and
**Sign out** ends it at once.

## 2. Pick an agent

**Agents** lists the agents you may use, with what each one can do.

![The agents page](../images/ui/agents.png)

An agent works on one repository of records, its **context repository**: for the example
agent `discovery`, product hypotheses and solutions. Each run does one step on one record:
the researcher fills a hypothesis's evidence, the designer proposes a solution for a
validated hypothesis, the reviewer reviews a proposed solution. The agent picks the next record
that needs that step; you do not choose it.

## 3. Write a goal and start

Choose **Give discovery a task** (or **New task**), pick the agent, write a goal, **Start**.

![The new task form](../images/ui/new-task.png)

The goal is what the agent's role reads first: say what you want considered and where to
look ("from the support tickets of the last quarter"). It does not pick the record: the agent
works on the next record that needs work, which may be another one than you named. Clicking
**Start** twice starts one run.

## 4. Watch the task

**My tasks** lists your tasks, newest first, fifty to a page (**Older tasks** for more).

![My tasks](../images/ui/tasks.png)

A task's page shows its state and, once it has ended, the outcome. Reload the page to see a
change: nothing updates by itself.

![A working task](../images/ui/task-working.png)

| State | Means | What you do |
| --- | --- | --- |
| `submitted` | received, not yet admitted | wait a moment |
| `working` | the run is going: a role is writing its proposal, then the merge request is opened | wait; a run takes minutes, at most the platform's deadline (an hour by default). **Cancel** stops it |
| `completed` | the run ended well: the page links the merge request, or says there was nothing to do | open the merge request |
| `failed` | the run ended without a proposal; the message says why | read the reason; tell the agent's author if it is the agent's mistake, the platform team otherwise |
| `rejected` | the run was not started; the message says why (for example, you already have as many runs going as allowed) | wait for your other runs, then start again |
| `canceled` | you canceled it | nothing |

## 5. Open the merge request

A completed task links its merge request.

![A completed task with its merge request](../images/ui/task-completed.png)

The merge request is in the agent's context repository, from a branch
`golem/<record>/<run>`, and changes one record: for the researcher, the `Evidence` section of
one hypothesis. The task is complete when the proposal exists; whether it becomes part of the
record is a person's decision, taken in GitLab.

A task that says "succeeded and proposed no changes" found nothing to do: every record that
needs a step already has a proposal waiting for a decision.

## 6. Accept or reject

Accepting is merging the merge request; rejecting is recording the decision on the record.
Who may do either, and how to do it so the agent does not propose the same thing again:
[reviewing-proposals.md](reviewing-proposals.md).

## When something goes wrong

A failed run says why. This one's role changed a status, which only people may do, so nothing
was proposed:

![A failed task](../images/ui/task-failed.png)

A rejected run was never started; here the caller had four runs going, the limit:

![A rejected task](../images/ui/task-rejected.png)

A canceled task shows no outcome:

![A canceled task](../images/ui/task-canceled.png)

Starting many tasks within a minute is limited; the page says when to try again:

![The rate limit page](../images/ui/error-rate-limited.png)

The pages work on a phone:

![My tasks on a phone](../images/ui/tasks-narrow.png)

Tasks started from Jira or Mattermost are not in **My tasks**: they belong to the channel, and
the outcome comes back there ([channels.md](channels.md)). Programs can do everything the UI
does over A2A with your own token ([install.md, step 10](../operations/install.md#10-first-run)).
