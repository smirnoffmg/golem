# Starting runs from Jira and Mattermost

Besides the [web UI](getting-started.md), an agent can be started where the work is discussed:
a label on a Jira issue, or `/golem` in a Mattermost channel. The outcome comes back to the
same place. Which agents each channel may start is the platform team's configuration.

## Jira

**Start.** Add the agent's label to an issue. The labels are configured by the platform team;
the example maps `golem:discovery` to the agent `discovery`. The run's goal is the issue:
`Jira issue PROJ-123: <summary>`.

**Outcome.** When the run ends, one comment appears on the issue
([adapters/jira.py](../../src/golem/adapters/jira.py)):

```
Golem run completed.

Run 93ebcf70-752f-4ed5-b347-723c0928354e succeeded; merge request: https://gitlab.internal/product/discovery-context/-/merge_requests/1

golem-task:02f15a50-92ef-4d88-b2e5-f2a844165053
```

The first line is the state (`completed`, `failed`, `rejected`, `canceled`), then the outcome,
then the task id. A refusal is a comment too (for example `Golem run rejected.` with the quota
that was reached).

**Good to know:**

- Removing the label does nothing, and does not stop the run. Adding it again later is a new
  change of the issue and starts a new run.
- Jira retries a webhook it thinks failed; a retry starts no second run.
- There is no reply when the run starts: the comment is the only answer, and it can take the
  run's whole duration.
- The comment is sent once. If Jira refused it at that moment, it is lost; ask the platform team
  for the run's outcome by the issue key.

## Mattermost

**Start.** In a channel where Golem is enabled:

```
/golem discovery Collect evidence for H-4 from last quarter's support tickets
```

`<agent> <goal>`: the agent's name exactly, then the goal. You get a private reply at once:

```
Started discovery, task `02f15a50-92ef-4d88-b2e5-f2a844165053`. The outcome will be posted in this channel.
```

**Outcome.** One post in the channel, mentioning you
([adapters/mattermost.py](../../src/golem/adapters/mattermost.py)):

```
@alice Golem run completed.

Run 93ebcf70-752f-4ed5-b347-723c0928354e succeeded; merge request: https://gitlab.internal/product/discovery-context/-/merge_requests/1

Task `02f15a50-92ef-4d88-b2e5-f2a844165053`
```

**Private replies instead of a start:**

| Reply | Means |
| --- | --- |
| ``Usage: `/golem <agent> <goal>`, for example `/golem discovery Redesign the checkout`. Agents: discovery.`` | the agent is not one this channel may start, or the goal is missing |
| `Golem is not enabled in this channel.` | the team or channel is not in the adapter's allowlist |
| `Could not start <agent>: Golem did not accept the request.` | the platform refused or could not be reached; try again later, or ask the platform team |
| `Starting <agent> was not confirmed in time. If it started, the outcome will be posted here; check before asking again, a new command starts a new run.` | Golem did not answer within 20 s |

Each command is a new run, even with the same text. The outcome is posted once; if the post
failed (the bot was removed from the channel), you still have the task id from the private
reply.

**Setting up** (platform team, environment-specific): a custom slash command `/golem` with
request URL `https://<golem host>/mattermost/command` and method `POST`, its token in
`GOLEM_MATTERMOST_COMMAND_TOKEN`; a bot account whose access token is
`GOLEM_MATTERMOST_BOT_TOKEN`, added to every channel where the command is enabled; the team
ids in `GOLEM_MATTERMOST_TEAMS` and optionally channel ids in `GOLEM_MATTERMOST_CHANNELS`
([configuration.md](../operations/configuration.md#mattermost-adapter-python--m-golemadapters-mattermost)).

## Limits

| Limit | Jira | Mattermost |
| --- | --- | --- |
| requests per minute (per replica, per sending address) | 300, bursts of 100 | 120, bursts of 60 |
| runs going at once | the adapter's quota, shared by everyone who uses the channel (3 in the base configuration) | the same, its own quota |
| who may start | anyone who may label the issue | anyone in an enabled channel |

**Identity.** Golem cannot verify who added a label or typed a command: the run's caller is the
channel's adapter, not you ([ADR 0010](../adr/0010-mattermost-adapter.md)). Your name is in the
run's goal and, for Mattermost, the task's metadata, as the channel reported it. So:

- every user of a channel shares the adapter's quota: one busy channel can make others' starts
  `rejected` until runs end;
- the audit log records the adapter as the caller;
- your runs from a channel do not appear in **My tasks** in the UI.

The UI starts runs as you, with your own quota and audit trail. Use it when that matters.
