"""The edge's signing keys: fetched from the IdP's JWKS URL, refreshed on an unknown key id."""

import time
from functools import partial
from itertools import pairwise
from typing import Any

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from support.idp import jwk
from test_edge_auth import AUDIENCE, ISSUER, claims

from golem import call_token
from golem.call_token import CallClaims
from golem.edge.__main__ import authenticator, call_authenticator
from golem.edge.auth import AuthFailure, Principal, authenticate_any
from golem.jwks import SigningKeys, fetch_jwks
from golem.run_token import SigningKey, public_jwks

JWKS_URL = "https://idp.example.test/realms/golem/protocol/openid-connect/certs"
OLD_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
NEW_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


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


def test_a_verifier_without_keys_retries_soon_instead_of_after_the_interval(
    keys: SigningKeys, idp: IdP, clock: Clock
) -> None:
    # Started before the key publisher: it must not refuse every token for a whole interval.
    idp.down = True
    keys.refresh()
    idp.down = False
    clock.now += 1

    assert isinstance(check(keys, token(OLD_KEY, "old")), Principal)
    assert idp.fetches == 2


def test_retries_without_keys_back_off_and_stay_bounded(
    keys: SigningKeys, idp: IdP, clock: Clock
) -> None:
    idp.down = True
    keys.refresh()
    fetched_at = []
    for _ in range(400):
        clock.now += 0.5
        check(keys, token(OLD_KEY, "old"))
        if idp.fetches > len(fetched_at) + 1:
            fetched_at.append(clock.now)
    gaps = [later - earlier for earlier, later in pairwise(fetched_at)]

    assert gaps == sorted(gaps)
    assert gaps[0] <= 2
    assert max(gaps) <= 60


def test_an_unknown_key_id_stays_rate_limited_once_keys_were_loaded(
    keys: SigningKeys, idp: IdP, clock: Clock
) -> None:
    keys.refresh()
    idp.down = True
    clock.now += 61
    check(keys, token(NEW_KEY, "ghost-1"))
    clock.now += 1

    check(keys, token(NEW_KEY, "ghost-2"))

    assert idp.fetches == 2


# Golem's own keys, for call tokens (ADR 0014)


GOLEM_KEY = SigningKey.generate(kid="golem-1")
CALL = CallClaims(
    subject="user:alice",
    agent="discovery",
    chain=("discovery",),
    root_run_id="root",
    run_id="root",
    expires_at=int(time.time()) + 300,
)


class TaskService:
    """The task service's run-keys route, as seen by the edge: down until it has started."""

    def __init__(self) -> None:
        self.down = True

    def handle(self, request: httpx.Request) -> httpx.Response:
        if self.down:
            raise httpx.ConnectError("connection refused")
        return httpx.Response(200, json=public_jwks([GOLEM_KEY]))


def test_an_edge_started_before_the_task_service_accepts_call_tokens_within_seconds(
    idp: IdP, clock: Clock
) -> None:
    tasks = TaskService()
    client = httpx.Client(transport=httpx.MockTransport(tasks.handle))
    golem_keys = SigningKeys(
        partial(fetch_jwks, client, "http://tasks:8001/internal/run-keys"), clock=clock
    )
    golem_keys.refresh()
    idp_keys = SigningKeys(
        partial(fetch_jwks, httpx.Client(transport=httpx.MockTransport(idp.handle)), JWKS_URL),
        clock=clock,
    )
    edge = partial(
        authenticate_any,
        idp=authenticator(idp_keys, issuer=ISSUER, audience=AUDIENCE),
        golem=call_authenticator(golem_keys),
    )
    token_ = call_token.issue(CALL, GOLEM_KEY, now=int(time.time()))

    assert edge(token_) == AuthFailure("Golem signing keys unavailable")
    tasks.down = False
    clock.now += 1

    assert isinstance(edge(token_), Principal)
    assert idp.fetches == 0
