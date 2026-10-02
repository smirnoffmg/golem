import jwt
import pytest

from golem import run_token
from golem.proposal_token import (
    ACTOR,
    APPLY,
    PREVIEW,
    TOKEN_SECONDS,
    ProposalClaims,
    issue,
    verify,
)
from golem.run_token import SigningKey, public_jwks

KEY = SigningKey.generate("k1")
KEYS = jwt.PyJWKSet.from_dict(public_jwks([KEY]))
AUDIENCE = "https://wiki-write.golem-system.svc:8000/mcp"
NOW = 1_800_000_000
CLAIMS = ProposalClaims(
    audience=AUDIENCE,
    decider="user:alice",
    proposal_id="0c6f0d4e-6c43-4a8e-9a55-0f6c7b0f4a11",
    scope=APPLY,
    group="wiki.write",
    digest="ab" * 32,
)


def test_a_token_names_the_person_the_platform_acting_and_the_payload():
    token = issue(CLAIMS, KEY, NOW)
    payload = jwt.decode(token, options={"verify_signature": False})

    assert payload["iss"] == "golem"
    assert payload["aud"] == AUDIENCE
    assert payload["sub"] == "user:alice"
    # RFC 8693 4.1: the person decided; the platform acts for them.
    assert payload["act"] == {"sub": ACTOR} == {"sub": "service:golem-tasks"}
    assert payload["proposal"] == CLAIMS.proposal_id
    assert payload["scope"] == "apply"
    assert payload["tools"] == ["wiki.write"]
    assert payload["digest"] == "ab" * 32
    assert payload["exp"] - payload["iat"] == TOKEN_SECONDS == 120
    assert payload["jti"]
    assert jwt.get_unverified_header(token)["kid"] == "k1"


def test_every_token_is_its_own():
    assert issue(CLAIMS, KEY, NOW) != issue(CLAIMS, KEY, NOW)


def test_a_write_server_verifies_its_audience_and_reads_the_claims():
    assert verify(issue(CLAIMS, KEY, NOW), KEYS, AUDIENCE, NOW + 1) == CLAIMS


def test_a_token_for_another_server_is_refused():
    token = issue(CLAIMS, KEY, NOW)

    refused = verify(token, KEYS, "https://desk-write.golem-system.svc:8000/mcp", NOW)

    assert isinstance(refused, run_token.RunTokenError)


def test_an_expired_token_is_refused():
    refused = verify(issue(CLAIMS, KEY, NOW), KEYS, AUDIENCE, NOW + TOKEN_SECONDS)

    assert isinstance(refused, run_token.RunTokenError)
    assert "expired" in refused.reason


def test_a_run_token_is_not_a_proposal_token():
    # A write server refuses a token without `proposal` and `act` (ADR 0015).
    claims = run_token.RunClaims("run", "agent", "user:alice", "root", ("wiki.write",), NOW + 60)
    payload = {
        **jwt.decode(run_token.issue(claims, KEY, NOW), options={"verify_signature": False}),
        "aud": AUDIENCE,
    }
    forged = jwt.encode(payload, KEY.private_pem, algorithm="ES256", headers={"kid": "k1"})

    refused = verify(forged, KEYS, AUDIENCE, NOW)

    assert isinstance(refused, run_token.RunTokenError)
    assert "proposal" in refused.reason


@pytest.mark.parametrize(
    "change",
    [
        {"act": {"sub": "agent:discovery"}},
        {"scope": "delete"},
        {"sub": "service:jira"},
        {"tools": ["wiki.write", "tracker.write"]},
        {"digest": "not-hex"},
    ],
)
def test_a_token_off_in_any_claim_is_refused(change):
    payload = jwt.decode(issue(CLAIMS, KEY, NOW), options={"verify_signature": False})
    forged = jwt.encode(payload | change, KEY.private_pem, algorithm="ES256", headers={"kid": "k1"})

    assert isinstance(verify(forged, KEYS, AUDIENCE, NOW), run_token.RunTokenError)


def test_a_preview_token_is_a_token_of_its_own_scope():
    claims = ProposalClaims(**{**CLAIMS.__dict__, "scope": PREVIEW})

    assert verify(issue(claims, KEY, NOW), KEYS, AUDIENCE, NOW) == claims
