import base64
import hashlib
import hmac
import json
from dataclasses import replace
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa

from golem import call_token, run_token
from golem.call_token import CallClaims
from golem.edge.auth import (
    AuthFailure,
    Principal,
    authenticate,
    authenticate_any,
    authenticate_call,
)
from golem.run_token import RunClaims, SigningKey, public_jwks

ISSUER = "https://idp.example.test/realms/golem"
AUDIENCE = "golem-edge"
NOW = 1_800_000_000
KID = "key-1"

RSA_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER_RSA_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
EC_KEY = ec.generate_private_key(ec.SECP256R1())


def jwks(*entries: tuple[Any, str, str]) -> jwt.PyJWKSet:
    keys = []
    for private_key, kid, alg in entries:
        public = json.loads(jwt.get_algorithm_by_name(alg).to_jwk(private_key.public_key()))
        keys.append({**public, "kid": kid, "alg": alg, "use": "sig"})
    return jwt.PyJWKSet.from_dict({"keys": keys})


KEYS = jwks((RSA_KEY, KID, "RS256"), (EC_KEY, "ec-1", "ES256"))


def claims(**overrides: Any) -> dict[str, Any]:
    return {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "iat": NOW - 10,
        "nbf": NOW - 10,
        "exp": NOW + 300,
        "sub": "0b5c6f1e",
        "azp": "golem-ui",
        "preferred_username": "alice",
    } | overrides


def sign(payload: dict[str, Any], key: Any = RSA_KEY, alg: str = "RS256", kid: str = KID) -> str:
    return jwt.encode(payload, key, algorithm=alg, headers={"kid": kid})


def check(token: str) -> Principal | AuthFailure:
    return authenticate(token, keys=KEYS, issuer=ISSUER, audience=AUDIENCE, now=NOW)


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def hs256_with_public_key_as_secret(payload: dict[str, Any]) -> str:
    secret = RSA_KEY.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    header = b64(json.dumps({"alg": "HS256", "typ": "JWT", "kid": KID}).encode())
    body = b64(json.dumps(payload).encode())
    mac = hmac.new(secret, f"{header}.{body}".encode(), hashlib.sha256).digest()
    return f"{header}.{body}.{b64(mac)}"


def test_human_user_becomes_user_principal() -> None:
    assert check(sign(claims())) == Principal(name="user:alice", chain=())


def test_service_account_becomes_service_principal_named_by_client() -> None:
    token = sign(claims(preferred_username="service-account-jira-adapter", azp="jira-adapter"))

    assert check(token) == Principal(name="service:jira-adapter", chain=())


def test_es256_token_is_accepted() -> None:
    assert check(sign(claims(), key=EC_KEY, alg="ES256", kid="ec-1")) == Principal(
        name="user:alice", chain=()
    )


def test_audience_list_containing_the_edge_is_accepted() -> None:
    assert check(sign(claims(aud=["account", AUDIENCE]))) == Principal("user:alice", ())


@pytest.mark.parametrize(
    ("token", "reason"),
    [
        (sign(claims(exp=NOW - 1)), "expired"),
        (sign(claims(exp=NOW)), "expired"),
        (sign(claims(nbf=NOW + 60)), "not yet valid"),
        (sign(claims(iss="https://idp.example.test/realms/other")), "issuer"),
        (sign(claims(aud="another-service")), "audience"),
        (sign(claims(), key=OTHER_RSA_KEY), "signature"),
        (sign(claims(), kid="unknown"), "unknown signing key"),
        (jwt.encode(claims(), RSA_KEY, algorithm="RS256"), "no key id"),
        (jwt.encode(claims(), None, algorithm="none"), "algorithm"),
        (hs256_with_public_key_as_secret(claims()), "algorithm"),
        (sign(claims(), alg="RS512"), "algorithm"),
        (sign({k: v for k, v in claims().items() if k != "exp"}), "exp"),
        (sign({k: v for k, v in claims().items() if k != "preferred_username"}), "username"),
        (sign(claims(preferred_username="service-account-x", azp="")), "client"),
        ("not-a-token", "malformed"),
        ("", "malformed"),
    ],
    ids=[
        "expired",
        "expires-now",
        "not-yet-valid",
        "wrong-issuer",
        "wrong-audience",
        "wrong-key",
        "unknown-kid",
        "missing-kid",
        "alg-none",
        "hs256-algorithm-confusion",
        "disallowed-rsa-variant",
        "missing-exp",
        "missing-username",
        "service-account-without-client",
        "garbage",
        "empty",
    ],
)
def test_invalid_tokens_are_refused_with_a_reason(token: str, reason: str) -> None:
    result = check(token)

    assert isinstance(result, AuthFailure)
    assert reason in result.reason


def test_ec_key_cannot_verify_a_token_claiming_rs256() -> None:
    result = check(sign(claims(), key=RSA_KEY, alg="RS256", kid="ec-1"))

    assert isinstance(result, AuthFailure)


# Golem call tokens (ADR 0014): the edge's second issuer


GOLEM_KEY = SigningKey.generate(kid="golem-1")
GOLEM_KEYS = jwt.PyJWKSet.from_dict(public_jwks([GOLEM_KEY]))
CALL = CallClaims(
    subject="user:alice",
    agent="reviewer",
    chain=("discovery", "reviewer"),
    root_run_id="root-run",
    run_id="child-run",
    expires_at=NOW + 600,
)


def dispatch(token: str) -> Principal | AuthFailure:
    return authenticate_any(
        token,
        idp=check,
        golem=lambda raw: authenticate_call(raw, keys=GOLEM_KEYS, now=NOW),
    )


def test_a_call_token_becomes_the_acting_agent_on_behalf_of_the_subject() -> None:
    principal = dispatch(call_token.issue(CALL, GOLEM_KEY, now=NOW))

    assert principal == Principal(
        name="agent:reviewer",
        chain=("discovery", "reviewer"),
        subject="user:alice",
        root_run_id="root-run",
        run_id="child-run",
    )
    assert isinstance(principal, Principal)
    assert principal.on_behalf_of == "user:alice"


def test_an_identity_provider_token_still_acts_for_itself() -> None:
    principal = dispatch(sign(claims()))

    assert principal == Principal(name="user:alice", chain=())
    assert isinstance(principal, Principal)
    assert principal.on_behalf_of == "user:alice"


def test_a_run_token_is_refused_by_the_edge() -> None:
    claims_ = RunClaims(
        run_id="run-1",
        agent="discovery",
        caller="user:alice",
        root_run_id="run-1",
        tools=("tracker.read",),
        expires_at=NOW + 600,
    )

    result = dispatch(run_token.issue(claims_, GOLEM_KEY, now=NOW))

    assert isinstance(result, AuthFailure)
    assert "audience" in result.reason.lower()


def test_a_token_claiming_golem_is_never_checked_against_the_identity_provider() -> None:
    # Signed by the IdP's key but naming Golem as issuer: Golem's keys do not verify it.
    forged = sign({"iss": "golem", "aud": "golem-a2a", "sub": "user:alice", "exp": NOW + 60})

    assert isinstance(dispatch(forged), AuthFailure)


def test_a_golem_token_signed_with_rs256_is_refused() -> None:
    body = {
        "iss": "golem",
        "aud": "golem-a2a",
        "sub": "user:alice",
        "act": {"sub": "agent:reviewer"},
        "chain": ["reviewer"],
        "root": "r",
        "run": "r",
        "exp": NOW + 60,
    }

    result = dispatch(sign(body, kid="golem-1"))

    assert isinstance(result, AuthFailure)
    assert "ES256" in result.reason


def test_an_expired_call_token_is_refused() -> None:
    token = call_token.issue(replace(CALL, expires_at=NOW), GOLEM_KEY, now=NOW - 1000)

    assert dispatch(token) == AuthFailure("token expired")


def test_a_malformed_token_goes_to_the_identity_provider_check() -> None:
    assert dispatch("not-a-jwt") == AuthFailure("malformed token")
