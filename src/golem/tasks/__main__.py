"""The task service process: ``python -m golem.tasks``."""

import asyncio
import logging
import os
import signal
import sys
from collections.abc import Iterator, Sequence
from contextlib import contextmanager

import httpx
import uvicorn
from a2a.types.a2a_pb2 import AgentCapabilities, AgentCard, AgentInterface

from golem.orchestrator.launchers import launcher_for
from golem.orchestrator.reconciler import apply_schema_once
from golem.orchestrator.service import PostgresOrchestrator
from golem.settings import (
    SettingsError,
    TaskServiceSettings,
    parse_agent_tools,
    parse_catalog_refs,
    parse_signing_key,
    task_service_settings,
)
from golem.tasks.app import Listeners, PushDelivery, create_listeners
from golem.tasks.store import push_config_store, tasks_engine, tasks_store


def service_card(public_base_url: str, push_notifications: bool = False) -> AgentCard:
    return AgentCard(
        name="golem",
        description="Runs catalog agents as A2A tasks.",
        version="0.1.0",
        supported_interfaces=[
            AgentInterface(
                url=f"{public_base_url}/a2a", protocol_binding="JSONRPC", protocol_version="1.0"
            )
        ],
        capabilities=AgentCapabilities(streaming=False, push_notifications=push_notifications),
        default_input_modes=["text/plain"],
        default_output_modes=["text/plain"],
    )


def build_listeners(settings: TaskServiceSettings) -> Listeners:
    signing_key = parse_signing_key(settings.run_token_key_file.read_text(), settings.run_token_kid)
    orchestrator = PostgresOrchestrator(
        dsn=settings.runs_dsn,
        limits=settings.limits,
        estimated_cost=settings.estimated_cost,
        launcher=launcher_for(settings.kubernetes, settings.template.namespace),
        template=settings.template,
        catalogs=parse_catalog_refs(settings.catalogs_file.read_text()),
        signing_key=signing_key,
        grants=parse_agent_tools(settings.agent_tools_file.read_text()),
    )
    engine = tasks_engine(settings.tasks_db_url)
    push = (
        PushDelivery(
            config_store=push_config_store(engine, settings.push_config_key or ""),
            client=httpx.AsyncClient(timeout=10),
            allowed_prefixes=settings.push_allowed_prefixes,
        )
        if settings.push_allowed_prefixes
        else None
    )
    return create_listeners(
        service_card(settings.public_base_url, push_notifications=push is not None),
        orchestrator,
        edge_token=settings.edge_token,
        task_store=tasks_store(engine),
        push=push,
        run_keys=(signing_key,),
    )


class Listener(uvicorn.Server):
    # uvicorn.Server.serve installs its own SIGINT and SIGTERM handlers with signal.signal and
    # restores the previous ones when it returns. With three servers in one loop each would
    # replace the last, and a signal would stop only one of them; serve_all handles signals.
    @contextmanager
    def capture_signals(self) -> Iterator[None]:
        yield


def listener_servers(listeners: Listeners, settings: TaskServiceSettings) -> list[Listener]:
    return [
        Listener(uvicorn.Config(app, host="0.0.0.0", port=port))
        for app, port in (
            (listeners.public, settings.port),
            (listeners.internal_read, settings.internal_read_port),
            (listeners.internal_write, settings.internal_write_port),
        )
    ]


async def serve_all(servers: Sequence[uvicorn.Server]) -> None:
    """Serve until a signal arrives or any server stops; then all of them stop."""
    loop = asyncio.get_running_loop()

    def stop_all() -> None:
        for server in servers:
            server.should_exit = True

    async def serve_one(server: uvicorn.Server) -> None:
        try:
            await server.serve()
        finally:
            stop_all()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop_all)
    try:
        await asyncio.gather(*(serve_one(server) for server in servers))
    finally:
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.remove_signal_handler(sig)


async def serve(settings: TaskServiceSettings, listeners: Listeners) -> None:
    await apply_schema_once(settings.runs_dsn)
    await serve_all(listener_servers(listeners, settings))


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        settings = task_service_settings(os.environ)
    except SettingsError as error:
        sys.exit(f"golem task service: {error}")
    try:
        listeners = build_listeners(settings)
    except SettingsError as error:
        sys.exit(f"golem task service: {error}")
    asyncio.run(serve(settings, listeners))


if __name__ == "__main__":
    main()
