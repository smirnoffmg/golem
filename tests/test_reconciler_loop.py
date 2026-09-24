import asyncio
import logging
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from golem.orchestrator.reconciler import run_forever


async def test_a_failing_pass_is_logged_and_the_loop_continues(
    caplog: pytest.LogCaptureFixture,
) -> None:
    stop = asyncio.Event()
    passes: list[int] = []

    async def reconcile_pass() -> None:
        passes.append(len(passes))
        if len(passes) == 1:
            raise RuntimeError("database went away")
        if len(passes) == 3:
            stop.set()

    with caplog.at_level(logging.ERROR):
        await run_forever(reconcile_pass, interval_seconds=0.01, stop=stop)

    assert len(passes) == 3
    assert "database went away" in caplog.text


async def test_stop_interrupts_the_wait_between_passes() -> None:
    stop = asyncio.Event()

    async def reconcile_pass() -> None:
        asyncio.get_running_loop().call_later(0.05, stop.set)

    started = time.monotonic()
    await asyncio.wait_for(run_forever(reconcile_pass, interval_seconds=60, stop=stop), 5)

    assert time.monotonic() - started < 5


def reconciler_env(runs_dsn: str, tmp_path: Path) -> dict[str, str]:
    projects = tmp_path / "projects.yaml"
    projects.write_text("discovery:\n  project: product/discovery-context\n  target_branch: main\n")
    return {
        "PATH": os.environ["PATH"],
        "GOLEM_RUNS_DSN": runs_dsn,
        "GOLEM_RECONCILE_INTERVAL_SECONDS": "0.2",
        "GOLEM_TASK_SERVICE_URL": "http://127.0.0.1:1",
        "GOLEM_GITLAB_URL": "http://127.0.0.1:1",
        "GOLEM_GITLAB_TOKEN": "unused",
        "GOLEM_GITLAB_PROJECTS_FILE": str(projects),
        "GOLEM_KUBERNETES_NAMESPACE": "team-jobs",
        "GOLEM_KUBERNETES": "none",
    }


def test_the_reconciler_process_stops_cleanly_on_sigterm(runs_db: str, tmp_path: Path) -> None:
    process = subprocess.Popen(
        [sys.executable, "-m", "golem.orchestrator.reconciler"],
        env=reconciler_env(runs_db, tmp_path),
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert process.stderr is not None
        lines = []
        for line in process.stderr:
            lines.append(line)
            if "reconciler started" in line:
                break
        else:
            pytest.fail("".join(lines))
        time.sleep(0.5)
        process.send_signal(signal.SIGTERM)
        assert process.wait(timeout=10) == 0
        assert "Traceback" not in process.stderr.read()
    finally:
        process.kill()


def test_the_reconciler_refuses_to_start_without_settings() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "golem.orchestrator.reconciler"],
        env={"PATH": os.environ["PATH"]},
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode != 0
    assert "GOLEM_RUNS_DSN" in result.stderr
    assert "GOLEM_GITLAB_TOKEN" in result.stderr
