"""Screenshots of the web UI for docs/images/ui: ``uv run python scripts/ui_screenshots.py``.

Runs the demo stack of scripts/ui_demo.py with ids and timestamps frozen, signs in as alice in
headless Chromium and captures every page at 1280x800, the task list also at 390x844.
Needs Docker and ``uv run playwright install --only-shell chromium`` once.
"""

import sys
from datetime import UTC, datetime
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))

from support.demo import databases_of, frozen, postgres_container, running, seed

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "images" / "ui"
WIDE = {"width": 1280, "height": 800}
NARROW = {"width": 390, "height": 844}
SEEDED_AT = datetime(2026, 9, 1, 9, 0, tzinfo=UTC)
NEW_GOAL = "Collect evidence for hypothesis H-4 from the support tickets of the last quarter."
# The UI's start limit is a burst of 5 per session; the sixth start in a row is refused.
START_BURST = 5
TIMEOUT_MS = 10_000


def shoot(page: Page, name: str) -> Path:
    path = OUT / f"{name}.png"
    page.screenshot(path=path, animations="disabled", caret="hide")
    return path


def capture(ui_url: str, tasks: dict[str, str]) -> list[Path]:
    shots = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        context = browser.new_context(viewport=WIDE, base_url=ui_url, locale="en-US")
        context.set_default_timeout(TIMEOUT_MS)
        page = context.new_page()

        page.goto("/")
        shots.append(shoot(page, "sign-in"))
        page.get_by_role("link", name="Sign in").click()
        page.wait_for_url(f"{ui_url}/tasks")
        page.goto("/agents")
        shots.append(shoot(page, "agents"))
        page.goto("/tasks/new")
        page.get_by_label("Goal").fill(NEW_GOAL)
        shots.append(shoot(page, "new-task"))
        page.goto("/tasks")
        shots.append(shoot(page, "tasks"))
        for state in ("working", "completed", "failed", "rejected", "canceled"):
            page.goto(tasks[state])
            shots.append(shoot(page, f"task-{state}"))

        page.set_viewport_size(NARROW)
        page.goto("/tasks")
        shots.append(shoot(page, "tasks-narrow"))
        page.set_viewport_size(WIDE)

        # Refused starts (a wrong CSRF token) still count against the limit, so the form that
        # follows is refused without a run being started.
        for _ in range(START_BURST):
            refused = context.request.post("/tasks", form={"csrf": "-"}, max_redirects=0)
            assert refused.status == 403, refused.status
        page.goto("/tasks/new")
        page.get_by_label("Goal").fill(NEW_GOAL)
        page.get_by_role("button", name="Start").click()
        page.wait_for_load_state()
        shots.append(shoot(page, "error-rate-limited"))
        browser.close()
    return shots


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    print("starting Postgres...", flush=True)
    with postgres_container() as container, running(databases_of(container)) as demo:
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
