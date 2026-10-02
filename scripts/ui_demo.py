"""The board with seeded tasks, to look around: ``uv run python scripts/ui_demo.py``.

Builds and starts the board's image and Postgres in Docker (testcontainers), the real
backend-for-frontend, edge and task service behind a front that routes like the ingress, and a
fake identity provider that signs in ``alice`` without a password. ``--processes`` pins a process,
and people see only the process, as in a deployment that pins one. ``--proposals`` gives alice
three goal agents to review, with a proposal of each kind and a report. Stop with Ctrl-C.
"""

import argparse
import sys
import threading
from contextlib import suppress
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))

from support.demo import databases_of, postgres_container, running, seed
from support.front import board_server

DEFAULT_PORT = 8090


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="the UI's port")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--processes", action="store_true", help="pin a process (ADR 0019)")
    modes.add_argument(
        "--proposals", action="store_true", help="proposals for alice to review (ADR 0015)"
    )
    args = parser.parse_args()
    print("building the board and starting Postgres...", flush=True)
    with (
        postgres_container() as container,
        board_server() as board,
        running(
            databases_of(container),
            board,
            args.port,
            processes=args.processes,
            proposals=args.proposals,
        ) as demo,
    ):
        seed(demo)
        print(f"Golem board: {demo.ui_url} (signs in as alice). Ctrl-C stops it.", flush=True)
        with suppress(KeyboardInterrupt):
            threading.Event().wait()


if __name__ == "__main__":
    main()
