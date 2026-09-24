"""The edge's signing keys: fetched from the IdP's JWKS URL, refreshed on an unknown key id."""

import json
import time
from functools import partial
from typing import Any

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from test_edge_auth import AUDIENCE, ISSUER, claims

from golem.edge.__main__ import authenticator
from golem.edge.auth import AuthFailure, Principal
from golem.jwks import SigningKeys, fetch_jwks

JWKS_URL = "https://idp.example.test/realms/golem/protocol/openid-connect/certs"
OLD_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
NEW_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def jwk(private_key: Any, kid: str) -> dict[str, Any]:
    public = json.loads(jwt.get_algorithm_by_name("RS256").to_jwk(private_key.public_key()))
    return {**public, "kid": kid, "alg": "RS256", "use": "sig"}


class IdP:
    def __init__(self) -> None:
        self.keys = [jwk(OLD_KEY, "old")]
        self.fetches = 0
        self.down = False

    def handle(self, request: httpx.Request) -> httpx.Response:
        assert str(request.url) == JWKS_URL
        self.fetches += 1
        if self.down:
            return httpx.Response(503)
        return httpx.Response(200, json={"keys": self.keys})


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def idp() -> IdP:
    return IdP()


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def keys(idp: IdP, clock: Clock) -> SigningKeys:
    client = httpx.Client(transport=httpx.MockTransport(idp.handle))
    return SigningKeys(partial(fetch_jwks, client, JWKS_URL), clock=clock)


def token(key: Any, kid: str) -> str:
    # The edge checks expiry against the wall clock, unlike test_edge_auth's fixed NOW.
    payload = claims(iat=int(time.time()), nbf=int(time.time()), exp=int(time.time()) + 300)
    return jwt.encode(payload, key, algorithm="RS256", headers={"kid": kid})


def check(keys: SigningKeys, raw: str) -> Principal | AuthFailure:
    return authenticator(keys, issuer=ISSUER, audience=AUDIENCE)(raw)


def test_keys_fetched_at_startup_verify_a_token(keys: SigningKeys, idp: IdP) -> None:
    keys.refresh()

    assert check(keys, token(OLD_KEY, "old")) == Principal(name="user:alice", chain=())
    assert idp.fetches == 1


def test_an_unknown_key_id_refreshes_the_keys(keys: SigningKeys, idp: IdP, clock: Clock) -> None:
    keys.refresh()
    idp.keys.append(jwk(NEW_KEY, "new"))
    clock.now += 61

    assert isinstance(check(keys, token(NEW_KEY, "new")), Principal)
    assert idp.fetches == 2


def test_refresh_happens_at_most_once_a_minute(keys: SigningKeys, idp: IdP, clock: Clock) -> None:
    keys.refresh()
    clock.now += 30

    for kid in ("ghost-1", "ghost-2", "ghost-3"):
        assert check(keys, token(NEW_KEY, kid)) == AuthFailure(f"unknown signing key {kid!r}")

    assert idp.fetches == 1
    clock.now += 31
    check(keys, token(NEW_KEY, "ghost-4"))
    assert idp.fetches == 2


def test_an_idp_outage_at_startup_fails_closed_then_recovers(
    keys: SigningKeys, idp: IdP, clock: Clock
) -> None:
    idp.down = True
    keys.refresh()

    assert check(keys, token(OLD_KEY, "old")) == AuthFailure("signing keys unavailable")

    idp.down = False
    clock.now += 60
    assert isinstance(check(keys, token(OLD_KEY, "old")), Principal)


def test_a_failed_refresh_keeps_the_keys_already_known(
    keys: SigningKeys, idp: IdP, clock: Clock
) -> None:
    keys.refresh()
    idp.down = True
    clock.now += 61

    check(keys, token(NEW_KEY, "new"))

    assert isinstance(check(keys, token(OLD_KEY, "old")), Principal)


def test_a_token_without_key_id_does_not_trigger_a_fetch(keys: SigningKeys, idp: IdP) -> None:
    keys.refresh()
    raw = jwt.encode(claims(), OLD_KEY, algorithm="RS256")

    assert isinstance(check(keys, raw), AuthFailure)
    assert idp.fetches == 1
