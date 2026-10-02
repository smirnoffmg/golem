import json

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec

from golem.run_token import (
    RunClaims,
    RunTokenError,
    SigningKey,
    issue,
    public_jwks,
    verify,
)

NOW = 1_800_000_000
CLAIMS = RunClaims(
    run_id="run-1",
    agent="discovery",
    caller="user:alice",
    root_run_id="run-1",
    tools=("tracker.read", "wiki.read"),
    expires_at=NOW + 3600,
)


@pytest.fixture(scope="module")
def key() -> SigningKey:
    return SigningKey.generate(kid="k1")


def keys_of(key: SigningKey) -> jwt.PyJWKSet:
    return jwt.PyJWKSet.from_dict(public_jwks([key]))


def test_an_issued_token_verifies_back_to_its_claims(key: SigningKey) -> None:
    token = issue(CLAIMS, key, now=NOW)

    assert verify(token, keys_of(key), now=NOW + 10) == CLAIMS


def test_an_expired_token_is_refused(key: SigningKey) -> None:
    token = issue(CLAIMS, key, now=NOW)

    result = verify(token, keys_of(key), now=CLAIMS.expires_at)

    assert isinstance(result, RunTokenError)
    assert "expired" in result.reason


def test_a_token_signed_by_another_key_is_refused(key: SigningKey) -> None:
    other = SigningKey.generate(kid="k1")

    result = verify(issue(CLAIMS, other, now=NOW), keys_of(key), now=NOW)

    assert isinstance(result, RunTokenError)


def test_an_edge_token_is_not_a_run_token(key: SigningKey) -> None:
    # Same key, wrong audience: a token meant for the edge must not open the MCP servers.
    forged = jwt.encode(
        {"iss": "golem", "aud": "golem-edge", "exp": NOW + 60, "sub": "run-1"},
        key.private_pem,
        algorithm="ES256",
        headers={"kid": key.kid},
    )

    result = verify(forged, keys_of(key), now=NOW)

    assert isinstance(result, RunTokenError)


@pytest.mark.parametrize("alg", ["none", "HS256"])
def test_unsigned_or_symmetric_tokens_are_refused(key: SigningKey, alg: str) -> None:
    header = {"alg": alg, "kid": key.kid, "typ": "JWT"}
    body = {"iss": "golem", "aud": "golem-mcp", "exp": NOW + 60, "sub": "run-1"}

    def part(data: dict) -> str:
        import base64

        return base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()

    result = verify(f"{part(header)}.{part(body)}.", keys_of(key), now=NOW)

    assert isinstance(result, RunTokenError)


def test_the_jwks_holds_only_public_material(key: SigningKey) -> None:
    [jwk] = public_jwks([key])["keys"]

    assert jwk["kid"] == "k1"
    assert jwk["kty"] == "EC"
    assert "d" not in jwk


def test_a_key_loads_from_pem(key: SigningKey) -> None:
    loaded = SigningKey.from_pem(key.private_pem, kid="k1")

    assert verify(issue(CLAIMS, loaded, now=NOW), keys_of(key), now=NOW) == CLAIMS


def test_only_p256_keys_are_accepted() -> None:
    from cryptography.hazmat.primitives import serialization

    pem = (
        ec.generate_private_key(ec.SECP384R1())
        .private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        .decode()
    )

    with pytest.raises(ValueError, match="P-256"):
        SigningKey.from_pem(pem, kid="k1")


def test_a_token_that_carries_a_proposal_is_not_a_run_token(key: SigningKey) -> None:
    # Every read server refuses a proposal token, whatever its audience says (ADR 0015).
    payload = jwt.decode(issue(CLAIMS, key, NOW), options={"verify_signature": False})
    forged = jwt.encode(
        payload | {"proposal": "p-1"}, key.private_pem, algorithm="ES256", headers={"kid": "k1"}
    )

    refused = verify(forged, jwt.PyJWKSet.from_dict(public_jwks([key])), NOW)

    assert isinstance(refused, RunTokenError)
    assert "proposal" in refused.reason
