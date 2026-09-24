"""The Jira channel adapter process: ``python -m golem.adapters``."""

import logging
import os
import sys

import httpx
import uvicorn
from starlette.applications import Starlette

from golem.adapters.jira import ClientCredentials, create_jira_adapter_app, jira_authorization
from golem.settings import AdapterSettings, SettingsError, adapter_settings, parse_label_agents

OUTBOUND_TIMEOUT_SECONDS = 30


def build_app(settings: AdapterSettings) -> Starlette:
    credentials = ClientCredentials(
        httpx.AsyncClient(timeout=OUTBOUND_TIMEOUT_SECONDS),
        token_url=settings.token_url,
        client_id=settings.client_id,
        client_secret=settings.client_secret,
    )
    return create_jira_adapter_app(
        labels=parse_label_agents(settings.labels_file.read_text()),
        webhook_secret=settings.webhook_secret,
        push_secret=settings.push_secret,
        public_base_url=settings.public_base_url,
        edge=httpx.AsyncClient(base_url=settings.edge_url, timeout=OUTBOUND_TIMEOUT_SECONDS),
        service_token=credentials.token,
        jira=httpx.AsyncClient(
            base_url=settings.jira_url,
            headers={
                "Authorization": jira_authorization(
                    user=settings.jira_user, token=settings.jira_token
                ),
                "Accept": "application/json",
            },
            timeout=OUTBOUND_TIMEOUT_SECONDS,
        ),
    )


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        settings = adapter_settings(os.environ)
        app = build_app(settings)
    except SettingsError as error:
        sys.exit(f"golem jira adapter: {error}")
    uvicorn.run(app, host="0.0.0.0", port=settings.port)


if __name__ == "__main__":
    main()
