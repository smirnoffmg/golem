"""The web UI process: ``python -m golem.ui``."""

import asyncio
import logging
import os
import sys

import httpx
import psycopg
from starlette.types import ASGIApp

from golem.metrics import Metrics, process_registry
from golem.ratelimit import Limiter
from golem.serving import serve_all, with_metrics
from golem.settings import SettingsError, UiSettings, ui_settings
from golem.ui.app import create_ui_app
from golem.ui.oidc import OidcClient
from golem.ui.store import SessionStore, apply_schema

OUTBOUND_TIMEOUT_SECONDS = 10
# Below the golem_ui role's statement_timeout: a refresh holds its session row meanwhile.
IDP_TIMEOUT_SECONDS = 4
SCHEMA_LOCK = "golem_ui:schema"


def build_app(settings: UiSettings, metrics: Metrics | None = None) -> ASGIApp:
    return create_ui_app(
        oidc=OidcClient(
            http=httpx.AsyncClient(timeout=IDP_TIMEOUT_SECONDS),
            keys_http=httpx.Client(timeout=IDP_TIMEOUT_SECONDS),
            issuer=settings.issuer,
            discovery_url=settings.discovery_url,
            client_id=settings.client_id,
            client_secret=settings.client_secret,
            redirect_url=settings.redirect_url,
        ),
        store=SessionStore(settings.dsn, settings.session_key),
        edge=httpx.AsyncClient(base_url=settings.edge_url, timeout=OUTBOUND_TIMEOUT_SECONDS),
        agents=settings.agents,
        public_base_url=settings.public_base_url,
        logins=Limiter(settings.login_rate),
        starts=Limiter(settings.start_rate),
        trusted_proxies=settings.trusted_proxies,
        metrics=metrics,
    )


async def prepare(dsn: str) -> None:
    # Both replicas start together; concurrent CREATE TABLE IF NOT EXISTS can still collide in
    # the catalog, so they take turns.
    async with await psycopg.AsyncConnection.connect(dsn) as conn, conn.transaction():
        await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (SCHEMA_LOCK,))
        await apply_schema(conn)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        settings = ui_settings(os.environ)
    except SettingsError as error:
        sys.exit(f"golem ui: {error}")
    asyncio.run(prepare(settings.dsn))
    registry = process_registry()
    # The client address comes from golem.ratelimit with GOLEM_TRUSTED_PROXIES, not uvicorn.
    servers = with_metrics(
        build_app(settings, Metrics("ui", registry=registry)),
        settings.port,
        registry=registry,
        metrics_port=settings.metrics_port,
        proxy_headers=False,
    )
    asyncio.run(serve_all(servers))


if __name__ == "__main__":
    main()
