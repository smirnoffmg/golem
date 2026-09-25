"""The A2A edge process: ``python -m golem.edge``."""

import asyncio
import logging
import os
import sys
from collections.abc import Callable
from functools import partial
from pathlib import Path

import httpx
from a2a.types.a2a_pb2 import AgentCard
from starlette.types import ASGIApp

from golem.catalog import load_catalog
from golem.edge.app import create_edge_app
from golem.edge.auth import AuthFailure, Principal, authenticate
from golem.edge.cards import build_public_card
from golem.edge.policy import ChainLimits
from golem.jwks import SigningKeys, fetch_jwks, key_id_of
from golem.metrics import Metrics, process_registry
from golem.ratelimit import Limiter
from golem.serving import serve_all, with_metrics
from golem.settings import EdgeSettings, SettingsError, edge_settings, parse_registry

JWKS_TIMEOUT_SECONDS = 2
FORWARD_TIMEOUT_SECONDS = 30
CATALOG_FILE = "agent.yaml"


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


def build_app(
    settings: EdgeSettings, jwks_client: httpx.Client, metrics: Metrics | None = None
) -> ASGIApp:
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
        edge_token=settings.edge_token,
        cards=load_public_cards(
            settings.catalogs_dir,
            base_url=settings.public_base_url,
            oidc_discovery_url=settings.oidc_discovery_url,
        ),
        callers=Limiter(settings.caller_rate),
        auth_failures=Limiter(settings.auth_failure_rate),
        trusted_proxies=settings.trusted_proxies,
        metrics=metrics,
    )


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        settings = edge_settings(os.environ)
    except SettingsError as error:
        sys.exit(f"golem edge: {error}")
    registry = process_registry()
    with httpx.Client(timeout=JWKS_TIMEOUT_SECONDS) as jwks_client:
        app = build_app(settings, jwks_client, Metrics("edge", registry=registry))
        # The client address comes from golem.ratelimit with GOLEM_TRUSTED_PROXIES, not uvicorn.
        servers = with_metrics(
            app,
            settings.port,
            registry=registry,
            metrics_port=settings.metrics_port,
            proxy_headers=False,
        )
        asyncio.run(serve_all(servers))


if __name__ == "__main__":
    main()
