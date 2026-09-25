# 10. The Mattermost adapter: a shared command token, a service identity, one post per task

## Status

Accepted, 2026-09-25

## Context

People should start an agent from chat: `/golem <agent> <goal>` in a Mattermost channel, and
see the outcome in the same channel. The Jira adapter already shows the shape of a channel
adapter: an A2A client of the edge authenticated as a service with client credentials, a
deterministic message id so retries start one run, and a per-run HMAC push token on the push
notification config. Mattermost differs from Jira in three ways that decide this record: how a
command proves where it came from, who the command is from, and what counts as a duplicate.

Sources:

- Mattermost, custom slash commands
  (<https://developers.mattermost.com/integrate/slash-commands/custom/>): the request is a
  `POST` (or `GET`) with `application/x-www-form-urlencoded` fields `channel_id`,
  `channel_name`, `command`, `response_url`, `team_domain`, `team_id`, `text`, `token`,
  `trigger_id`, `user_id`, `user_name`, and "will contain an `Authorization` header with a
  bearer token. The bearer token should match the **Token** value [...] for a request to be
  considered valid"; the example shows `Authorization: Token <token>`. The response is JSON
  with `response_type` (`ephemeral` or `in_channel`) and `text`. "Delayed responses are useful
  when the action takes more than three seconds to perform", through `response_url`.
- Mattermost server source, `server/channels/app/command.go`: the request is built with
  `Authorization: Token <command token>` and nothing else that authenticates it, sent once
  with no retry, under a deadline of `OutgoingIntegrationRequestsTimeout`, which the
  integrations settings
  (<https://docs.mattermost.com/administration-guide/configure/integrations-configuration-settings>)
  describe as "The number of seconds to wait for external integration HTTP requests before
  timing out, including custom slash commands", default `30`.
- Mattermost server source, `GenerateTriggerId` in `server/public/model/integration_action.go`:
  a trigger id is a fresh random id, the user id, the time in milliseconds and the channel id,
  signed with the server's key; one per invocation.
- Mattermost API reference, CreatePost
  (<https://developers.mattermost.com/api-documentation/#/operations/CreatePost>):
  `POST /api/v4/posts` with `channel_id` and `message` required, `root_id`, `props` ("A
  general JSON property bag"), answered `201`; needs `create_post` in the channel. Bot accounts
  (<https://developers.mattermost.com/integrate/reference/bot-accounts/>) call the REST API with
  an access token as `Authorization: Bearer`, and must be added to the teams and channels they
  post in.
- Mattermost server source, `deduplicateCreatePost` in `server/channels/app/post.go`: a post
  whose `pending_post_id` was seen within `pendingPostIDsCacheTTL = 30 * time.Second` returns
  the post already created instead of a second one; "If there isn't one, we can't deduplicate".
  The field is not in the API reference's request schema.
- a2a-sdk 1.1.5, `BasePushNotificationSender._dispatch_notification`: one `POST` per push
  config and event; an error or a non-2xx answer is logged and not retried.
- *Designing Web APIs* (Jin, Sahni, Shevat), с. 43 (PDF 57): verification tokens are simple
  but "offer limited security because they are sent in plain text with every request. If a
  verification token is leaked or compromised, an attacker can forge WebHook requests"; an
  HMAC signature of the body is stronger, and a timestamp in it prevents replays.
- The author's notes, *Token Exchange — доступ от имени пользователя*, section «Delegation
  против impersonation»: in RFC 8693 delegation the token carries an `act` claim, "user X,
  service Y acts on their behalf", and downstream sees both; for agents this is the right
  mode, because the audit must tell a person's action from a tool's action on their behalf
  (translated).

## Decision

`python -m golem.adapters mattermost` serves `POST /mattermost/command` and `POST /a2a/push`.
The parts the Jira adapter shares with it move to `golem/adapters/common.py`: the client
credentials token cache, push token issue and check, the `SendMessage` body and its call to the
edge, push parsing. `python -m golem.adapters` without an argument is still the Jira adapter;
the manifests now name `jira` or `mattermost` explicitly.

**The command token is the only proof, so it is checked strictly and backed by the network.**
Mattermost signs nothing: no HMAC of the body, no timestamp. The adapter requires
`Authorization: Token <GOLEM_MATTERMOST_COMMAND_TOKEN>` and compares it in constant time; a
missing header, another scheme, the token only in the form body, or a wrong token is a 401
before anything else is read. The compensating controls:

- *Network.* The `mattermost-adapter` NetworkPolicy admits port 8000 only from the Mattermost
  server's address (placeholder `203.0.113.30/32`) and from the task service (pushes). A leaked
  token is useless from anywhere else in or outside the cluster, as long as the policy holds.
- *Allowlists.* `GOLEM_MATTERMOST_TEAMS` (required) and `GOLEM_MATTERMOST_CHANNELS` (optional;
  empty means every channel of those teams) name where the command works; elsewhere it answers
  privately that Golem is not enabled and starts nothing. `GOLEM_MATTERMOST_AGENTS` names the
  agents it may start; the edge's call registry must also allow
  `service:<GOLEM_OIDC_CLIENT_ID>` for each, so either list can narrow, neither can widen.
- *Replay.* The A2A message id is `mattermost:` plus a hash of `trigger_id`, which Mattermost
  mints once per invocation. A replayed or duplicated request carries the same id, and the
  orchestrator starts one run per (caller, message id). Typing the command again is a new
  invocation and a new run, on purpose.

**Usage and replies.** `<agent>` must be one of the allowed agents, exactly; an unknown agent,
no goal or an empty command gets a private (`ephemeral`) usage reply and no run, no token grant.
A started run gets a private reply with the task id at once: the adapter answers after one
token grant and one `SendMessage`, bounded at 20 s, below Mattermost's default 30 s deadline;
past the bound it answers that the start was not confirmed and that the outcome will be posted
if it started. A refused or failed start is a private reply too, never an error page. The
`response_url` is not used: the outcome can arrive hours later, and a response URL takes five
messages within 30 minutes.

**Identity: the service, on behalf of a chat user it cannot prove.** The adapter calls the
edge with its own client credentials token, so the edge authenticates and audits
`service:golem-mattermost-adapter`, the run's caller and its run token's `caller` are that
service, and admission counts it as one caller. The chat user's id and name, the team and
the channel go into the message's `metadata` (`{"mattermost": {"userId", "userName",
"teamId", "channelId"}}`) and the goal text ("Asked in Mattermost by @alan (user id ...) in
~town-square (...)"), so they are in the stored task and the run's goal. They are what the command said, not something proven: anyone holding the
command token, on an admitted address, can name any user.

**Outcome: one post per task, posted in the channel.** A terminal push (`completed`, `failed`,
`canceled`, `rejected`) becomes one `POST /api/v4/posts` with the bot's token: a normal post in
the originating channel mentioning `@<user>`, with the state, the reason or the merge request
URL the task's message carries, and the task id; `props` carries `golem_task_id`. It cannot
thread under the reply to the command: an ephemeral post is not a post others can see or answer.
Non-terminal pushes are acknowledged with 204. The push token names the channel, the user and
the run (`<message id>:<channel>:<user id>:<user name>`, HMAC-signed with
`GOLEM_PUSH_TOKEN_SECRET` together with its issue time), so a push is posted only where a command
of this adapter started it, and only for 24 hours; anything else is a 401.

**Duplicates: no lookup, no state.** The Jira adapter searches the issue's comments for the
task id before commenting. Here a lookup would page through a channel's history, and it is not
needed, because the task service tells each task its outcome at most once: the sender makes
one attempt per event, the executor finishes a task once, and the outcome route leaves an ended
task as it is, so a reconciler that reports an outcome twice produces one push
(`test_the_outcome_reaches_the_channel_once_however_often_it_is_reported` runs this through the
real task service). The one case with two terminal pushes for one run is a replayed command:
the same message id is a second A2A task of the same run, and the reconciler tells every task of
a run in the same pass. Both posts carry `pending_post_id` = `golem-<run>`, which the Mattermost
server collapses into one within 30 seconds.

## Consequences

- **A documented gap for the security review: the caller is not the subject.** The rule that a
  run's caller and the person it acts for are one principal does not hold for chat. The edge
  proves the adapter, not the user; the user is an unverified claim in metadata. Nothing
  downstream can tell "the adapter" from "the user through the adapter" by token: the audit
  rows, admission quotas, rate limits and run tokens all name `service:golem-mattermost-adapter`,
  so every chat user shares one quota, and the run acts with the agent's grant whichever
  member of the channel asked. Closing the gap needs a token for the user: token exchange (RFC 8693,
  delegation with `act`) from a user token the identity provider trusts, which a slash command
  never carries. Options, none built: the identity provider as Mattermost's login with a user
  token the adapter can exchange; a one-time account link per user (the user signs in to the
  identity provider once, the adapter keeps their refresh token); or allowing only agents whose
  grants are safe for any member of the allowed channels. Until then, keep
  `GOLEM_MATTERMOST_AGENTS` and the channel allowlist to what every member of those channels may
  do.
- **A leaked command token forges commands** as any user, within the allowed teams, channels
  and agents, from an admitted address, bounded by the service's admission quota. Rotating it is
  regenerating it in Mattermost, updating the Secret and restarting the adapter; commands fail
  in between. If commands must come through the ingress controller, the network control moves
  there (a source allowlist on the route), and the policy admits the controller's namespace.
- **At most once, not at least once.** The sender does not retry, so a post that fails
  (Mattermost down, the bot removed from the channel) is lost; the push answers 502 and the
  sender logs it. The user still has the task id from the private reply. The Jira adapter has
  the same property with the same sender.
- The replay dedupe rests on server behavior outside the API reference: without it a replayed
  command costs one duplicate post, not a second run. The 30-second window is enough because
  both tasks are told in one reconciler pass; a pass that fails between them may post twice.
- The adapter keeps no state and needs no database; it runs as one more Deployment with its own
  service account without a token, and its own push token secret.
- The bot account must be a member of every allowed channel to post there, which also keeps it
  out of channels nobody added it to.
