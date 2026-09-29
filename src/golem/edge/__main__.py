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

from golem.catalog import AgentCatalog, CatalogError, Catalogs, ProcessCatalog, load_catalogs
from golem.edge.app import create_edge_app
from golem.edge.auth import (
    AuthFailure,
    Principal,
    authenticate,
    authenticate_any,
    authenticate_call,
)
from golem.edge.card_signing import KEYS_PATH, card_keys, sign_card
from golem.edge.cards import build_public_card
from golem.edge.policy import ChainLimits
from golem.edge.registry import RegistryError, derive_registry
from golem.jwks import SigningKeys, fetch_jwks, key_id_of
from golem.metrics import Metrics, process_registry
from golem.ratelimit import Limiter
from golem.run_status import RunStatuses
from golem.run_token import SigningKey
from golem.serving import serve_all, with_metrics
from golem.settings import (
    EdgeSettings,
    SettingsError,
    edge_settings,
    parse_registry,
    parse_signing_key,
)
from golem.tasks.app import RUN_KEYS_PATH

JWKS_TIMEOUT_SECONDS = 2
RUN_STATUS_TIMEOUT_SECONDS = 2
FORWARD_TIMEOUT_SECONDS = 30


def authenticator(
    keys: SigningKeys, *, issuer: str, audience: str
) -> Callable[[str], Principal | AuthFailure]:
    def check(token: str) -> Principal | AuthFailure:
        current = keys.for_key_id(key_id_of(token))
        if current is None:
            return AuthFailure("signing keys unavailable")
        return authenticate(token, keys=current, issuer=issuer, audience=audience)

    return check


def call_authenticator(keys: SigningKeys) -> Callable[[str], Principal | AuthFailure]:
    def check(token: str) -> Principal | AuthFailure:
        current = keys.for_key_id(key_id_of(token))
        if current is None:
            return AuthFailure("Golem signing keys unavailable")
        return authenticate_call(token, keys=current)

    return check


def load_public_cards(
    catalogs_dir: Path, *, base_url: str, oidc_discovery_url: str, signing_key: SigningKey
) -> dict[str, AgentCard]:
    return public_cards(
        load_catalogs(catalogs_dir),
        base_url=base_url,
        oidc_discovery_url=oidc_discovery_url,
        signing_key=signing_key,
    )


def public_cards(
    catalogs: Catalogs, *, base_url: str, oidc_discovery_url: str, signing_key: SigningKey
) -> dict[str, AgentCard]:
    cards = {}
    published: list[AgentCatalog | ProcessCatalog] = [
        *catalogs.agents.values(),
        *catalogs.processes.values(),
    ]
    for catalog in sorted(published, key=lambda c: c.name):
        card = build_public_card(catalog, base_url=base_url, oidc_discovery_url=oidc_discovery_url)
        cards[catalog.name] = sign_card(card, signing_key, jku=f"{base_url}{KEYS_PATH}")
    return cards


def reviewers_of(catalogs: Catalogs) -> dict[str, frozenset[str]]:
    """Who may decide each agent's proposals besides their owner (ADR 0015)."""
    return {
        name: frozenset(agent.reviewers)
        for name, agent in catalogs.agents.items()
        if agent.reviewers
    }


def build_app(
    settings: EdgeSettings, jwks_client: httpx.Client, metrics: Metrics | None = None
) -> ASGIApp:
    keys = SigningKeys(partial(fetch_jwks, jwks_client, settings.jwks_url))
    keys.refresh()
    golem_keys = SigningKeys(
        partial(fetch_jwks, jwks_client, f"{settings.task_service_read_url}{RUN_KEYS_PATH}")
    )
    golem_keys.refresh()
    card_key = parse_signing_key(
        settings.card_signing_key_file.read_text(),
        settings.card_signing_kid,
        variable="GOLEM_CARD_SIGNING_KEY_FILE",
    )
    catalogs = load_catalogs(settings.catalogs_dir)
    limits = ChainLimits(max_depth=settings.max_chain_depth)
    return create_edge_app(
        authenticate=partial(
            authenticate_any,
            idp=authenticator(keys, issuer=settings.issuer, audience=settings.audience),
            golem=call_authenticator(golem_keys),
        ),
        registry=derive_registry(
            parse_registry(settings.registry_file.read_text()), catalogs, limits
        ),
        limits=limits,
        audit_dsn=settings.audit_dsn,
        forward=httpx.AsyncClient(
            base_url=settings.task_service_url, timeout=FORWARD_TIMEOUT_SECONDS
        ),
        edge_token=settings.edge_token,
        cards=public_cards(
            catalogs,
            base_url=settings.public_base_url,
            oidc_discovery_url=settings.oidc_discovery_url,
            signing_key=card_key,
        ),
        card_keys=card_keys([card_key]),
        public_base_url=settings.public_base_url,
        callers=Limiter(settings.caller_rate),
        auth_failures=Limiter(settings.auth_failure_rate),
        directory=Limiter(settings.directory_rate),
        trusted_proxies=settings.trusted_proxies,
        metrics=metrics,
        run_statuses=RunStatuses(
            httpx.AsyncClient(
                base_url=settings.task_service_read_url, timeout=RUN_STATUS_TIMEOUT_SECONDS
            ),
            ttl_seconds=settings.run_status_ttl_seconds,
        ),
        reviewers=reviewers_of(catalogs),
    )


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        settings = edge_settings(os.environ)
    except SettingsError as error:
        sys.exit(f"golem edge: {error}")
    registry = process_registry()
    with httpx.Client(timeout=JWKS_TIMEOUT_SECONDS) as jwks_client:
        try:
            app = build_app(settings, jwks_client, Metrics("edge", registry=registry))
        except (SettingsError, CatalogError, RegistryError, OSError) as error:
            sys.exit(f"golem edge: {error}")
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
