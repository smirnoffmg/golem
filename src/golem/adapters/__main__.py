"""The channel adapter processes: ``python -m golem.adapters [jira|mattermost]``.

Without an argument it is the Jira adapter, as before there was a second one.
"""

import logging
import os
import sys

import httpx
import uvicorn
from starlette.applications import Starlette

from golem.adapters.common import ClientCredentials
from golem.adapters.jira import create_jira_adapter_app, jira_authorization
from golem.adapters.mattermost import create_mattermost_adapter_app
from golem.ratelimit import Limiter
from golem.settings import (
    AdapterSettings,
    MattermostAdapterSettings,
    SettingsError,
    adapter_settings,
    mattermost_adapter_settings,
    parse_label_agents,
)

OUTBOUND_TIMEOUT_SECONDS = 30
ADAPTERS = ("jira", "mattermost")


def adapter_of(argv: list[str]) -> str:
    if not argv:
        return "jira"
    if len(argv) != 1 or argv[0] not in ADAPTERS:
        raise SettingsError(f"usage: python -m golem.adapters [{', '.join(ADAPTERS)}]")
    return argv[0]


def _credentials(*, token_url: str, client_id: str, client_secret: str) -> ClientCredentials:
    return ClientCredentials(
        httpx.AsyncClient(timeout=OUTBOUND_TIMEOUT_SECONDS),
        token_url=token_url,
        client_id=client_id,
        client_secret=client_secret,
    )


def build_app(settings: AdapterSettings) -> Starlette:
    credentials = _credentials(
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
        inbound=Limiter(settings.webhook_rate),
        trusted_proxies=settings.trusted_proxies,
    )


def build_mattermost_app(settings: MattermostAdapterSettings) -> Starlette:
    credentials = _credentials(
        token_url=settings.token_url,
        client_id=settings.client_id,
        client_secret=settings.client_secret,
    )
    return create_mattermost_adapter_app(
        command_token=settings.command_token,
        agents=settings.agents,
        teams=settings.teams,
        channels=settings.channels,
        push_secret=settings.push_secret,
        public_base_url=settings.public_base_url,
        edge=httpx.AsyncClient(base_url=settings.edge_url, timeout=OUTBOUND_TIMEOUT_SECONDS),
        service_token=credentials.token,
        mattermost=httpx.AsyncClient(
            base_url=settings.mattermost_url,
            headers={
                "Authorization": f"Bearer {settings.bot_token}",
                "Accept": "application/json",
            },
            timeout=OUTBOUND_TIMEOUT_SECONDS,
        ),
        inbound=Limiter(settings.command_rate),
        trusted_proxies=settings.trusted_proxies,
    )


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        adapter = adapter_of(sys.argv[1:])
        if adapter == "mattermost":
            mattermost = mattermost_adapter_settings(os.environ)
            app, port = build_mattermost_app(mattermost), mattermost.port
        else:
            jira = adapter_settings(os.environ)
            app, port = build_app(jira), jira.port
    except SettingsError as error:
        sys.exit(f"golem adapter: {error}")
    # The client address comes from golem.ratelimit with GOLEM_TRUSTED_PROXIES, not uvicorn.
    uvicorn.run(app, host="0.0.0.0", port=port, proxy_headers=False)


if __name__ == "__main__":
    main()
