# 12. Rate limits: token buckets per replica, at every public entry point

## Status

Accepted, 2026-09-25

## Context

ADR 0004 lists a "rate limit per caller at the edge" as the first layer of protection from
others' overload; it was never built. Meanwhile every public entry point gained a way for an
anonymous or single client to make Golem do work or write rows:

- the edge verifies every bearer token it is sent, which may refetch the identity provider's
  keys, and audits every authenticated call into the insert-only audit log;
- a platform MCP server audits every request, including those that fail authentication
  (ADR 0008), so a flood of forged tokens grows the audit log at the flood's rate;
- the UI's `/login` writes a transaction row per anonymous `GET` (ADR 0011), and `POST /tasks`
  starts a run;
- the Jira webhook and the Mattermost command compute an HMAC or compare a token for any
  request that reaches them.

The orchestrator's admission quotas (ADR 0004) already bound the runs a caller has at once, in
Postgres, shared by every replica. What is missing is a bound on request rates and on rows
written before admission is ever asked.

Sources:

- *OWASP Application Security Verification Standard 5.0*, с. 31 (PDF 32), 2.4.1: "Verify that
  anti‑automation controls are in place to protect against excessive calls to application
  functions that could lead to data exfiltration, garbage‑data creation, quota exhaustion,
  rate‑limit breaches, denial‑of‑service, or overuse of costly resources."
- *Designing Web APIs* (Jin, Sahni, Shevat), с. 104 (PDF 118): "APIs requiring user
  authentication generally apply rate-limiting on a per-user basis, whereas APIs requiring an
  application authentication typically rate-limit on a per-app basis. For unauthenticated API
  calls, API providers often choose to rate-limit by IP address." And: "If you choose to
  support occasional traffic bursts, you probably want to use the token bucket algorithm".
- The same, с. 106 (PDF 120): the token bucket in a key-value store: "On subsequent requests,
  refill the bucket with new tokens per the defined fixed rate and time elapsed since the last
  request", then remove one token.
- The same, с. 110 (PDF 124): "deny the request by returning an HTTP 429 status code [...]
  It's also standard to set the retry-after header to let developers programmatically retry
  the request."
- RFC 6585, section 4: "The 429 status code indicates that the user has sent too many requests
  in a given amount of time ("rate limiting")"; the response "MAY include a Retry-After header
  indicating how long to wait before making a new request".

## Decision

**One limiter, `golem.ratelimit`.** A token bucket per key: a rate per minute and a burst
(the bucket's size). The refill and the take are pure functions of the bucket, the rate and
the time; the `Limiter` holding the buckets takes its clock as a parameter, so tests move
time instead of sleeping. It keeps at most 10 000 keys and forgets the least recently used
beyond that, so a flood of distinct keys costs bounded memory; a forgotten key starts with a
full bucket, which gives a flood of distinct addresses nothing it would not get from new
addresses anyway. A lock keeps each take whole even though verification runs in worker
threads. A refused request gets 429 with `Retry-After` in whole seconds, the time until a
token is back.

**Where limits sit, and the defaults** (each `<NAME>_PER_MINUTE` and `<NAME>_BURST`):

| Process | Key | What is limited | Setting | Default |
| --- | --- | --- | --- | --- |
| edge | authenticated principal | every call to `/a2a`, before the call registry | `GOLEM_RATE_CALLER` | 60/min, burst 20 |
| edge | client address | failed authentications; an address out of tokens is refused before its token is verified | `GOLEM_RATE_AUTH_FAILURES` | 30/min, burst 10 |
| MCP server | client address | failed authentications (401), the same way, before the audit write | `GOLEM_RATE_AUTH_FAILURES` | 30/min, burst 10 |
| UI | client address | `GET /login` | `GOLEM_RATE_LOGIN` | 30/min, burst 10 |
| UI | session | `POST /tasks` | `GOLEM_RATE_START` | 10/min, burst 5 |
| Jira adapter | client address | `POST /jira/webhook`, before the signature check | `GOLEM_RATE_WEBHOOK` | 300/min, burst 100 |
| Mattermost adapter | client address | `POST /mattermost/command`, before the token check | `GOLEM_RATE_COMMAND` | 120/min, burst 60 |

Authenticated callers are limited per principal, anonymous ones per address, as the source
advises. The edge and the MCP servers count only *failed* authentications per address: the UI
and each adapter reach the edge from their own pod address on behalf of many people, and a
per-address limit on every call would make them one client. A successful call costs nothing
there; the per-principal limit covers it. An address that has used up its failures is refused
even with a good token until a token is back, which is the point: it is refused before the
verification it would cost. The adapters' defaults are high because every Jira webhook comes
from Jira's few addresses and every command from the Mattermost server's; they bound a flood,
not a person. The push routes (`/a2a/push`) are not limited: only the task service reaches them
(network policy), and a push without a valid HMAC token is refused at once.

**Audit once per refusal streak.** A rate-limited call at the edge is audited as a deny with
reason `rate_limited`, and at an MCP server a request refused for its address's failures is
audited the same way, but only the first refusal after the key was last admitted. Auditing
every refusal would make the log grow at the rate of the flood, the garbage-data creation
2.4.1 names, while the one row records the decision that matters: this principal or address
hit its limit, then. The next row comes after the key has been admitted again, so refusal rows
are never more than admitted requests, which are themselves limited and audited. The Retry-
After a caller sees and the 429 do not depend on whether a row was written.

**Per replica.** Buckets live in each replica's memory. With N replicas a client can get up to
N times a limit (the edge and the UI run two, so twice the table above), and a restart forgets
the buckets. That is accepted: the stack has no shared store fit for a counter on every
request (Postgres is the only one, and a write per request is the load being limited), the
limits protect each replica and the audit log from floods rather than enforce a contract, and
the limit that must hold across replicas, how many runs a caller has, is the orchestrator's
admission quota in Postgres. Sizing a limit means dividing the intended total by the replica
count.

**The client address.** The peer address of the connection, unless the peer is in
`GOLEM_TRUSTED_PROXIES` (comma-separated CIDRs, none by default). Then `X-Forwarded-For` is
read from the right, where each proxy appended the address it saw, and the first address not
in a trusted network is the client; anything to its left is the client's own writing and is
ignored. A malformed entry stops the walk at the nearest proxy. A client's own
`X-Forwarded-For` sent straight to a process is therefore never believed. IPv4-mapped IPv6
peers count as their IPv4 address. Uvicorn's own proxy header handling is off
(`proxy_headers=False`), so there is one place that decides. The address also goes into the
audit row's `source_ip` at the edge and the MCP servers, instead of the proxy's. The trusted
networks must hold the ingress controller's pods and nothing else: the Jobs, which are
untrusted (ADR 0004), call the edge and the MCP servers directly, and a Job whose address fell
in a trusted network could pick its own key with the header.

## Consequences

- ADR 0004's per-caller rate limit exists, at the edge and at every other entry point;
  admission quotas remain the hard limit on runs.
- An anonymous flood no longer grows the MCP audit log or the UI's `logins` table without
  bound: rows are bounded by the rate, per address and replica.
- Many clients behind one address (a NAT, a proxy not listed as trusted) share one bucket.
  Listing the ingress controller in `GOLEM_TRUSTED_PROXIES` is part of deploying; the base
  manifests carry a placeholder for it.
- Per-address keys are whole addresses: an IPv6 client holding a /64 has many keys. Keying IPv6
  by prefix is not built.
- A limit set too low looks like an outage to legitimate callers; 429s are visible to them with
  `Retry-After`, but there is no metric of refusals yet.
