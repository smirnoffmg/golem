# Discovering and calling Golem's agents

For agents and services of other platforms that want to use Golem's agents over A2A 1.0: how
to list them, fetch a card and check that Golem issued it, and call one. The examples use
`https://golem.internal` for the edge; ask the platform team for the real address and for a
client in Golem's identity provider.

## List the agents

`GET /agents` on the edge. Without a token it lists every published agent:

```sh
curl -s https://golem.internal/agents
```

```json
{"agents":[{"name":"discovery","description":"Turns a product problem space into evidenced hypotheses and reviewed solutions.","card_url":"https://golem.internal/agents/discovery/.well-known/agent-card.json","skills":["research","design","review"]}]}
```

With a token, the same request lists only the agents your caller may call: the ones the call
registry names you for (`user:<name>`, `service:<client id>`, or, for Golem's own agents while
they delegate, `agent:<name>`, which never sees itself or an agent already in its chain). An
empty list means you may call nothing yet; ask the platform team for an entry.

```sh
curl -s https://golem.internal/agents -H "Authorization: Bearer $TOKEN"
```

A token that does not verify gets `401` with `WWW-Authenticate: Bearer error="invalid_token"`,
never the public list. The anonymous list is cacheable (`Cache-Control: public, max-age=60`);
yours is private (`private, max-age=60`, `Vary: Authorization`), so a shared cache never hands
it to someone else. Too many requests get `429` with `Retry-After` in seconds: anonymous ones
per client address, authenticated ones per caller, counted together with your calls to `/a2a`.

## Verify a card

Each entry's `card_url` is the agent's A2A 1.0 card, served without authentication and signed
by Golem's card key as A2A 1.0 specifies (section 8.4: a JWS whose payload is the card without
`signatures` and without default values, canonicalized with RFC 8785). The public key is a JWKS
at `GET /.well-known/golem-card-keys.json`:

```json
{"keys": [{"kty": "EC", "crv": "P-256", "x": "...", "y": "...", "kid": "golem-cards-1", "alg": "ES256", "use": "sig"}]}
```

With the Python a2a-sdk, the card resolver checks the signature while it fetches the card:

```python
import asyncio

import httpx
import jwt
from a2a.client.card_resolver import A2ACardResolver
from a2a.utils.signing import create_signature_verifier

GOLEM = "https://golem.internal"
KEYS_URL = f"{GOLEM}/.well-known/golem-card-keys.json"


async def verified_card(agent: str):
    async with httpx.AsyncClient() as http:
        keys = jwt.PyJWKSet.from_dict((await http.get(KEYS_URL)).json())

        def key_for(kid: str | None, _jku: str | None) -> jwt.PyJWK:
            # Only Golem's key set, never the jku a card names: any card can name any keys.
            try:
                return keys[kid or ""]
            except KeyError:
                raise jwt.PyJWKError(f"unknown card key {kid!r}") from None

        resolver = A2ACardResolver(http, base_url=f"{GOLEM}/agents/{agent}")
        return await resolver.get_agent_card(
            signature_verifier=create_signature_verifier(key_for, ["ES256"])
        )


card = asyncio.run(verified_card("discovery"))
print(card.name, card.supported_interfaces[0].url, card.supported_interfaces[0].tenant)
```

```
discovery https://golem.internal/a2a discovery
```

A card that was changed, or signed by another key, raises
`a2a.utils.signing.InvalidSignaturesError`. Other languages: follow A2A 1.0, section 8.4.3,
and accept only `ES256` and the keys of Golem's key set.

Good to know:

- **Keys rotate without overlap.** A card whose `kid` is not in your cached key set is signed
  by a new key: fetch the key set once more before refusing the card. Both are cacheable for
  five minutes (`Cache-Control: public, max-age=300`, with an `ETag` for `If-None-Match`).
- **Pin the key set if the card reaches you through someone else** (a registry, a copy in your
  configuration). Fetched from the edge over HTTPS together with its key set, a card proves no
  more than the TLS connection did.
- **A signature carries no date.** An old copy verifies until the key is rotated; fetch cards
  from the edge.
- **What is not signed:** fields A2A 1.0 does not define (the SDK drops them while parsing) and
  empty values, such as the card's security requirement `{"oidc": {}}`; its security scheme,
  with the identity provider's URL, is signed. More in
  [security.md](../operations/security.md#rotate-the-card-signing-key).

## Call an agent

Take the interface from the verified card: the URL (the edge's `/a2a`), the binding
(`JSONRPC`) and the `tenant`, which you set in every request. The card names Golem's identity
provider under `securitySchemes`; get an access token for the audience `golem-edge` from it,
for a service with client credentials:

```sh
TOKEN=$(curl -s https://idp.internal/realms/golem/protocol/openid-connect/token \
  -d grant_type=client_credentials -d client_id="$CLIENT_ID" \
  -d client_secret="$CLIENT_SECRET" | jq -r .access_token)
```

Then start a task:

```sh
curl -s https://golem.internal/a2a \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' -H 'A2A-Version: 1.0' \
  -d '{"jsonrpc": "2.0", "id": 1, "method": "SendMessage", "params": {"tenant": "discovery",
       "message": {"messageId": "partner-1", "role": "ROLE_USER",
                   "parts": [{"text": "Research the checkout drop-off hypothesis"}]}}}'
```

The answer is a task in `TASK_STATE_WORKING`. Golem's runs take minutes to hours and end with
a merge request, so poll with `GetTask` (same `tenant`, the task's `id`); push notifications
go only to addresses the platform team allows, which today are Golem's own adapters. The whole
exchange, with its outputs, is in
[install.md, the first run](../operations/install.md#10-first-run). The edge supports
`SendMessage`, `GetTask`, `ListTasks` and `CancelTask`, no streaming. A refusal by the call
registry is a JSON-RPC error `-32041` naming the reason (`not_allowed`, `unknown_agent`, ...).

## How this page was checked

The outputs of `GET /agents`, the key set and the verification script are from an edge started
locally with the example catalog and the settings of
[configuration.md](../operations/configuration.md), its address replaced by
`https://golem.internal`; the key's coordinates are elided. The calls with a token were not
run against a real identity provider; the edge's tests cover the per-caller list
(`tests/test_edge_app.py`) and the signatures (`tests/test_card_signing.py`).
