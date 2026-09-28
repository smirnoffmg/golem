"""The UI's host as the ingress routes it (ADR 0018), for the demo stack: the
backend-for-frontend's paths to it, every other path to the board's nginx.

A plain reverse proxy that adds nothing and takes nothing away, so what the browser sees is
what the two containers answer: their headers, cookies and redirects.
"""

import subprocess
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import httpx
from starlette.types import Receive, Scope, Send
from testcontainers.core.container import DockerContainer

ROOT = Path(__file__).resolve().parents[2]
BOARD_DIR = ROOT / "board"
BOARD_IMAGE = "golem-board:dev"
BOARD_PORT = 8080
# The ingress sends these to Service ui; everything else goes to Service board.
BFF_PREFIXES = ("/api/",)
BFF_PATHS = frozenset({"/login", "/callback", "/logout", "/healthz"})
# Per connection, not end to end (RFC 9110, 7.6.1); the body is re-framed here.
HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-connection",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
        "host",
        "content-length",
    }
)
START_TIMEOUT_SECONDS = 30


def to_bff(path: str) -> bool:
    return path in BFF_PATHS or path.startswith(BFF_PREFIXES)


class Front:
    def __init__(self, bff_url: str, board_url: str) -> None:
        self.bff = httpx.AsyncClient(base_url=bff_url, timeout=30)
        self.board = httpx.AsyncClient(base_url=board_url, timeout=30)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            while (message := await receive())["type"] != "lifespan.shutdown":
                await send({"type": "lifespan.startup.complete"})
            await self.bff.aclose()
            await self.board.aclose()
            await send({"type": "lifespan.shutdown.complete"})
            return
        assert scope["type"] == "http"
        body = b""
        while True:
            message = await receive()
            body += message.get("body", b"")
            if not message.get("more_body"):
                break
        path = scope["path"]
        target = scope["raw_path"].decode() + (
            f"?{scope['query_string'].decode()}" if scope["query_string"] else ""
        )
        headers = [
            (name.decode(), value.decode())
            for name, value in scope["headers"]
            if name.decode().lower() not in HOP_BY_HOP | {"accept-encoding"}
        ]
        upstream = self.bff if to_bff(path) else self.board
        response = await upstream.request(scope["method"], target, headers=headers, content=body)
        await send(
            {
                "type": "http.response.start",
                "status": response.status_code,
                "headers": [
                    (name.encode(), value.encode())
                    for name, value in response.headers.multi_items()
                    if name.lower() not in HOP_BY_HOP
                ],
            }
        )
        await send({"type": "http.response.body", "body": response.content})


def build_board_image() -> None:
    subprocess.run(
        ["docker", "build", "--quiet", "--tag", BOARD_IMAGE, str(BOARD_DIR)],
        check=True,
        stdout=subprocess.DEVNULL,
    )


@contextmanager
def board_server() -> Iterator[str]:
    """The board's image, built from board/ and run as in the cluster: read-only root, /tmp a
    tmpfs, the unprivileged nginx user, no capabilities. Yields its URL."""
    build_board_image()
    container = (
        DockerContainer(BOARD_IMAGE)
        .with_exposed_ports(BOARD_PORT)
        .with_tmpfs_mount("/tmp")
        .with_kwargs(
            read_only=True,
            user="101",
            cap_drop=["ALL"],
            security_opt=["no-new-privileges"],
        )
    )
    with container:
        url = f"http://{container.get_container_host_ip()}:{container.get_exposed_port(BOARD_PORT)}"
        deadline = time.monotonic() + START_TIMEOUT_SECONDS
        while True:
            try:
                if httpx.get(f"{url}/", timeout=2).status_code == 200:
                    break
            except httpx.TransportError:
                pass
            if time.monotonic() > deadline:
                raise RuntimeError(f"the board did not start: {container.get_logs()}")
            time.sleep(0.2)
        yield url
