"""Agent Card signatures (A2A 1.0, section 8.4), made and checked with the a2a-sdk's helpers.

The signed payload is the card without its ``signatures`` field and without default or empty
values, canonicalized with RFC 8785 (JCS); the signature is a JWS (RFC 7515) with a detached
payload: only its protected header and signature are kept in ``AgentCardSignature``. The card
key is Golem's own ES256 key, separate from the run-token key so the two rotate on their own.
"""

import copy
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

import jwt
from a2a.client.card_resolver import parse_agent_card
from a2a.types import AgentCard
from a2a.utils.signing import (
    ProtectedHeader,
    SignatureVerificationError,
    create_agent_card_signer,
    create_signature_verifier,
)
from google.protobuf.json_format import ParseError

from golem.run_token import SigningKey, public_jwks

ALGORITHM = "ES256"
# A2A 1.0, 8.4.2: "typ: SHOULD be set to "JOSE" for JWS".
TYPE = "JOSE"
KEYS_PATH = "/.well-known/golem-card-keys.json"


@dataclass(frozen=True)
class CardVerified:
    kid: str


@dataclass(frozen=True)
class CardRejected:
    reason: str


def sign_card(card: AgentCard, key: SigningKey, *, jku: str) -> AgentCard:
    """A signed copy of ``card``: any signature it had is replaced, the card itself untouched."""
    unsigned = AgentCard()
    unsigned.CopyFrom(card)
    del unsigned.signatures[:]
    header: ProtectedHeader = {"alg": ALGORITHM, "typ": TYPE, "kid": key.kid, "jku": jku}
    return create_agent_card_signer(key.private_pem, header)(unsigned)


def card_keys(keys: Iterable[SigningKey]) -> dict[str, Any]:
    """The public JWKS that verifies cards signed with ``keys``."""
    return public_jwks(keys)


def verify_card(card: Mapping[str, Any], jwks: Mapping[str, Any]) -> CardVerified | CardRejected:
    """Whether ``card``, as JSON, carries at least one valid signature by a key in ``jwks``.

    Keys come only from ``jwks``, never from the ``jku`` a signature names: a card could point
    at any key set (A2A 1.0, 8.4.3 allows "a trusted key store"). Only ES256 is accepted, so a
    header naming ``none`` or an HMAC algorithm never reaches a key.
    """
    try:
        keys = jwt.PyJWKSet.from_dict(dict(jwks))
    except (jwt.PyJWKSetError, jwt.PyJWKError, TypeError, AttributeError) as error:
        return CardRejected(f"malformed key set: {error}")
    try:
        # Parsed as the SDK's client does; fields A2A 1.0 does not define are dropped, so they
        # are neither signed nor trusted.
        parsed = parse_agent_card(copy.deepcopy(dict(card)))
    except (ParseError, TypeError, AttributeError, ValueError) as error:
        return CardRejected(f"malformed card: {error}")
    if not parsed.signatures:
        return CardRejected("the card has no signature")
    used: list[str] = []

    def key_for(kid: str | None, _jku: str | None) -> jwt.PyJWK:
        try:
            key = keys[kid or ""]
        except KeyError:
            raise jwt.PyJWKError(f"unknown card key {kid!r}") from None
        used.append(key.key_id or "")
        return key

    try:
        create_signature_verifier(key_for, [ALGORITHM])(parsed)
    # A protected header that is not base64url JSON escapes the SDK as a ValueError.
    except (SignatureVerificationError, ValueError, TypeError, AttributeError) as error:
        return CardRejected(f"no valid signature: {error}")
    return CardVerified(kid=used[-1])
