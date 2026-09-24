import base64
import hashlib
import hmac
import json
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa

from golem.edge.auth import AuthFailure, Principal, authenticate

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
