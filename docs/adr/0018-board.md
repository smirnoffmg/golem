# 18. The board: a React client over a JSON backend-for-frontend

## Status

Proposed, 2026-09-28

## Context

The web UI of [ADR 0011](0011-web-ui.md) is a set of server-rendered pages: a fixed list of agents
(`GOLEM_UI_AGENTS`), "my tasks" as one flat list of 50, a task page, a form to start a task and a
button to cancel one. It has no script at all. That was enough to start a run from a browser. It
is not enough for how people now work with Golem:

- **Work is per agent.** A person looks at one agent's work at a time: what is running, what
  waits for them, what is ready to review. A flat list mixes all agents together.
- **Results wait for a decision.** With [ADR 0015](0015-proposals.md) a run's result is a
  proposal that a person accepts or rejects. Some proposals belong to tasks the person never
  started: a channel task is owned by `service:<client id>`, and the person sees its proposal
  only as one of the agent's reviewers. A page for that has to show a Confluence diff and a
  customer reply side by side with the person's own tasks.
- **Some results are only reports.** A goal agent started by an alert or a schedule
  ([ADR 0017](0017-triggers.md)) often finds nothing to act on. Its run ends `reported`: the
  investigation is an A2A artifact `report` on a task owned by `service:golem-alertmanager-adapter`
  or `service:golem-scheduler`, and no proposal exists. [ADR 0017](0017-triggers.md) left open who may read it. The
  people who answer for the agent should, or a quiet investigation is lost.
- **Status changes while you look.** A run takes minutes. A page that shows it has to update
  itself without being reloaded by hand, and reloading the whole page would also throw away a
  half-written goal or reply.

Four facts about the current code shape the answer:

- **The task does not know its agent.** A2A tasks do not keep the tenant. The UI writes the
  agent into the first message's metadata (`golemAgent`), which is the caller's own claim, and
  `ListTasks` cannot filter by it: a2a-sdk 1.1.5's `DatabaseTaskStore.list` filters by owner,
  `context_id`, one `status` and `status_timestamp_after`, never by tenant. Filtering in the UI
  would mean walking every page of the person's tasks on every refresh.
- **The status timestamp is the list's clock.** The store's `last_updated` column is the task
  status's `timestamp`, and `status_timestamp_after` filters with `>=`. A change that does not
  touch the status, such as [ADR 0015](0015-proposals.md) rewriting `metadata.golemProposal`, does not move it.
- **Status updates merge into the task's metadata.** `TaskManager` merges the metadata of every
  status update event into `task.metadata` (`task.metadata.MergeFrom(event.metadata)`), and the
  executor already puts `runId` there when the run starts.
- **The edge limits every caller.** Each call to `/a2a` and each authenticated `GET /agents`
  takes a token from the principal's bucket: 60 a minute, burst 20, per edge replica
  ([ADR 0012](0012-rate-limits.md)). Polling has to fit inside it.

The browser security of [ADR 0011](0011-web-ui.md) is not up for discussion: tokens stay on the server, one opaque
`__Host-` session cookie, CSRF tokens on every state change, the same response headers.

Sources:

- OWASP *Application Security Verification Standard 5.0*:
  - с. 35 (PDF 36), 3.5.1: requests to sensitive functionality that do not rely on the CORS
    preflight mechanism must be validated to "originate from the application itself. This may be
    done by using and validating anti‑forgery tokens or requiring extra HTTP header fields that
    are not CORS‑safelisted request‑header fields"; 3.5.2: when relying on preflight, "it is not
    possible to call the functionality with a request which does not trigger a CORS‑preflight
    request. This may require checking the values of the 'Origin' and 'Content‑Type' request
    header fields"; 3.4.8: `Cross‑Origin‑Opener‑Policy` "same‑origin" on responses "that initiate
    a document rendering".
  - The items [ADR 0011](0011-web-ui.md) cites for cookies, headers, sessions and tokens (3.3.1 to 3.3.5, 3.4.1 to
    3.4.6, 7.2.3, 7.2.4, 7.3.2, 7.4.1, 7.4.4, 10.1.1, 10.1.2, 10.2.1, 10.4.6), which still apply.
- RFC 6797, section 8.1: "If an HTTP response is received over insecure transport, the UA MUST
  ignore any present STS header field(s)."
- a2a-sdk 1.1.5: `a2a/server/tasks/database_task_store.py` (`list`: `where(owner == owner)`,
  then `context_id`, `status['state']`, `last_updated >= status_timestamp_after`, ordered by
  `last_updated` descending and id; `_to_orm`: `last_updated` = `task.status.timestamp`),
  `a2a/server/tasks/task_manager.py` (`task.metadata.MergeFrom(event.metadata)` for a status
  update), `a2a/server/routes/jsonrpc_dispatcher.py` (`call_context.tenant = request.tenant`),
  `a2a/utils/task.py` (`validate_page_size`: 1 to `MAX_LIST_TASKS_PAGE_SIZE` = 100).
- TanStack Query v5, `useQuery` options
  (<https://tanstack.com/query/latest/docs/framework/react/reference/useQuery>): `refetchInterval`
  "`number | false | ((query) => number | false | undefined)`": "If set to a number, the query
  will continuously refetch at this frequency in milliseconds. If set to a function, the function
  will be executed with the latest data and query to compute a frequency";
  `refetchIntervalInBackground`, default `false`: "If set to `true`, the query will continue to
  refetch while their tab/window is in the background"; `retry` "Defaults to `3` on the client".
- Vite build options (<https://vite.dev/config/build-options>): `build.assetsDir` default
  `assets`; output names `[name]-[hash]` for entries, chunks and assets; `build.assetsInlineLimit`
  default 4096, "Set to `0` to disable inlining altogether", and the features guide's CSP note
  that inlined assets need `data:` in `img-src` or `font-src` unless inlining is disabled;
  `build.modulePreload`: the polyfill "is auto injected into the proxy module of each
  `index.html` entry", a module, not an inline script; `server.proxy` for the development server.
- nginx `ngx_http_headers_module` (<https://nginx.org/en/docs/http/ngx_http_headers_module.html>):
  `add_header` adds the field for 200, 201, 204, 206, 301, 302, 303, 304, 307 and 308 only,
  unless "the `always` parameter is specified"; the value "can contain variables"; the
  directives "are inherited from the previous configuration level if and only if there are no
  `add_header` directives defined on the current level".
- Python `difflib.SequenceMatcher.get_opcodes()`: `'replace'`, `'delete'`, `'insert'`,
  `'equal'` operations over two sequences.

## Decision

The UI becomes two containers behind one host. **`golem.ui` stays the backend-for-frontend** of
[ADR 0011](0011-web-ui.md), with the same sign-in, sessions, cookies and headers, but it answers JSON instead of
pages. **A new `board` container** serves a static React application built with Vite
(TypeScript, TanStack Query) from nginx. The browser talks only to its own origin.

### One origin, two containers

The ingress routes the UI's host by path:

| Path | Service | What |
| --- | --- | --- |
| `/api/` (prefix) | `ui:8000` | the JSON API below |
| `/login`, `/callback`, `/logout` (exact) | `ui:8000` | sign-in and sign-out, as in [ADR 0011](0011-web-ui.md) |
| everything else | `board:8080` | `index.html` and `/assets/*` |

Same origin means no CORS: the BFF sends no `Access-Control-*` header, so a cross-origin script
can neither read an answer nor send a JSON body without a preflight that fails. The session
cookie stays `__Host-golem-session` with `Path=/`, and the browser sends it to both services; the
static one never reads it.

The Jinja environment, the templates, `views.py`'s HTML concerns, the stylesheet route and
`GOLEM_UI_AGENTS` are removed from `golem.ui`. What stays is `oidc.py`, `store.py`, `edge.py`
and the [ADR 0011](0011-web-ui.md) rules they implement. The readiness probe moves from `/static/golem.css` to
`GET /healthz`, which touches neither a session nor the database.

### Sign-in and sign-out without pages

- `/login` and `/callback` are unchanged redirects. A callback that fails no longer renders an
  error page: it redirects to `/?signin=<code>`, where `<code>` is one of `expired`, `refused`,
  `failed`, and the board shows a fixed message for each. The identity provider's `error` value
  is never echoed into the page.
- `GET /api/session` answers `{"name", "csrf", "expiresAt"}` for a live session, `401
  {"error": "unauthenticated"}` otherwise. The board keeps the CSRF token in memory only, never
  in `localStorage` or a cookie.
- `POST /logout` with the CSRF header deletes the session as before and answers `{"redirect":
  <end_session URL> | "/"}`; the board navigates there with `location.assign`. [ADR 0011](0011-web-ui.md)'s
  refresh page existed only because CSP `form-action` blocks a redirect that follows a form's
  `POST`. A script's navigation is not a form submission, so the workaround goes.
- Any `/api/` request whose session is gone, whose refresh failed, or whose token the edge
  refused answers `401` with a JSON body, never a redirect: a `fetch` must not follow a redirect
  to the identity provider. The board then navigates to `/login`.

### CSRF for a JSON API

Every request to the BFF other than `GET` and `HEAD` (every `POST` under `/api/`, and `POST
/logout`) must:

1. carry `X-Golem-CSRF` equal to the session's CSRF token, compared in constant time; and
2. carry `Content-Type: application/json` (415 otherwise), with a body of at most 16 KiB.

A missing or wrong token is 403 before anything else happens, as in [ADR 0011](0011-web-ui.md). Either condition
alone meets 3.5.1: the token is an anti-forgery token, and both the custom header and the JSON
content type are outside the CORS-safelisted set, so a cross-site page cannot send the request
without a preflight, which the BFF never answers with permission. `SameSite=Lax` keeps the
cookie off cross-site `POST`s as a third layer. No `GET` changes state.

### The JSON API

All answers carry `Cache-Control: no-store` and [ADR 0011](0011-web-ui.md)'s headers. Errors are `{"error":
<code>, "message": <text>}`: 400 malformed, 401 unauthenticated, 403 CSRF, 404 not found (also a
task of another owner or an unknown agent), 409 conflict (a proposal already decided, a task
already final), 429 with `Retry-After` when the edge or the BFF limited the caller, 502 when the
edge failed.

| Route | Edge call | What |
| --- | --- | --- |
| `GET /api/session` | none | the signed-in person and the CSRF token |
| `GET /api/agents` | `GET /agents` with the user's token | the directory ([ADR 0014](0014-golem-as-an-a2a-node.md)): name, description, skills |
| `GET /api/review-counts` | `GET /proposals?state=pending,failed` | proposals waiting for me, counted per agent |
| `GET /api/agents/{agent}/board?since=<cursor>` | `ListTasks`, `GET /proposals?agent=` | one agent's board, below |
| `GET /api/agents/{agent}/tasks?page=<token>` | `ListTasks` with `pageToken` | older tasks for the archive |
| `GET /api/agents/{agent}/tasks/{id}` | `GetTask` | one task: messages, artifacts, status |
| `POST /api/agents/{agent}/tasks` | `SendMessage` | start: `{"goal", "nonce"}` |
| `POST /api/agents/{agent}/tasks/{id}/messages` | `SendMessage` with `taskId` | reply to `input-required`: `{"text", "nonce"}` |
| `POST /api/agents/{agent}/tasks/{id}/cancel` | `CancelTask` | cancel |
| `GET /api/proposals/{id}` | `GET /proposals/{id}` | one proposal, with the diff for `wiki_edit` and the run's report if it has one |
| `GET /api/agents/{agent}/reports?page=` | `GET /reports?agent=&page=` | the agent's reports without a proposal, below |
| `GET /api/reports/{taskId}` | `GET /reports/{taskId}` | one report |
| `POST /api/proposals/{id}/decision` | `POST /proposals/{id}/decision` | `{"decision": "accept" \| "reject", "reason"}` |

The agents a person sees come from the edge's directory with their own token, so the left
column lists exactly the agents the call registry lets them call. The BFF keeps each session's
directory for 60 s, the edge's `max-age` for it. It answers 404 for an `{agent}` that is not in
that list, so a mistyped or forbidden name costs no edge call and no audit row. The edge still
decides every call.

Start and reply work as [ADR 0011](0011-web-ui.md)'s form did: the board draws a nonce (at least 128 random bits,
`crypto.getRandomValues`) when it opens a form, and the BFF sends it as message id `ui:<nonce>`,
so a double submit is one message. Goals and replies are 1 to 4000 characters. Both take a token
from the session's start bucket (`GOLEM_RATE_START`, [ADR 0012](0012-rate-limits.md)). The BFF no longer writes
`golemAgent` into the message's metadata; the task service records the agent (below).

A reply sends a message into the same task. Today no Golem run asks for input, and the task
service's executor ignores messages into a task that has a run. The column and the action exist
so that a run that can ask, or an agent of another platform, has a place to be answered. What a
reply then does to the run belongs to the ADR that lets a run ask.

### The task service records the agent, and `ListTasks` filters by it

- **Recording.** The executor's first status update of a new task, `WORKING`, carries
  `metadata={"golemAgent": <tenant>}`, and `TaskManager` merges it into `task.metadata`. The
  tenant is the one the edge forwarded and checked against the call registry, not the caller's
  claim. A task refused by admission already has the key, since the refusal follows that update.
  A task rejected for having no tenant has none.
- **Filtering.** The task service's store becomes `AgentTaskStore(DatabaseTaskStore)`. It
  overrides `list` with the SDK's query plus one predicate when the request names a tenant:
  `metadata->>'golemAgent' = tenant`. Owner scoping, the other filters, the order and the page
  token are the SDK's own. A test runs the SDK's `list` and this one over the same rows without a
  tenant and requires the same pages, so an SDK upgrade that changes the query fails the build
  instead of drifting. An expression index `(owner, (metadata->>'golemAgent'), last_updated
  DESC)` serves it.
- **Backfill.** A one-off migration sets `golemAgent` on existing tasks from `golem_runs` (the
  agent of the run that has the task's id), which the task service already reads. Tasks without a
  run are left alone and appear on no board.

This reverses a line of [ADR 0011](0011-web-ui.md): the tenant of `ListTasks` is now a filter. Adapters do not
list, and an A2A client that names a tenant now gets that agent's tasks, which is what naming a
tenant means.

### The board

`GET /api/agents/{agent}/board` answers:

```json
{
  "agent": "docs-writer",
  "complete": true,
  "cursor": "<opaque>",
  "tasks": [{"id", "state", "column", "goal", "message", "updated", "proposal"}],
  "proposals": [{"id", "taskId", "kind", "state", "column", "summary", "url", "owner", "age"}]
}
```

- **Without `since`** it is a snapshot (`complete: true`): one `ListTasks` page of 100, the
  most recently updated tasks, plus the agent's open proposals.
- **With `since`** it is a delta (`complete: false`): `ListTasks` with `statusTimestampAfter` =
  the cursor, page size 100, plus the open proposals again. The cursor is the newest status
  timestamp seen, minus 30 s: a task whose update committed just after a poll read the list
  still falls inside the next window. Tasks already seen come back and replace themselves. A
  delta that fills its page is answered as a snapshot instead.
- **`proposals` is always the whole open set**: `GET /proposals?agent=<agent>&state=pending,
  accepted,failed`, one page of 50. A proposal's change does not move its task's status
  timestamp, so a task delta would miss it; the open set is small, so it is sent whole.
  ([ADR 0015](0015-proposals.md)'s `state` parameter takes a comma-separated list.)
- `goal` is the first user message cut at 280 characters, `message` the status message cut at
  500, `summary` a proposal's one-line description (page title, request key, issue summary, or
  merge request title). They are text, and the board renders them as text.

The BFF assigns the column, as a pure function of the task, its proposal and the open set:

| Column | Tasks | Proposals |
| --- | --- | --- |
| In progress | `submitted`, `working`, and any state the BFF does not know | |
| Waiting for me | `input-required`, `auth-required` | |
| To review | `completed` whose proposal is in the open set | every open proposal: `pending`, `accepted` (being applied), `failed` (apply refused; accept again or reject) |
| Failed | `failed`, `rejected` | |
| Archive (collapsed) | `canceled`; `completed` without a proposal; `completed` whose proposal is `applied`, `rejected` or `stale` | |
| Reports (collapsed) | | none: reports without a proposal, loaded from `/api/agents/{agent}/reports` when opened (below) |

A proposal of the person's own task and the task itself are one card, joined by `taskId`. A
proposal the person may decide only as a reviewer is a card of its own, without a task link,
since the person cannot read that task ([ADR 0015](0015-proposals.md)). A `merge_request` proposal's card links to
GitLab and has no decision buttons: the decision stays there. The archive loads more with
`GET /api/agents/{agent}/tasks?page=`, 50 at a time, on request.

Tasks older than the snapshot's 100 do not appear until the archive is paged. An active task
older than a person's last 100 updates on one agent is not expected: a run is bounded by its
Job's deadline. If it happens, the task is still shown by the delta when its status next changes.

### Reports: reviewers read what a goal run found

A `reported` run leaves no proposal, so [ADR 0015](0015-proposals.md)'s routes do not show it. The edge gains two
more routes of the same kind, with the same rules: identity provider tokens only (a call token
gets 403 `agents_do_not_decide`), a token from the caller's bucket, an audit row before the
answer (`operation` `ListReports` or `ReadReport`, `target_system` `reports`), forwarding to the
task service with the principal header and `X-Golem-Reviews`:

- `GET /reports?agent=&page=` lists reports newest first, 20 a page, each `{"taskId", "agent",
  "target", "completedAt", "summary"}`. `target` is the run's `golemTarget` ([ADR 0017](0017-triggers.md)), and
  `summary` is the report's first line cut at 280 characters.
- `GET /reports/{taskId}` returns the `report` artifact's text (at most 20 000 characters, as
  [ADR 0017](0017-triggers.md) cuts it) with `agent`, `target` and `completedAt`.

The task service answers only tasks whose owner is the caller, or whose agent is in
`X-Golem-Reviews`. Anything else is 404, exactly as for proposals. A reviewer gets the report and
nothing else of the task: not its history, not the goal text the adapter wrote from the alert,
not its push configuration. [ADR 0011](0011-web-ui.md)'s ownership of the task stays as it is. The right to read a
report comes with the right to decide the agent's proposals, since a reviewer cannot judge an
agent whose quiet runs they never see.

The task service finds these tasks by two metadata keys it writes itself. `golemAgent` is
recorded as above. When the task service delivers a run outcome of `reported`, the final status
update carries `metadata={"golemOutcome": "reported"}`, merged into the task like `runId`. The
query is the task service's own SQL on `golem_tasks`, across owners, served by the partial index
`((metadata->>'golemAgent'), last_updated DESC) WHERE metadata->>'golemOutcome' = 'reported'`.

The Reports lane is collapsed and costs nothing while closed. Open, it loads the first page and
refetches it every 60 s. A goal run that did propose something shows its report on the proposal
instead: for a proposal whose task carries a `report` artifact, `GET /proposals/{id}` includes it
as `report`. The person deciding on the issue an investigation proposes reads the investigation
next to it. (This extends [ADR 0015](0015-proposals.md)'s answer, not its authorization.)

### Deciding

`GET /api/proposals/{id}` passes on the edge's answer. For `wiki_edit` the BFF adds a diff of
the live body against the proposed one, computed with `difflib.SequenceMatcher` over lines. The
storage format is split into lines before each opening block tag (`p`, `h1` to `h6`, `li`, `tr`,
`table`, `ac:structured-macro`), so a changed paragraph is one changed line, not one changed page.
The diff is a list of `{"op": "equal" | "insert" | "delete", "lines"}`, and runs of `equal`
longer than six lines are folded. `desk_reply` shows the text and whether it goes to the
customer; `tracker_issue` shows the project, type, summary and description, or the issue and the
comment. Nothing from a payload is ever rendered as HTML: the board has no
`dangerouslySetInnerHTML`, and a lint rule fails the build if one appears.

`POST /api/proposals/{id}/decision` answers with the resulting state from the edge (`applied`,
`stale`, `failed`, `rejected`, or `accepted` if the apply is still running). A 409 means someone
else decided first. In both cases the board refetches the agent's board at once.

### Updates: polling now, a stream later

The board polls with TanStack Query:

- the open agent's board every 10 s (`refetchInterval: 10_000`), with
  `refetchIntervalInBackground: false`, so a hidden tab does not poll;
- the review counts in the left column every 60 s;
- the Reports lane, only while it is open, every 60 s;
- the directory with `staleTime` 60 s and no interval.

`refetchInterval` is the function form. After a 429 it returns the `Retry-After` interval once,
then 10 s again. `retry` is off for 4xx and 2 attempts for 5xx and network errors. A 401 on any
query sends the browser to `/login`.

**Budget.** One visible board costs 2 edge calls per poll: 12 a minute, plus one for the review
counts, at most one for the directory and one for an open Reports lane. That is about 14 a
minute steady, 15 with the lane open, against 60 a minute
per principal per edge replica. A person can keep three boards visible at once, in separate
windows, and still start tasks and decide proposals without meeting a 429. With two edge
replicas the real headroom is about twice that, but the budget is sized for one replica, as
[ADR 0012](0012-rate-limits.md) advises. Opening the board takes four calls at once (session, directory, counts,
board), well inside the burst of 20.

**The stream later.** The client does not care how a board delta arrives. The client keeps a
map of tasks by id and replaces the open proposal set whole. Every delta is merged into it, and
a snapshot replaces it. A later `GET /api/agents/{agent}/events` (server-sent events) will send
the same two shapes as `task` and `proposals` events, with the cursor as the event id. A client
that switches to the stream changes its transport, not its model.

### The board container

- **Build.** Vite with `build.assetsInlineLimit: 0`, so no asset becomes a `data:` URL. The
  default output puts every script and stylesheet under `/assets/` with a content hash in its
  name. The built `index.html` loads them with `<script type="module" src>` and `<link>`, with
  no inline script. No CSS-in-JS that injects `<style>` elements, and no `style` attributes: CSS
  files only.
- **Served by nginx**, as a non-root user with a read-only root filesystem: port 8080, `pid`
  and the temporary paths under `/tmp` (an `emptyDir`), `server_tokens off`. Configuration is
  baked into the image; nothing is templated at start.
- **Routes.** `/assets/` answers the file or 404, never `index.html`. Every other path answers
  `index.html` (`try_files $uri /index.html`), so the board's own routes (`/agents/<name>`,
  `/agents/<name>/tasks/<id>`, `/proposals/<id>`) survive a reload.
- **Headers**, all `add_header ... always` and all at the `server` level, because a `location`
  with an `add_header` of its own would silently drop the rest:
  - `Content-Security-Policy: default-src 'self'; frame-ancestors 'none'; form-action 'self';
    base-uri 'none'; object-src 'none'`, the same policy as the BFF's;
  - `X-Content-Type-Options: nosniff`, `Referrer-Policy: same-origin`,
    `Cross-Origin-Opener-Policy: same-origin`;
  - `Strict-Transport-Security: max-age=31536000; includeSubDomains`, sent unconditionally.
    Browsers ignore it over plain HTTP (RFC 6797, 8.1), so local development over
    `http://localhost` is unaffected, and nothing needs templating.
  - `Cache-Control` from a `map` on the URI: `public, max-age=31536000, immutable` for
    `/assets/`, `no-cache` for everything else, so a deploy reaches browsers at once and a hashed
    file is never fetched twice.
- **Deployment.** A Deployment and Service `board` (two replicas), service account without a
  token, the same pod and container security context as `ui`. A NetworkPolicy admits the ingress
  controller's namespace on 8080 and allows no egress at all: the static server calls nothing.
- **Development.** `vite` serves the board and proxies `/api`, `/login`, `/callback` and
  `/logout` to a local BFF (`server.proxy`). The development server sets no CSP. The browser
  test of [ADR 0011](0011-web-ui.md) runs against the built image behind the same routing, so the policy is
  checked where it applies.

### What changes elsewhere

- `golem.settings`: `GOLEM_UI_AGENTS` goes away; the UI settings otherwise stay.
- The edge: `GET /reports` and `GET /reports/{taskId}` next to [ADR 0015](0015-proposals.md)'s routes; `ListTasks`
  is forwarded as before, now with a tenant that filters.
- The task service: `golemAgent` and `golemOutcome` in task metadata, `AgentTaskStore`, the
  two report queries, and the `report` field on a proposal whose task has one.
- [ADR 0017](0017-triggers.md)'s open question on reports without a proposal is answered here: the agent's
  reviewers read them.
- [ADR 0011](0011-web-ui.md) keeps its decisions on sign-in, sessions, CSRF tokens and headers. Its **Pages**
  paragraph and the logout refresh page are superseded by this ADR.

## Consequences

- One page shows a person their work on one agent and every decision waiting for them, including
  channel tasks they could not see before, and it updates within about 10 s without a reload.
- The browser still holds no token. A script injected into the board could act within the
  session through the API, as one injected into a page could through its forms. The CSP gives it
  no inline or foreign source to come from, and payloads are never rendered as HTML.
- The UI is now two images in two languages. The board brings a Node toolchain and an npm
  dependency tree into the build, and its dependencies need the same update and vulnerability
  watch as the Python ones. The Node toolchain stays in the build stage; the runtime image is
  nginx and static files.
- The task service overrides one method of a2a-sdk's store. The parity test guards it, but every
  SDK upgrade that touches `list` means redoing the override.
- Polling costs about 14 calls a minute for each visible board. The load on the edge and the
  task service grows with the number of people looking, not with the number of tasks; a hidden
  tab costs nothing.
- The board shows the last 100 updated tasks per agent at first. Older ones are in the archive's
  pages.
- The "Waiting for me" column stays empty for Golem's own agents until a run can ask for input.
- An agent's reviewers read every report its goal runs leave, including those about alerts
  from environments they do not otherwise watch. Whoever controls the catalog's `reviewers`
  controls that too, the same trust as deciding its proposals. Reports carry whatever the
  model saw: logs and service answers that are clean at the source
  ([ADR 0016](0016-observability-tools.md)), so they are no more sensitive than the proposals the same people already decide.
- Reports are not pushed anywhere and not counted in the left column: a quiet investigation is
  there to be read when someone looks, not to demand attention.
- The UI's documentation must be redone, not patched:
  - `docs/guide/getting-started.md` and every screenshot in `docs/images/ui/` (sign-in, agents,
    new task, tasks, the five task states, the rate-limited page, the narrow layout), taken again
    by `scripts/ui_screenshots.py` and `scripts/ui_demo.py`, which move to the board;
  - `docs/guide/reviewing-proposals.md`, which describes review in GitLab only;
  - `docs/guide/channels.md`, where it points to the UI;
  - `docs/operations/install.md` (the ingress's path routing and the new Service),
    `docs/operations/configuration.md` (`GOLEM_UI_AGENTS` removed, the board's image),
    `docs/operations/security.md` (the headers of two containers, the CSRF header),
    `deploy/k8s/README.md` (the routing table, policy `board`), `docs/architecture.md` and the
    containers table in `README.md`;
  - `docs/operations/upgrade.md`, for the backfill and the ingress change, which must be
    deployed together with the new `ui` image, since the old pages are gone.
