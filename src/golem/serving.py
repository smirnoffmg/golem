"""Several uvicorn listeners in one process and one event loop: the ports a process serves its
callers on, and the metrics port (ADR 0009, ADR 0013)."""

import asyncio
import signal
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from typing import Any

import uvicorn
from prometheus_client import CollectorRegistry
from starlette.types import ASGIApp

from golem.metrics import metrics_app


class Listener(uvicorn.Server):
    # uvicorn.Server.serve installs its own SIGINT and SIGTERM handlers with signal.signal and
    # restores the previous ones when it returns. With several servers in one loop each would
    # replace the last, and a signal would stop only one of them; serve_all handles signals.
    @contextmanager
    def capture_signals(self) -> Iterator[None]:
        yield


def listener(app: ASGIApp, port: int, **config: Any) -> Listener:
    return Listener(uvicorn.Config(app, host="0.0.0.0", port=port, **config))


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


def with_metrics(
    app: ASGIApp, port: int, *, registry: CollectorRegistry, metrics_port: int, **config: Any
) -> list[Listener]:
    """A process's public listener and its metrics listener, never the same port."""
    if port == metrics_port:
        raise ValueError(f"metrics must not be served on the public port {port}")
    return [listener(app, port, **config), listener(metrics_app(registry), metrics_port)]
