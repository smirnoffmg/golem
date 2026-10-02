"""The task service process: ``python -m golem.tasks``."""

import asyncio
import logging
import os
import sys
from dataclasses import dataclass

import httpx
from a2a.types.a2a_pb2 import AgentCapabilities, AgentCard, AgentInterface
from prometheus_client import CollectorRegistry
from sqlalchemy.ext.asyncio import AsyncEngine

from golem.catalog import CatalogError, Catalogs, load_catalogs
from golem.metrics import Metrics, metrics_app, process_registry
from golem.orchestrator.launchers import launcher_for
from golem.orchestrator.reconciler import apply_schema_once
from golem.orchestrator.service import PostgresOrchestrator
from golem.run_token import SigningKey
from golem.serving import Listener, listener, serve_all
from golem.settings import (
    SettingsError,
    TaskServiceSettings,
    parse_agent_tools,
    parse_catalog_refs,
    parse_signing_key,
    task_service_settings,
)
from golem.tasks.app import Listeners, PushDelivery, create_listeners
from golem.tasks.apply import McpApplier, NoWriteServers, parse_write_servers
from golem.tasks.ports import Applier
from golem.tasks.store import backfill_agents, push_config_store, tasks_engine, tasks_store


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


@dataclass(frozen=True)
class TaskService:
    listeners: Listeners
    engine: AsyncEngine
    orchestrator: PostgresOrchestrator


def pinned_catalogs(settings: TaskServiceSettings) -> Catalogs:
    """The pinned catalogs, checked as the edge checks them: a task for one of their processes
    is a process (ADR 0019), and each agent's catalog says what its runs propose (ADR 0015)."""
    if settings.catalogs_dir is None:
        return Catalogs(agents={}, processes={})
    try:
        return load_catalogs(settings.catalogs_dir)
    except (CatalogError, OSError) as error:
        raise SettingsError(f"GOLEM_CATALOGS_DIR: {error}") from error


def applier_for(settings: TaskServiceSettings, signing_key: SigningKey) -> Applier:
    if settings.write_servers_file is None:
        return NoWriteServers()
    servers = parse_write_servers(settings.write_servers_file.read_text())
    return McpApplier(servers=servers, signing_key=signing_key)


def build_service(
    settings: TaskServiceSettings, registry: CollectorRegistry | None = None
) -> TaskService:
    signing_key = parse_signing_key(settings.run_token_key_file.read_text(), settings.run_token_kid)
    catalogs = parse_catalog_refs(settings.catalogs_file.read_text())
    pinned = pinned_catalogs(settings)
    # The registered agents are the bounded set the run metrics name; others are "other".
    metrics = Metrics("tasks", registry=registry, agents=catalogs)
    orchestrator = PostgresOrchestrator(
        dsn=settings.runs_dsn,
        limits=settings.limits,
        estimated_cost=settings.estimated_cost,
        launcher=launcher_for(settings.kubernetes, settings.template.namespace),
        template=settings.template,
        catalogs=catalogs,
        signing_key=signing_key,
        grants=parse_agent_tools(settings.agent_tools_file.read_text()),
        metrics=metrics,
        processes=dict(pinned.processes),
        proposal_kinds={name: agent.proposal for name, agent in pinned.agents.items()},
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
    listeners = create_listeners(
        service_card(settings.public_base_url, push_notifications=push is not None),
        orchestrator,
        edge_token=settings.edge_token,
        task_store=tasks_store(engine),
        push=push,
        run_keys=(signing_key,),
        metrics=metrics,
        applier=applier_for(settings, signing_key),
    )
    return TaskService(listeners, engine, orchestrator)


def listener_servers(
    listeners: Listeners, settings: TaskServiceSettings, registry: CollectorRegistry
) -> list[Listener]:
    return [
        listener(app, port)
        for app, port in (
            (listeners.public, settings.port),
            (listeners.internal_read, settings.internal_read_port),
            (listeners.internal_write, settings.internal_write_port),
            # Not the read port: the MCP servers are admitted to that one (ADR 0013).
            (metrics_app(registry), settings.metrics_port),
        )
    ]


async def serve(
    settings: TaskServiceSettings, service: TaskService, registry: CollectorRegistry
) -> None:
    await apply_schema_once(settings.runs_dsn)
    # Tasks from before the task service recorded their agent would appear on no agent's list.
    await backfill_agents(service.engine, service.orchestrator.agents_of_tasks)
    await serve_all(listener_servers(service.listeners, settings, registry))


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        settings = task_service_settings(os.environ)
    except SettingsError as error:
        sys.exit(f"golem task service: {error}")
    registry = process_registry()
    try:
        service = build_service(settings, registry)
    except SettingsError as error:
        sys.exit(f"golem task service: {error}")
    asyncio.run(serve(settings, service, registry))


if __name__ == "__main__":
    main()
