"""Processes on the board in a real browser (ADR 0019): the demo stack with a process pinned, so
the edge's registry, derived from the catalogs, lets people start the process and not its
worker, as a deployment does. Its own module, so its stack follows the agent stack's rather
than sharing its databases.
"""

import re
from collections.abc import Iterator

import pytest
from playwright.sync_api import Browser, expect
from support.demo import PROCESS, STAGE_AGENT, Demo, databases_of, reconcile, running, seed
from test_ui_browser import column, sign_in, watched
from testcontainers.community.postgres import PostgresContainer

pytestmark = pytest.mark.e2e

PROCESS_BOARD = f"/agents/{PROCESS}"


@pytest.fixture(scope="module")
def demo(postgres: PostgresContainer, board_url: str) -> Iterator[Demo]:
    with running(databases_of(postgres), board_url, processes=True) as demo:
        yield demo


@pytest.fixture(scope="module")
def tasks(demo: Demo) -> dict[str, str]:
    return seed(demo)


def test_people_see_the_process_and_not_its_worker(
    browser: Browser, demo: Demo, tasks: dict[str, str]
) -> None:
    watch = watched(browser, demo)
    page = watch.page()
    sign_in(page, demo, PROCESS_BOARD)

    rail = page.get_by_role("navigation", name="Agents")
    expect(rail.locator(".rail-link")).to_have_text([re.compile(rf"^{PROCESS}")])
    for path in tasks.values():
        page.goto(path)
        expect(page.get_by_role("heading", level=1)).to_be_visible()
    assert watch.console_errors == watch.page_errors == watch.csp_violations == []
    watch.context.close()


def test_a_process_is_one_card_per_process_showing_its_stage(
    browser: Browser, demo: Demo, tasks: dict[str, str]
) -> None:
    watch = watched(browser, demo)
    page = watch.page()
    sign_in(page, demo, PROCESS_BOARD)

    waiting = column(page, "Waiting for me").locator(".card")
    review = column(page, "To review").locator(".card")
    expect(waiting).to_have_count(1)
    expect(review).to_have_count(1)
    expect(waiting).to_contain_text("Stage 1 of 2: evidence")
    expect(waiting.get_by_label("Why was it rejected?")).to_be_visible()
    expect(review).to_contain_text("Stage 1 of 2: evidence")
    expect(review).to_contain_text("close it with a comment")
    expect(review.get_by_role("link", name="Merge request")).to_have_attribute(
        "href", re.compile(r"/merge_requests/\d+$")
    )
    # A stage is not a card: its task belongs to the worker, which people cannot open.
    expect(page.locator(".card", has_text="Collect evidence:")).to_have_count(0)
    assert watch.console_errors == watch.csp_violations == []
    watch.context.close()


def test_rerunning_a_stage_needs_a_reason_and_starts_it_again(
    browser: Browser, demo: Demo, tasks: dict[str, str]
) -> None:
    watch = watched(browser, demo)
    page = watch.page()
    sign_in(page, demo, PROCESS_BOARD)
    card = column(page, "Waiting for me").locator(".card")
    rerun = card.get_by_role("button", name="Rerun the stage")
    expect(rerun).to_be_disabled()
    stages_before = len([s for s in demo.launcher.launched if s.agent == STAGE_AGENT])

    card.get_by_label("Why was it rejected?").fill("Count the tickets per customer, not per day.")
    rerun.click()
    expect(card.get_by_role("alert")).to_have_count(0)
    # The reconciler starts the attempt, then shows it on the process's task.
    reconcile(demo)
    reconcile(demo)
    page.reload()

    moved = column(page, "In progress").locator(".card", has_text="Attempt 2 of 3")
    expect(moved).to_have_count(1)
    expect(column(page, "Waiting for me").locator(".card")).to_have_count(0)
    assert len([s for s in demo.launcher.launched if s.agent == STAGE_AGENT]) == stages_before + 1
    assert watch.console_errors == watch.csp_violations == []
    watch.context.close()
