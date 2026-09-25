"""Call tokens: what a Job presents to the edge to delegate to another agent (ADR 0014)."""

import jwt
import pytest

from golem import call_token, run_token
from golem.call_token import CallClaims
from golem.run_token import RunClaims, RunTokenError, SigningKey, public_jwks

NOW = 1_800_000_000
CLAIMS = CallClaims(
    subject="user:alice",
    agent="discovery",
    chain=("discovery",),
    root_run_id="11111111-1111-1111-1111-111111111111",
    run_id="11111111-1111-1111-1111-111111111111",
    expires_at=NOW + 3600,
)
RUN_CLAIMS = RunClaims(
    run_id="run-1",
    agent="discovery",
    caller="user:alice",
    root_run_id="run-1",
    tools=("tracker.read",),
    expires_at=NOW + 3600,
)


@pytest.fixture(scope="module")
def key() -> SigningKey:
    return SigningKey.generate(kid="k1")


def keys_of(key: SigningKey) -> jwt.PyJWKSet:
    return jwt.PyJWKSet.from_dict(public_jwks([key]))


def test_an_issued_call_token_verifies_back_to_its_claims(key: SigningKey) -> None:
    token = call_token.issue(CLAIMS, key, now=NOW)

    assert call_token.verify(token, keys_of(key), now=NOW + 10) == CLAIMS


def test_the_claims_follow_the_token_exchange_shape(key: SigningKey) -> None:
    payload = jwt.decode(
        call_token.issue(CLAIMS, key, now=NOW),
        options={"verify_signature": False},
    )

    assert payload == {
        "iss": "golem",
        "aud": "golem-a2a",
        "sub": "user:alice",
        # RFC 8693, 4.1: a JSON object whose members identify the current actor.
        "act": {"sub": "agent:discovery"},
        "chain": ["discovery"],
        "root": CLAIMS.root_run_id,
        "run": CLAIMS.run_id,
        "iat": NOW,
        "exp": NOW + 3600,
    }


def test_a_run_token_is_not_a_call_token(key: SigningKey) -> None:
    token = run_token.issue(RUN_CLAIMS, key, now=NOW)

    result = call_token.verify(token, keys_of(key), now=NOW)

    assert isinstance(result, RunTokenError)
    assert "audience" in result.reason.lower()


def test_a_call_token_is_not_a_run_token(key: SigningKey) -> None:
    token = call_token.issue(CLAIMS, key, now=NOW)

    result = run_token.verify(token, keys_of(key), now=NOW)

    assert isinstance(result, RunTokenError)
    assert "audience" in result.reason.lower()


def test_an_expired_call_token_is_refused(key: SigningKey) -> None:
    token = call_token.issue(CLAIMS, key, now=NOW)

    assert call_token.verify(token, keys_of(key), now=CLAIMS.expires_at) == RunTokenError(
        "token expired"
    )


def test_a_call_token_signed_by_another_key_is_refused(key: SigningKey) -> None:
    other = SigningKey.generate(kid="k1")

    result = call_token.verify(call_token.issue(CLAIMS, other, now=NOW), keys_of(key), now=NOW)

    assert isinstance(result, RunTokenError)


def forged(key: SigningKey, **changes: object) -> str:
    payload = {
        "iss": "golem",
        "aud": "golem-a2a",
        "sub": "user:alice",
        "act": {"sub": "agent:discovery"},
        "chain": ["discovery"],
        "root": "r",
        "run": "r",
        "exp": NOW + 60,
    }
    payload.update(changes)
    return jwt.encode(
        {k: v for k, v in payload.items() if v is not None},
        key.private_pem,
        algorithm="ES256",
        headers={"kid": key.kid},
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"act": None},
        {"act": "agent:discovery"},
        {"act": {"sub": "discovery"}},
        {"act": {"sub": "user:alice"}},
        {"chain": []},
        {"chain": "discovery"},
        {"chain": ["reviewer"]},
        {"chain": ["discovery", 1]},
        {"root": None},
        {"run": ""},
        {"sub": "agent:discovery"},
    ],
    ids=repr,
)
def test_a_call_token_with_malformed_claims_is_refused(
    key: SigningKey, changes: dict[str, object]
) -> None:
    result = call_token.verify(forged(key, **changes), keys_of(key), now=NOW)

    assert isinstance(result, RunTokenError)


def test_the_acting_agent_ends_a_longer_chain(key: SigningKey) -> None:
    claims = CallClaims(
        subject="service:ci",
        agent="reviewer",
        chain=("discovery", "reviewer"),
        root_run_id="root",
        run_id="child",
        expires_at=NOW + 60,
    )

    assert call_token.verify(call_token.issue(claims, key, now=NOW), keys_of(key), now=NOW) == (
        claims
    )
