"""Screenshots of the board for docs/images/ui: ``uv run python scripts/ui_screenshots.py``.

Runs the demo stack of scripts/ui_demo.py with ids and timestamps frozen, signs in as alice in
headless Chromium with the browser's clock fixed two hours after the seed, and captures the
board, a process's board and every task state at 1280x800, the board also at 390x844. Needs
Docker and ``uv run playwright install --only-shell chromium`` once.
"""

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from playwright.sync_api import BrowserContext, Page, expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))

from support.demo import (
    AGENT,
    PROCESS,
    databases_of,
    frozen,
    postgres_container,
    running,
    seed,
)
from support.front import board_server

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "images" / "ui"
WIDE = {"width": 1280, "height": 800}
NARROW = {"width": 390, "height": 844}
SEEDED_AT = datetime(2026, 9, 1, 9, 0, tzinfo=UTC)
# "Updated 2 h ago" reads the same on every run.
BROWSER_NOW = SEEDED_AT + timedelta(hours=2)
BOARD = f"/agents/{AGENT}"
PROCESS_BOARD = f"/agents/{PROCESS}"
NEW_GOAL = "Collect evidence for hypothesis H-4 from the support tickets of the last quarter."
# The backend's start limit is a burst of 5 per session; the sixth start in a row is refused.
START_BURST = 5
TIMEOUT_MS = 10_000


def shoot(page: Page, name: str) -> Path:
    path = OUT / f"{name}.png"
    page.screenshot(path=path, animations="disabled", caret="hide")
    return path


def use_up_starts(context: BrowserContext) -> None:
    """Starts through the API with the session's own CSRF token, until the bucket is empty."""
    csrf = context.request.get("/api/session").json()["csrf"]
    for index in range(START_BURST):
        started = context.request.post(
            f"/api/agents/{AGENT}/tasks",
            headers={"X-Golem-CSRF": csrf, "Content-Type": "application/json"},
            data={"goal": f"Burst {index}", "nonce": f"screenshot-burst-{index:02d}-padding"},
        )
        assert started.status in (201, 429), started.status


def capture(ui_url: str, tasks: dict[str, str]) -> list[Path]:
    shots = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        context = browser.new_context(
            viewport=WIDE, base_url=ui_url, locale="en-US", color_scheme="light"
        )
        context.set_default_timeout(TIMEOUT_MS)
        context.clock.set_fixed_time(BROWSER_NOW)
        page = context.new_page()

        page.goto("/")
        expect(page.get_by_role("link", name="Sign in")).to_be_visible()
        shots.append(shoot(page, "sign-in"))
        page.get_by_role("link", name="Sign in").click()
        page.wait_for_url(f"{ui_url}/")
        page.goto(BOARD)
        expect(page.locator(".card").first).to_be_visible()
        shots.append(shoot(page, "board"))
        page.goto(PROCESS_BOARD)
        expect(page.locator(".resolution")).to_be_visible()
        shots.append(shoot(page, "process-board"))
        page.goto(BOARD)
        page.get_by_label(f"New task for {AGENT}").fill(NEW_GOAL)
        shots.append(shoot(page, "new-task"))
        page.get_by_label(f"New task for {AGENT}").fill("")
        for state in ("working", "completed", "failed", "rejected", "canceled"):
            page.goto(tasks[state])
            expect(page.locator(".facts")).to_be_visible()
            shots.append(shoot(page, f"task-{state}"))

        page.set_viewport_size(NARROW)
        page.goto(BOARD)
        expect(page.locator(".card").first).to_be_visible()
        shots.append(shoot(page, "board-narrow"))
        page.set_viewport_size(WIDE)

        use_up_starts(context)
        page.goto(BOARD)
        page.get_by_label(f"New task for {AGENT}").fill(NEW_GOAL)
        page.get_by_role("button", name="Start").click()
        expect(page.locator(".start .form-error")).to_be_visible()
        shots.append(shoot(page, "error-rate-limited"))
        browser.close()
    return shots


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    print("building the board and starting Postgres...", flush=True)
    with (
        postgres_container() as container,
        board_server() as board,
        running(databases_of(container), board) as demo,
    ):
        with frozen(SEEDED_AT):
            tasks = seed(demo)
        shots = capture(demo.ui_url, tasks)
    total = 0
    for path in shots:
        size = path.stat().st_size
        total += size
        print(f"{path.relative_to(ROOT)}  {size / 1024:.0f} KiB")
    print(f"{len(shots)} screenshots, {total / 1024:.0f} KiB in all")


if __name__ == "__main__":
    main()
