# 11. The web UI: a backend-for-frontend that calls the edge as the user

## Status

Accepted, 2026-09-25

## Context

People should give an agent a goal from a browser, see their tasks and cancel one. Every
entry into Golem is an A2A call to the edge with a bearer token the edge verifies
(ADR 0002), and the caller the edge sees is the principal every later decision is made for:
the call registry, the audit row, admission quotas, the run token's `caller`, and the task
store's owner, which decides who may read, list or cancel a task.

The two channel adapters call the edge as themselves (`service:<client id>`), which ADR 0010
records as a gap: the edge cannot tell who in a chat channel asked. A browser can do better,
because the user can sign in at the identity provider and the UI can hold a token issued to
that user. What is left to decide is where that token lives, how the browser is tied to it,
and how the pages resist the attacks a browser brings with it.

The edge let a caller send, get and cancel a task, but not list its tasks, which a "my tasks"
page needs. a2a-sdk 1.1.5 implements A2A 1.0's `ListTasks` (JSON-RPC method `ListTasks`,
`ListTasksRequest` with `tenant`, `context_id`, `status`, `page_size`, `page_token`,
`history_length`, `status_timestamp_after`, `include_artifacts`): `DefaultRequestHandler.
on_list_tasks` calls `TaskStore.list(params, context)`, and both `InMemoryTaskStore` and
`DatabaseTaskStore` filter by the owner that `resolve_user_scope` takes from the call
context's user, which the task service builds from the edge's principal header. The tenant is
not a filter.

Sources:

- OWASP *Application Security Verification Standard 5.0*:
  - с. 63 (PDF 64), 10.1.1: "when using a backend‑for‑frontend pattern for browser‑based
    JavaScript applications, access and refresh tokens shall only be accessible for the
    backend"; 10.1.2: `code_verifier`, `state` and `nonce` must be "not guessable, [...]
    specific to the transaction, and [...] securely bound to both the client and the user agent
    session in which the transaction was started"; 10.2.1: protection against CSRF that
    triggers token requests "either by using [PKCE] or checking the 'state' parameter".
  - с. 66 (PDF 67), 10.4.6: PKCE is required and `plain` is not accepted.
  - с. 33 (PDF 34), 3.3.1 to 3.3.5: cookies `Secure` with the `__Host-` prefix, `SameSite` set
    according to the cookie's purpose to limit CSRF, `HttpOnly` for session tokens, set only
    through `Set-Cookie`, name and value within 4096 bytes.
  - с. 34 (PDF 35), 3.4.1: HSTS "with a maximum age of at least 1 year", for L2 applying to
    subdomains; 3.4.3: a Content-Security-Policy that "includes the directives object‑src
    'none' and base‑uri 'none'"; 3.4.4: `X‑Content‑Type‑Options: nosniff` on all responses;
    3.4.5: a referrer policy against leaking "path and query data in the URL"; 3.4.6:
    `frame-ancestors` on every response, `X-Frame-Options` being obsolete.
  - с. 35 (PDF 36), 3.4.8: `Cross-Origin-Opener-Policy: same-origin` on document responses;
    3.5.1: without CORS preflight, requests to sensitive functionality are validated "by using
    and validating anti‑forgery tokens".
  - с. 54 (PDF 55), 7.2.3: reference session tokens from a CSPRNG with at least 128 bits;
    7.2.4: "generates a new session token on user authentication [...] and terminates the
    current session token"; 7.3.2: an absolute maximum session lifetime.
  - с. 55 (PDF 56), 7.4.1: on logout or expiry "invalidating the session data at the
    application backend"; 7.4.4: logout visible on every authenticated page.
- OpenID Connect Core 1.0 (<https://openid.net/specs/openid-connect-core-1_0.html>):
  3.1.2.1 (authorization request: `scope` with `openid`, `response_type=code`, `client_id`,
  `redirect_uri`, `state`, `nonce`), 3.1.2.6 (error response), 3.1.3.1 and 3.1.3.3 (token
  request and response: `id_token`, `access_token`, `token_type` "MUST be Bearer",
  `expires_in`), 3.1.3.7 (ID token validation: issuer exact match, `aud` contains the client
  id, `azp`, signature, `exp`, `nonce`), 12.1 and 12.2 (refresh; the new ID token's `iss`,
  `sub` and `aud` are those of the first).
- OpenID Connect Discovery 1.0, sections 3 and 4.3: the provider's metadata, whose `issuer`
  must be identical to the issuer discovery was asked for.
- OpenID Connect RP-Initiated Logout 1.0
  (<https://openid.net/specs/openid-connect-rpinitiated-1_0.html>), section 2:
  `end_session_endpoint`, `id_token_hint` (RECOMMENDED), `client_id`,
  `post_logout_redirect_uri`; section 3: the provider redirects back only to a registered URI.
- RFC 6749: 2.3.1 (`client_secret_basic`, id and secret form-urlencoded first), 5.1 and 5.2
  (token response, `invalid_grant`), 6 (refresh; a new refresh token replaces the old one).
- RFC 7636 (<https://www.rfc-editor.org/rfc/rfc7636>): 4.1 (verifier of 43 to 128 unreserved
  characters), 4.2 (`S256`: `BASE64URL-ENCODE(SHA256(ASCII(code_verifier)))`), 4.3, 4.5;
  7.2 (`plain` gives no protection against an observed request).
- a2a-sdk 1.1.5, `a2a/server/routes/jsonrpc_dispatcher.py` (`'ListTasks': ListTasksRequest`),
  `a2a/server/tasks/database_task_store.py` (`list`: `where(self.task_model.owner == owner)`),
  `a2a/server/owner_resolver.py` (`resolve_user_scope`: `context.user.user_name`).

## Decision

`python -m golem.ui` is a backend-for-frontend: server-rendered pages, one opaque cookie, and
every token kept on the server.

**The user's own token reaches the edge.** The UI signs the user in with the OpenID Connect
authorization code flow as a confidential client, and calls the edge with that user's access
token. The edge verifies it like any other and sees `user:<preferred_username>`: the audit
row, the call registry (`user:*` or the named user), admission, the run token and the task's
owner all name the person, not the UI. This is the difference from ADR 0010, where the adapter
is the caller and the person a claim in metadata. The UI has no service identity of its own at
the edge and no way to act without a signed-in user. The access token must carry the edge's
audience (an audience mapper on the UI's client at the identity provider).

**Login.** `/login` creates a transaction: `state`, `nonce` and a binding value, 256 random bits
each, and a PKCE verifier (43 characters), and redirects to the authorization endpoint with
`code_challenge` = S256 of the verifier; `plain` is never sent. The transaction is stored in
`golem_ui` with the verifier encrypted and the binding hashed, and the browser gets the binding
in `__Host-golem-login` (`Secure`, `HttpOnly`, `SameSite=Lax`, `Path=/`, `Max-Age=600`).
`/callback` deletes the transaction by `state` before anything else, so a state works once,
refuses it after ten minutes or when the browser's binding cookie does not match, then
exchanges the code with the verifier (client authentication `client_secret_basic`) and
verifies the ID token: signature with the provider's keys (fetched through `golem.jwks`,
refetched on an unknown key id, rate limited), issuer, audience, `azp`, expiry and the nonce.
Anything that fails is a 400 and no session. Discovery is read on first use, and its `issuer`
must equal the configured one.

**Sessions in their own database.** `golem_ui`, owned by the login role `golem_ui`
(`CONNECTION LIMIT 10`, `statement_timeout` 5 s), reachable by no other role; ADR 0003's table
gains the row. `sessions(id, subject, name, access_token, refresh_token, id_token,
expires_at, csrf, created_at)`: `id` is the SHA-256 of the cookie value, so a copy of the table
opens no session; the three tokens are encrypted with a Fernet key (`GOLEM_UI_SESSION_KEY`) the
database never sees. The browser gets 256 random bits in `__Host-golem-session`, with exactly
`Path=/; Secure; HttpOnly; SameSite=Lax`, no `Domain` and no `Max-Age`. A login replaces the
browser's previous session. The access token is refreshed with the refresh token when it has a
minute or less left, under a row lock so concurrent requests refresh once; a refreshed ID token
must name the same subject. A failed refresh, or a refused token at the edge, deletes the
session and sends the browser to sign in. Twelve hours after login a session ends whatever the
refreshes. `/logout` deletes the row, clears the cookie and, if discovery names an
`end_session_endpoint`, redirects there with `id_token_hint`, `client_id` and
`post_logout_redirect_uri` = the UI's base URL + `/`.

**Pages.** Jinja2 with autoescape on, no inline script or style, no third-party assets; the one
stylesheet is served by the UI. Agents come from the edge's public cards for
`GOLEM_UI_AGENTS`; a missing card is shown as unavailable. A new task is `SendMessage` with the
agent as `tenant` and message id `ui:<nonce>`, the nonce rendered into each form, so a double
submit is one message id and the orchestrator starts one run for it. "My tasks" is `ListTasks`
(page size 50); a task page is `GetTask`, and another user's task is not found; cancel is
`CancelTask`. A merge request URL in a task's outcome becomes a link only if it is `http` or
`https` with a host. The agent a task was started for is kept in the message's metadata
(`golemAgent`), since a task does not keep its tenant; it is the caller's own claim, used only
to address the caller's own task at the edge.

**CSRF.** Every state-changing request is a `POST` carrying the session's CSRF token (256 bits,
compared in constant time); a missing or wrong token is a 403 before anything else happens.
`SameSite=Lax` keeps the session cookie off cross-site `POST`s as a second layer, and still
sends it on the top-level `GET` back from the identity provider.

**Headers on every response**, errors and redirects included:
`Content-Security-Policy: default-src 'self'; frame-ancestors 'none'; form-action 'self';
base-uri 'none'; object-src 'none'` (`object-src 'none'` is what 3.4.3 asks for beyond the
rest), `X-Content-Type-Options: nosniff`, `Referrer-Policy: same-origin`,
`Cross-Origin-Opener-Policy: same-origin`, `Cache-Control: no-store`, and
`Strict-Transport-Security: max-age=31536000; includeSubDomains` when the public base URL is
`https`. The public base URL must be `https`, or `http` on localhost, where browsers accept
`Secure` cookies.

**ListTasks at the edge.** The edge forwards `ListTasks` like the other three methods: it needs
a `tenant` the caller may call, is audited allow or deny, and the task service answers with the
caller's own tasks only. `tests/test_edge_app.py` shows another user's list is empty.

**Deployment.** A Deployment and Service `ui`, service account `golem-ui` without a token, a
NetworkPolicy admitting the ingress controller's namespace on 8000 and letting the UI reach the
edge (8000), the identity provider (443) and Postgres (5432); the edge's policy admits the UI.
The Secret `golem-ui` holds the client secret, the DSN and the Fernet key.

## Consequences

- Tokens never reach the browser: a script injected into a page could act within the session
  through the page's own forms, but could not take a token away, and the CSP leaves such a
  script no inline or foreign source to come from.
- The audit log and quotas tell people apart for runs started from the UI, unlike chat (ADR
  0010). Each user's runs count against that user's admission quota.
- A stolen copy of `golem_ui` without the Fernet key yields no token and no working cookie.
  Rotating the key signs everyone out: rows sealed with the old key read as no session.
- The UI depends on the identity provider to sign in and to refresh. A provider outage ends
  sessions at their next refresh, by design: a session the UI cannot refresh is not kept.
- Holding the row lock while the token endpoint answers bounds a refresh by the role's
  `statement_timeout`; the UI's identity provider timeout (4 s) stays below it.
- "My tasks" shows pages of 50, newest first, following `ListTasks`' `nextPageToken` with an
  "Older tasks" link (`/tasks?page=<token>`; a token that is not 1 to 256 base64 characters is a
  400). It lists everything the user started over A2A, not only from the UI.
- `/login` stores a transaction per anonymous `GET`; since [ADR 0012](0012-rate-limits.md) it is
  limited per client address (30/min, burst 10), and starting tasks per session (10/min, burst
  5), which needs the ingress controller in `GOLEM_TRUSTED_PROXIES`.
- The identity provider must register `GOLEM_OIDC_REDIRECT_URL` (the base URL + `/callback`)
  and the base URL + `/` as the post-logout redirect, require PKCE S256 for the client, and put
  the edge's audience into its access tokens.
- Sessions end on logout, expiry and failed refresh; there is no listing of one's own sessions
  or administrative termination yet (ASVS 7.4.5, 7.5.2).
