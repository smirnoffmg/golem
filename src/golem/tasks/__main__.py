"""The task service process: ``python -m golem.tasks``."""

import asyncio
import logging
import os
import sys

import httpx
import uvicorn
from a2a.types.a2a_pb2 import AgentCapabilities, AgentCard, AgentInterface
from starlette.applications import Starlette

from golem.orchestrator.launchers import launcher_for
from golem.orchestrator.reconciler import apply_schema_once
from golem.orchestrator.service import PostgresOrchestrator
from golem.settings import (
    SettingsError,
    TaskServiceSettings,
    parse_catalog_refs,
    task_service_settings,
)
from golem.tasks.app import PushDelivery, create_app
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


def build_app(settings: TaskServiceSettings) -> Starlette:
    orchestrator = PostgresOrchestrator(
        dsn=settings.runs_dsn,
        limits=settings.limits,
        estimated_cost=settings.estimated_cost,
        launcher=launcher_for(settings.kubernetes, settings.template.namespace),
        template=settings.template,
        catalogs=parse_catalog_refs(settings.catalogs_file.read_text()),
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
    return create_app(
        service_card(settings.public_base_url, push_notifications=push is not None),
        orchestrator,
        tasks_store(engine),
        push,
    )


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        settings = task_service_settings(os.environ)
    except SettingsError as error:
        sys.exit(f"golem task service: {error}")
    asyncio.run(apply_schema_once(settings.runs_dsn))
    uvicorn.run(build_app(settings), host="0.0.0.0", port=settings.port)


if __name__ == "__main__":
    main()
