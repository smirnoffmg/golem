# 15. Proposals: a run's result waits for a person, and the platform applies it

## Status

Proposed, 2026-09-28

## Context

Every run that changes anything ends the same way today: the runtime pushes a branch of the
agent's context repository, the reconciler opens a merge request, and a person merges or closes
it in GitLab (`orchestrator/merge_requests.py`). That is the whole of Golem's write path. The
platform MCP servers serve read tools only, `tracker.read` and `wiki.read`
([ADR 0008](0008-platform-mcp-servers.md)), and everything inside a Job is untrusted
([ADR 0004](0004-security-boundary-outside-the-job.md)), so nothing a role does reaches Jira or
Confluence.

The agents that come next do not write code. One edits Confluence pages, one answers customer
requests in a service desk, one investigates alerts and asks for a Jira issue. Their results
live outside Git, and a merge request is the wrong shape for them: a reply to a customer is not
a diff to review in GitLab, and a mirror of Confluence in Git conflicts with everyone who edits
the page by hand.

Four constraints decide the shape:

- **A person decides every result, whatever its kind.** A merge request already works that way,
  and an agent that answers customers or edits the documentation others rely on is the case
  where an unreviewed result costs the most. Automation can grow later, from evidence.
- **The Job still writes nothing outward.** Whatever applies a result must hold its own secrets
  and act outside the Job, as the MCP servers do for reads.
- **Channel tasks have no human owner.** A task started from Jira or chat is owned by
  `service:<client id>` ([ADR 0010](0010-mattermost-adapter.md)), and only its owner can read it
  ([ADR 0011](0011-web-ui.md)). Under that model nobody could see, let alone accept, what the
  service desk agent proposes.
- **A page can change while its proposal waits.** A person may edit the page, or another run
  may, between the run that read it and the decision.

Sources:

- *AI Engineering* (Huyen), с. 31 (PDF 55): "Involving humans in AI's decision-making processes
  is called human-in-the-loop"; Microsoft's Crawl-Walk-Run, where "Crawl means human
  involvement is mandatory" and automation grows when, for example, "95% of AI-suggested
  responses to simple requests are used by human agents verbatim".
- *Архитектура корпоративных программных приложений* (Fowler), с. 92 (PDF 89): optimistic
  locking detects conflicts rather than preventing them, and "выполнить слияние нескольких
  версий одной и той же порции бизнес-данных намного сложнее, поэтому зачастую проще
  поступиться затратами времени и сил и все начать сначала".
- *Architecture Patterns with Python*, PDF 203: "Version numbers are just one way to implement
  optimistic locking [...] Version numbers also make implicit concepts explicit."
- Confluence Cloud REST API v2, Page
  (<https://developer.atlassian.com/cloud/confluence/rest/v2/api-group-page/>):
  `GET /wiki/api/v2/pages/{id}` with `body-format` (`storage`), a `version` of `number`,
  `message`, `authorId`, `createdAt`; `PUT /wiki/api/v2/pages/{id}` with `id`, `status`,
  `title`, `body` (`representation`, `value`) and `version` (`number`, `message`), answering
  409 "if the page version does not match the current version".
- Confluence Data Center REST API 8.1.1, `PUT /rest/api/content/{contentId}`
  (<https://docs.atlassian.com/ConfluenceServer/rest/8.1.1/>): "To update a piece of content you
  must increment the version.number, supplying the number of the version you are creating";
  the documented answers are 200, 400 and 404, no 409.
- Jira Service Management REST API, `POST /rest/servicedeskapi/request/{issueIdOrKey}/comment`
  (Cloud: <https://developer.atlassian.com/cloud/jira/service-desk/rest/api-group-request/>;
  Data Center 5.12: <https://docs.atlassian.com/jira-servicedesk/REST/5.12.0/>): "Creates a
  public or internal comment on an existing customer request. The currently logged-in user will
  be the author of the comment. The comment visibility is set by the `public` field."
  `GET` on the same path lists comments, filtered by `public` and `internal`.
- Jira REST API v2 (Data Center 11.0.3,
  <https://developer.atlassian.com/server/jira/platform/rest/v11003/api-group-issue>; the same
  resources on Cloud): `POST /rest/api/2/issue` with `fields`, answered 201 with `id`, `key`,
  `self`; `POST /rest/api/2/issue/{issueIdOrKey}/comment` with `body`, answered 201.
- GitLab Merge requests API (<https://docs.gitlab.com/api/merge_requests/>): `state` is
  `opened`, `closed`, `merged` or `locked`, where `locked` is "short-lived and transitional";
  `merge_user` is "the user who merged this merge request, the user who set it to auto-merge,
  or `null`" (`merged_by` is deprecated); `closed_by` is "the user who closed the merge
  request"; `PUT /projects/:id/merge_requests/:merge_request_iid/merge` takes an optional `sha`
  which, if present, must match the HEAD of the source branch, otherwise the merge fails with
  409.
- GitLab Repository files API (<https://docs.gitlab.com/api/repository_files/>):
  `GET /projects/:id/repository/files/:file_path/raw` with `ref`, "Name of branch, tag, or
  commit".
- RFC 8693, section 4.1: the `act` claim is "a JSON object, and members in the JSON object are
  claims that identify the actor"; RFC 8785 for the canonical JSON the digest is taken over.

## Decision

A **proposal** is the result of a succeeded run that a person must accept or reject. It has a
kind, fixed per agent, and a state. The merge request stays what it is and becomes one kind among
four; the other three are applied by the platform after a person accepts them in Golem.

### Kinds

| Kind | What the run proposes | Where a person decides | What applies it |
| --- | --- | --- | --- |
| `merge_request` | the run's branch (as today) | GitLab: merge or close | the person, by merging |
| `wiki_edit` | a Confluence page's new body (storage format) and title, plus the page version the role read | Golem | `wiki.write` |
| `desk_reply` | a reply to a service desk request: request key, text, `public` (to the customer) or internal | Golem | `desk.write` |
| `tracker_issue` | a new Jira issue (project, type, summary, description), or a comment on an open issue the role found with `tracker.read` | Golem | `tracker.write` |

The catalog names the kind: `proposal: merge_request | wiki_edit | desk_reply | tracker_issue`
on the agent, `merge_request` when absent, so today's catalogs are unchanged. One kind per agent
keeps the validator, the evaluation's golden set and the reviewers' expectations about one thing.

### States

| State | Meaning | Next |
| --- | --- | --- |
| `pending` | waiting for a decision | `accepted`, `rejected`; for `wiki_edit` also `stale` |
| `accepted` | a person accepted it; the apply is in flight or will be retried | `applied`, `stale`, `failed` |
| `applied` | the platform made the change, or the merge request was merged | final |
| `rejected` | a person rejected it, or the merge request was closed | final |
| `stale` | the page changed since the role read it; nothing was written | final |
| `failed` | the upstream refused the change; `detail` says why | `accepted` again (a person retries), `rejected` |

A `merge_request` proposal follows its merge request: `opened` and `locked` are `pending`,
`merged` is `applied` with `merge_user` as the decider, `closed` is `rejected` with `closed_by`.
It never passes through `accepted`, and Golem refuses a decision on it (409 `decided_in_gitlab`):
the decision stays in GitLab, where the diff is. The reconciler checks each pending merge request
at most once per `GOLEM_MR_POLL_SECONDS` (300 s by default), oldest check first, at most 50 per
pass, so a pass stays short however many are open.

Every transition is one compare-and-set on the row (`UPDATE ... WHERE id = %s AND state IN
(...)`), so two people deciding at once, or a decision racing the reconciler, make one
transition and the other gets 409 `already_decided`.

### How a run produces a proposal

The run's branch stays the carrier, because it is the one thing a Job may write
([ADR 0007](0007-run-tokens.md): a branch-only Git token). The role writes the proposal's
content under its `writes` directory along with its record change, as today; after validation
the runtime writes one file of its own at the branch root, `golem-proposal.json`: the kind, the
kind's fields, and the paths of any body files. The validator refuses a run whose file is
missing, names another kind than the catalog's, or breaks the kind's limits (a page body over
200 000 characters, a reply over 30 000, a summary over 255, a project outside the agent's
grant), so an invalid proposal is an `invalid` run, not a row. A `merge_request` agent writes no
file.

For `wiki_edit` the role needs the page as Confluence stores it, not the plain text
`get_page` returns: `wiki.read` gains `get_page_source(page_id)`, the storage-format body, the
title and `version.number`, refused as a tool error above 200 000 characters instead of cut, since
a cut body proposed back would delete the rest of the page.

When the run settles, the reconciler reads `golem-proposal.json` and the files it names through
the Repository files API with `ref` = the branch's head commit, never the branch name, so what is
stored is what was validated at that commit. It checks everything again (the file comes from an
untrusted Job) and inserts a row into `proposals` in `golem_runs`: `id`, `run_id`, `task_id`
(the run's first task), `agent`, `owner` (the run's caller), `kind`, `state`, `payload` (JSON),
`digest` (SHA-256 of the RFC 8785 form of the payload), `commit`, `target` (the record the run
worked on), `url`, `decided_by`, `decided_at`, `detail`, `checked_at`, `notified_state`. `merge_requests.py` becomes the
`merge_request` case of this step: it opens the merge request as today and stores its URL and
iid. A run that has a proposal is settled only when its row exists (a goal run may end
without one, [ADR 0017](0017-triggers.md)), which keeps the outbox rule of
[ADR 0009](0009-deployment-on-kubernetes.md) (`FINAL_OUTCOME`) unchanged.

### The task shows it

The task's outcome, delivered through the existing outbox, carries the proposal: the final
status update's metadata, merged into the task's, holds `golemProposal` = `{"id", "kind",
"state", "url"}`, and the task gets one A2A artifact,
`proposal`, with the payload as a data part. The task ends `completed` as today; a proposal's
later life does not reopen it. When a row's state changes, the reconciler notifies the task
service on `internal-write` with `POST /internal/proposal-state` naming the proposal. As for run
outcomes, the body is only a pointer: the task service reads the row from `golem_runs` and
rewrites `metadata.golemProposal` on the stored task. `notified_state` is the outbox column: the
row is notified until it equals `state`.

### Who may decide

- **The owner**, when the owner is a person (`user:*`). A service or an agent never decides.
- **The agent's reviewers**: `reviewers: [user:alice, user:bob]` in the catalog, `user:`
  principals only. The edge reads them from the pinned catalogs it already loads for cards
  (`GOLEM_CATALOGS_DIR`), so reviewers change through the catalog's merge request and the
  platform's deploy of the new revision, like an agent's grant.

The right to decide is not the right to read the task: [ADR 0011](0011-web-ui.md)'s ownership stays as it is, and a
reviewer reads a channel task's proposal through the routes below, never through `GetTask`.

### Routes through the edge

The edge stays the only door. It serves three routes outside A2A, like `GET /agents`:

- `GET /proposals?agent=&state=&page=` — the proposals the caller may decide, newest first,
  50 a page; `state` takes one state or a comma-separated list
  ([ADR 0018](0018-board.md) asks for `pending,accepted,failed`);
- `GET /proposals/{id}` — one proposal with its payload; for `wiki_edit` also the page as it is
  now (below); when the proposal's task carries a `report` artifact
  ([ADR 0017](0017-triggers.md)), that text as `report`;
- `POST /proposals/{id}/decision` with `{"decision": "accept" | "reject", "reason"}`.
  Rejecting a proposal of a process stage requires a `reason` of 1 to 4 000 characters (400
  `reason_required`), since the stage reruns with it in its brief
  ([ADR 0019](0019-processes.md)); `GET /proposals` also takes `process=<name>`, matching the
  proposals of that process's stages. A `stale` stage proposal reruns the stage without
  spending its return limit, and a stage's merge request closed without a comment puts the
  process in `needs_reason` rather than failing it (both in [ADR 0019](0019-processes.md)).

Only identity provider tokens: a call token (an agent) gets 403 `agents_do_not_decide`. Each
request takes a token from the caller's `/a2a` bucket, and each is audited before it is served
(`operation` `ListProposals`, `ReadProposal` or `DecideProposal`, `target_system`
`proposals`, the proposal id and decision in `request`), failing closed as for calls. The edge
forwards to the task service's `a2a` port with the edge token, the principal header and
`X-Golem-Reviews`, the agents whose reviewers include the caller, built from nothing like the
principal. The task service answers only rows where `owner` = the principal or `agent` is in
that list; any other id is 404, so an id reveals nothing.

**What a person sees is the platform's reading, not the Job's.** A `wiki_edit` is judged by its
diff, and a base body taken from the Job's file could be forged to hide what the edit removes.
`GET /proposals/{id}` for a `wiki_edit` therefore calls `wiki.write`'s
`preview_page_edit(page_id)`, which returns the live body, title and version. The response
carries both bodies and both versions; when the live version is not the one the role read, the
row goes to `stale` there and then. Payloads are text: no client may render a page body or a
reply as HTML, because they were written in an untrusted Job.

### Applying

`accept` moves the row to `accepted` (recording `decided_by`, `decided_at` and the reason), then
the task service applies it at once through the kind's write server and records the result
(through the orchestrator's runs module it already runs, as for a cancel, so `golem_runs`
keeps one owner role):
`applied`, `stale` or `failed` with `detail`. The decision answers with the resulting state, so
the person sees the outcome. If the call times out (15 s) the row stays `accepted`; the
reconciler notifies the task service about `accepted` rows older than 60 s through the same
`POST /internal/proposal-state`, and the task service applies again. Every apply is idempotent
per proposal id, so a retry after a lost answer does not write twice:

- **`wiki_edit`.** Read the page (Cloud: `GET /wiki/api/v2/pages/{id}?body-format=storage`; Data
  Center: `GET /rest/api/content/{id}?expand=version,body.storage`). A version whose `message`
  carries `golem:<proposal id>` is this proposal already applied: `applied`. Any other version
  than the one the role read: `stale`, nothing written. Otherwise `PUT` with `version.number` =
  that version + 1 and `message` = `golem:<proposal id> accepted by <user>`. If the `PUT` fails
  (Cloud answers 409 on a version mismatch; Data Center documents no 409), the page is read once
  more and the same rules decide between `applied`, `stale` and `failed`. A stale page is not
  merged: merging two versions of prose is exactly what is simpler to start again than to
  reconcile.
- **`desk_reply`.** List the request's comments; a comment by the write server's account,
  created after `decided_at`, with exactly the proposed text and visibility, is this proposal
  already applied. Otherwise `POST /rest/servicedeskapi/request/{key}/comment` with `body` and
  `public`. No marker goes into the text: it is what the customer reads.
- **`tracker_issue`.** A new issue carries the label `golem-<first 12 hex of the proposal id>`;
  a JQL search for it finds one already created. A goal agent's issue also carries
  `golem-<target>`, for an alert `golem-alert-<hash>` ([ADR 0017](0017-triggers.md)), taken from
  the row's `target`, not from the Job's payload, so the next run on the same alert group finds
  it with `tracker.read` and proposes a comment instead. A comment ends with the line
  `Golem proposal <id>`, found by the Jira adapter's rule for its own markers (the marker ends
  the comment). Otherwise `POST /rest/api/2/issue` or `POST /rest/api/2/issue/{key}/comment`.

**Then the record lands.** A non-`merge_request` run's branch also carries its record change
(the reply drafted, the page proposed). When its proposal is `applied`, the reconciler opens the
branch's merge request and merges it with `sha` = the proposal's `commit`, so a branch that moved
since is not merged (409, left for a person). When it is `stale`, the reconciler deletes the
branch: the target is no longer pending (`pending_ids`), and the lead picks it again on the
fresh page. When it is `rejected`, the branch stays, as a closed merge request's branch does
today, and the target stays pending until a person changes the record or deletes the branch.

### The proposal token and the write servers (amending ADR 0008)

The task service calls a write server with a **proposal token** it issues per call, signed with
the run-token key and verified against the same JWKS (`/internal/run-keys`):

- `iss` `golem`, `aud` = the write server's canonical URI, one token per server as for run
  tokens ([ADR 0016](0016-observability-tools.md)), `sub` = the person who decided
  (`user:<name>`), `act` = `{"sub": "service:golem-tasks"}` (RFC 8693: the person decided, the
  platform acts), `proposal` = the id, `scope` = `preview` or `apply`, `tools` = the one write
  group, `digest` = the row's payload digest, `iat`, `exp` = `iat` + 120 s, `jti`.
- A write server refuses a token without `proposal` and `act`, and every read server refuses a
  token that carries `proposal`, so neither kind of token works at the other kind of server even
  if an audience were misconfigured. Write groups are never in an agent's grant, so no run token
  is ever issued for a write server's audience.
- The server checks the signature, the audience, its group in `tools`, and that the payload in
  the call hashes to `digest`, so a token applies exactly the content the person saw and nothing
  else. Before an `apply` it asks the task service (`GET /internal/proposals/{id}` on
  `internal-read`) whether the row is `accepted`; before a `preview`, whether it is `pending` or
  `failed`. Anything else is 401 `invalid_token`, like a stopped run's token. This is the
  revocation of [ADR 0008](0008-platform-mcp-servers.md), keyed on the proposal instead of the run, with the same 10 s cache.
- For a preview, `sub` is the person looking at it, so the audit records who read which page.

[ADR 0008](0008-platform-mcp-servers.md) changes in five ways:

1. **Write groups exist**: `wiki.write` (`preview_page_edit`, `apply_page_edit`), `desk.write`
   (`apply_reply`), `tracker.write` (`apply_issue`, `apply_comment`). Each runs as its own
   Deployment, one group per process as today.
2. **A write server accepts only proposal tokens**, and nothing but the task service reaches it:
   its NetworkPolicy admits the task service only, and no Job's egress admits it, so a Job cannot
   even open a connection, let alone present a token.
3. **Write servers use their own upstream accounts**, not the read servers'. Their scope is
   narrowed again on the server: `GOLEM_MCP_WIKI_SPACES`, `GOLEM_MCP_DESK_PROJECTS` and
   `GOLEM_MCP_TRACKER_PROJECTS` name where they may write; anything else is a tool error before
   any upstream call.
4. **The audit row names the person**: `account` = the token's `sub`, the `act` subject and the
   proposal id in `request`, `token=sha256:<16 hex>` as before. Page bodies and replies are not
   logged, only their digest.
5. **`wiki.read` gains `get_page_source`**, uncut, as above.

## Consequences

- Every kind of result now waits for a person, in one place with one set of states, and the
  board ([ADR 0018](0018-board.md)) can show "waiting for my decision" across agents and channels. The
  channel-ownership gap of [ADR 0010](0010-mattermost-adapter.md) is closed for decisions, not for reading tasks: a reviewer
  sees a channel task's proposal, not the task.
- The Job's boundary does not move. What leaves it is still a branch; what writes outward is a
  platform process holding its own secrets, reached only by the task service, and acting for a
  named person with a token bound to one payload.
- The task service grows a fourth role: it applies. Its egress now includes the write servers,
  and an outage of Jira or Confluence shows up as `failed` proposals, not as a failing task
  service. A decision waits up to 15 s for the upstream.
- The reviewers of an agent can accept what it proposes for anyone who started it; whoever
  controls the catalog's merge request controls who that is. The platform's deploy of a catalog
  revision is the second gate, as for grants.
- The reconciler's GitLab token is allowed to merge into the context repositories' target
  branches, and nowhere else: the platform gains write access to those branches. It merges
  only the commit a person accepted (`sha`), so the person's decision is taken once. Where
  the grant is not yet in place, the records of non-`merge_request` kinds land as merge
  requests a person merges.
- A `rejected` proposal keeps its target pending, as a closed merge request does today; the
  lead does not retry what a person rejected. A `stale` one is redone on the next run that
  reaches its target.
- Proposals do not expire. A reply nobody accepted for a week is still `pending`; the board shows
  its age, and a person rejects it. Expiry per kind is left until there is data on how long
  decisions take.
- A later change of a proposal's state updates the stored task but sends no push: the A2A task
  is final, and the channels see the effect itself (the reply, the page, the issue). An adapter
  that needs the change reads it with `GetTask`.
- The runtime stays record-driven for record agents. Agents that act on a goal without records
  (alert investigation) get a goal mode in the runtime, not a change to proposals
  ([ADR 0017](0017-triggers.md)).
- Deciding on the diff Golem shows depends on `preview_page_edit` reaching Confluence; if it
  cannot, the proposal page shows the proposed body alone and the decision is refused until the
  live page can be read.

## As first built

The kinds the platform applies, from the Job to the decision; the write servers themselves are
built separately.

- **`golem-proposal.json`.** One JSON object: `kind` and the kind's fields, body text in files
  under the role's `writes` directory, named by relative path.
  - `wiki_edit`: `page_id` (digits), `title` (one line, at most 255), `version` (the page version
    the role read, a positive integer), `body_file` (storage format, at most 200 000
    characters).
  - `desk_reply`: `request` (a request key, `SD-12`), `public` (required, no default: `true` the
    customer reads it, `false` an internal note), `text_file` (at most 30 000).
  - `tracker_issue`: `action` `create` with `project`, `issue_type`, `summary` (one line, at most
    255) and `description_file` (at most 30 000); or `action` `comment` with `issue` and
    `comment_file` (at most 30 000).

  The payload a person decides is the same object with each file replaced by its text (`body`,
  `text`, `description`, `comment`); a body over its limit, or empty, is refused, never cut.
  `golem.proposal_payload` does this one way for the runtime and the reconciler.
- **The role proposes through its tool.** An agent of a kind the platform applies gets
  `submit_proposal` with that kind's fields as its arguments; the tool checks them as the
  reconciler will and answers what to fix. A record run of such a kind must end with a proposal
  (`invalid` otherwise); a goal run without one reports ([ADR 0017](0017-triggers.md)). The
  runtime writes `golem-proposal.json` after validation, outside the role's directory.
- **No grant check on the project or space in the Job.** No catalog field grants projects or
  spaces, so the runtime and the reconciler check the shape only; `GOLEM_MCP_WIKI_SPACES`,
  `GOLEM_MCP_DESK_PROJECTS` and `GOLEM_MCP_TRACKER_PROJECTS` on the write servers are the check.
- **A run records its kind** (`runs.proposal_kind`) from the task service's pinned catalogs
  (`GOLEM_CATALOGS_DIR`); an agent not pinned there proposes a merge request, as before.
- **The digest** is SHA-256 of the payload's RFC 8785 form, computed without a library: the
  payload holds only objects, arrays, strings, booleans and integers a double carries exactly,
  and anything else is refused rather than approximated.
- **Columns.** `proposals` gains `digest`, `commit`, `reason` (a decision's words; a failed
  apply's cause stays in `detail`) and `landed_at`. A process reads a stage's rejection reason
  as `coalesce(reason, detail)`.
- **The task** gets the payload as its `proposal` artifact (id `proposal-<run id>`, one data
  part) next to `golemProposal`.
- **Reading and deciding.** Through the edge, answered from `golem_runs`:
  - `GET /proposals?agent=&state=&process=&page=` → `{"proposals": [...], "next"}`, 50 a page,
    newest first; an item is `{"id", "taskId", "agent", "kind", "state", "summary", "url",
    "owner", "createdAt", "decidedBy", "decidedAt"}`.
  - `GET /proposals/{id}` → the item and `payload`, `target`, `reason`, `detail`, `report`,
    `stage` (a process stage's proposal); for a `wiki_edit` still `pending` or `failed`, also
    `live` (`{"title", "version", "body"}`, or `null` with `liveError`), and `state` `stale` when
    the live version moved.
  - `POST /proposals/{id}/decision` `{"decision", "reason"}` → the proposal after the decision
    and its apply; 400 `malformed` or `reason_required`, 404 `not_found`, 409 `already_decided`
    or `decided_in_gitlab`.
  - `GET /reports?agent=&page=` → `{"reports": [{"taskId", "agent", "target", "completedAt",
    "summary"}], "next"}`, 20 a page; `GET /reports/{taskId}` → `{"taskId", "agent", "target",
    "completedAt", "text"}`. They are read from `golem_runs` (`runs.report` of a `reported`
    run), the system of record, rather than from `golem_tasks` as [ADR 0018](0018-board.md)
    has it; `target` is the record's file name.
  - The edge audits `ListProposals`, `ReadProposal`, `DecideProposal` (`id=` and `decision=`,
    never the reason), `ListReports` and `ReadReport`, and refuses what does not parse before
    it forwards anything.
- **The decision is not refused when the live page cannot be read.** The apply reads the page
  itself before writing, so an unreadable page ends the proposal `failed`, which a person may
  accept again; the proposal page shows the proposed body with `live: null`.
- **Applying.** The task service calls the write server's MCP endpoint (`GOLEM_WRITE_SERVERS_FILE`:
  per group its `url` and `resource`, the token's audience) with one proposal token per call.
  Without an entry for a group, an accepted proposal of its kind ends `failed` with the reason.
  An apply that raises or takes over 15 s leaves the row `accepted`; the reconciler names
  `accepted` rows decided over 60 s ago on `POST /internal/proposal-state`, at most once a minute
  each, and the task service applies them again.
- **The write servers' tools**, each answering JSON text:
  - `wiki.write`: `preview_page_edit(proposal_id, payload)` → `{"title", "version", "body"}`,
    token scope `preview`, subject the person looking; `apply_page_edit(proposal_id, payload)`.
  - `desk.write`: `apply_reply(proposal_id, payload, decided_at)`.
  - `tracker.write`: `apply_issue(proposal_id, payload, target)`; `apply_comment(proposal_id,
    payload)`.

  An apply answers `{"state": "applied" | "stale" | "failed", "detail"}`; a tool error is
  `failed` with its text. The server recomputes the digest of `payload` and compares it with the
  token's `digest`, and reads the decider from the token's `sub`.
- **The record lands.** For an `applied` proposal the reconciler opens the run's branch as a
  merge request (`<agent>: <target> (applied)`) and merges it with `sha` = the proposal's
  `commit`; a 409 leaves it open for a person. A `stale` proposal's branch is deleted, a
  `rejected` one's left. `landed_at` marks either, so each is done once.
