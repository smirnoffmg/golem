"""The A2A edge process: ``python -m golem.edge``."""

import logging
import os
import sys
import time
from collections.abc import Callable
from functools import partial
from pathlib import Path

import httpx
import jwt
import uvicorn
from a2a.types.a2a_pb2 import AgentCard
from starlette.applications import Starlette

from golem.catalog import load_catalog
from golem.edge.app import create_edge_app
from golem.edge.auth import AuthFailure, Principal, authenticate
from golem.edge.cards import build_public_card
from golem.edge.policy import ChainLimits
from golem.settings import EdgeSettings, SettingsError, edge_settings, parse_registry

JWKS_TIMEOUT_SECONDS = 2
FORWARD_TIMEOUT_SECONDS = 30
MIN_REFRESH_SECONDS = 60.0
CATALOG_FILE = "agent.yaml"

log = logging.getLogger("golem.edge")


def fetch_jwks(client: httpx.Client, url: str) -> jwt.PyJWKSet:
    response = client.get(url)
    response.raise_for_status()
    return jwt.PyJWKSet.from_dict(response.json())


class SigningKeys:
    """The IdP's signing keys, refetched when a token names an unknown key id.

    Refetching is rate limited so that tokens with made-up key ids cannot turn the edge into
    a request flood against the IdP; a failed fetch keeps the keys already known. The edge app
    authenticates synchronously, so a refetch blocks its event loop for at most the JWKS
    timeout, at most once per interval.
    """

    def __init__(
        self,
        fetch: Callable[[], jwt.PyJWKSet],
        *,
        clock: Callable[[], float] = time.monotonic,
        min_refresh_seconds: float = MIN_REFRESH_SECONDS,
    ) -> None:
        self._fetch = fetch
        self._clock = clock
        self._min_refresh_seconds = min_refresh_seconds
        self._keys: jwt.PyJWKSet | None = None
        self._fetched_at: float | None = None

    def refresh(self) -> None:
        self._fetched_at = self._clock()
        try:
            self._keys = self._fetch()
        except (httpx.HTTPError, jwt.PyJWKSetError, ValueError) as error:
            log.warning("could not fetch signing keys: %s", error)

    def for_key_id(self, kid: str | None) -> jwt.PyJWKSet | None:
        if kid is not None and not self._knows(kid) and self._may_refresh():
            self.refresh()
        return self._keys

    def _knows(self, kid: str) -> bool:
        try:
            return self._keys is not None and self._keys[kid] is not None
        except KeyError:
            return False

    def _may_refresh(self) -> bool:
        return (
            self._fetched_at is None
            or self._clock() - self._fetched_at >= self._min_refresh_seconds
        )


def key_id_of(token: str) -> str | None:
    try:
        kid = jwt.get_unverified_header(token).get("kid")
    except jwt.DecodeError:
        return None
    return kid if isinstance(kid, str) else None


def authenticator(
    keys: SigningKeys, *, issuer: str, audience: str
) -> Callable[[str], Principal | AuthFailure]:
    def check(token: str) -> Principal | AuthFailure:
        current = keys.for_key_id(key_id_of(token))
        if current is None:
            return AuthFailure("signing keys unavailable")
        return authenticate(token, keys=current, issuer=issuer, audience=audience)

    return check


def load_public_cards(
    catalogs_dir: Path, *, base_url: str, oidc_discovery_url: str
) -> dict[str, AgentCard]:
    cards = {}
    for path in sorted(catalogs_dir.glob(f"*/{CATALOG_FILE}")):
        catalog = load_catalog(path)
        cards[catalog.name] = build_public_card(
            catalog, base_url=base_url, oidc_discovery_url=oidc_discovery_url
        )
    return cards


def build_app(settings: EdgeSettings, jwks_client: httpx.Client) -> Starlette:
    keys = SigningKeys(partial(fetch_jwks, jwks_client, settings.jwks_url))
    keys.refresh()
    return create_edge_app(
        authenticate=authenticator(keys, issuer=settings.issuer, audience=settings.audience),
        registry=parse_registry(settings.registry_file.read_text()),
        limits=ChainLimits(max_depth=settings.max_chain_depth),
        audit_dsn=settings.audit_dsn,
        forward=httpx.AsyncClient(
            base_url=settings.task_service_url, timeout=FORWARD_TIMEOUT_SECONDS
        ),
        cards=load_public_cards(
            settings.catalogs_dir,
            base_url=settings.public_base_url,
            oidc_discovery_url=settings.oidc_discovery_url,
        ),
    )


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        settings = edge_settings(os.environ)
    except SettingsError as error:
        sys.exit(f"golem edge: {error}")
    with httpx.Client(timeout=JWKS_TIMEOUT_SECONDS) as jwks_client:
        uvicorn.run(build_app(settings, jwks_client), host="0.0.0.0", port=settings.port)


if __name__ == "__main__":
    main()
