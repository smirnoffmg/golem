"""The web UI with seeded tasks, to look around: ``uv run python scripts/ui_demo.py``.

Starts Postgres in Docker (testcontainers), the real UI, edge and task service, and a fake
identity provider that signs in ``alice`` without a password. Stop with Ctrl-C.
"""

import argparse
import sys
import threading
from contextlib import suppress
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))

from support.demo import databases_of, postgres_container, running, seed

DEFAULT_PORT = 8090


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="the UI's port")
    port = parser.parse_args().port
    print("starting Postgres...", flush=True)
    with postgres_container() as container, running(databases_of(container), port) as demo:
        seed(demo)
        print(f"Golem UI: {demo.ui_url} (signs in as alice). Ctrl-C stops it.", flush=True)
        with suppress(KeyboardInterrupt):
            threading.Event().wait()


if __name__ == "__main__":
    main()
