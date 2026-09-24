"""The task service process: ``python -m golem.tasks``."""

import asyncio
import logging
import os
import sys

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
from golem.tasks.app import create_app


def service_card(public_base_url: str) -> AgentCard:
    return AgentCard(
        name="golem",
        description="Runs catalog agents as A2A tasks.",
        version="0.1.0",
        supported_interfaces=[
            AgentInterface(
                url=f"{public_base_url}/a2a", protocol_binding="JSONRPC", protocol_version="1.0"
            )
        ],
        capabilities=AgentCapabilities(streaming=False),
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
    return create_app(service_card(settings.public_base_url), orchestrator)


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
