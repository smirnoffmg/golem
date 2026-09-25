import asyncio
import logging
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
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


async def test_a_pass_that_hangs_is_abandoned_and_the_next_one_runs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    stop = asyncio.Event()
    passes: list[int] = []

    async def reconcile_pass() -> None:
        passes.append(len(passes))
        if len(passes) == 1:
            await asyncio.Event().wait()
        stop.set()

    with caplog.at_level(logging.ERROR):
        await asyncio.wait_for(
            run_forever(reconcile_pass, interval_seconds=0.01, stop=stop, pass_timeout_seconds=0.1),
            5,
        )

    assert len(passes) == 2
    assert "reconcile pass failed" in caplog.text


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def reconciler_env(runs_dsn: str, tmp_path: Path, metrics_port: int = 0) -> dict[str, str]:
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
        "GOLEM_METRICS_PORT": str(metrics_port or free_port()),
    }


def test_the_reconciler_process_serves_metrics_and_stops_cleanly_on_sigterm(
    runs_db: str, tmp_path: Path
) -> None:
    port = free_port()
    process = subprocess.Popen(
        [sys.executable, "-m", "golem.orchestrator.reconciler"],
        env=reconciler_env(runs_db, tmp_path, port),
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
        deadline = time.monotonic() + 10
        while True:
            try:
                exposed = httpx.get(f"http://127.0.0.1:{port}/metrics", timeout=2)
                if "golem_reconcile_pass_duration_seconds_count 0.0" not in exposed.text:
                    break
            except httpx.TransportError:
                pass
            assert time.monotonic() < deadline, "no metrics after a pass"
            time.sleep(0.2)
        assert "golem_outbox_pending 0.0" in exposed.text
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
