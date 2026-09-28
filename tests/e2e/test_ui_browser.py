"""The board in a real browser: headless Chromium against the demo stack (tests/support/demo.py),
the board's own nginx image and the real backend-for-frontend, edge, task service and
orchestrator behind one front that routes like the ingress (ADR 0018).

What only a browser shows: whether the Content-Security-Policy blocks anything the board needs,
whether the stylesheet applies, whether polling moves a card without a reload, and whether the
pages fit a phone screen.
"""

import re
from collections.abc import Iterator
from dataclasses import dataclass, field

import httpx
import pytest
from playwright.sync_api import (
    Browser,
    BrowserContext,
    ConsoleMessage,
    Page,
    Response,
    expect,
    sync_playwright,
)
from support.demo import (
    AGENT,
    MERGE_REQUEST_URL,
    PROCESS,
    STAGE_AGENT,
    USER,
    Demo,
    databases_of,
    in_own_loop,
    reconcile,
    run_count,
    running,
    seed,
)
from support.front import board_server
from testcontainers.community.postgres import PostgresContainer

from golem.ui.edge import rpc

pytestmark = pytest.mark.e2e

TIMEOUT_MS = 10_000
# The board polls every 10 s; a card must have moved by the poll after next.
POLL_TIMEOUT_MS = 25_000
BOARD = f"/agents/{AGENT}"
PROCESS_BOARD = f"/agents/{PROCESS}"
# styles.css gives the top bar this background; the browser default is transparent.
TOPBAR_BACKGROUND = "rgb(35, 64, 95)"
SECURITY_HEADERS = {
    "x-content-type-options": "nosniff",
    "referrer-policy": "same-origin",
    "cross-origin-opener-policy": "same-origin",
}
CSP = (
    "default-src 'self'; frame-ancestors 'none'; form-action 'self'; base-uri 'none'; "
    "object-src 'none'"
)
# Reported through a binding, which the page's CSP cannot block.
REPORT_CSP_VIOLATIONS = """
document.addEventListener('securitypolicyviolation', (event) => {
  window.reportCspViolation(`${event.violatedDirective} blocked ${event.blockedURI}`);
});
"""


@dataclass
class Watched:
    """A signed-out browser context and everything its pages reported."""

    context: BrowserContext
    console_errors: list[str] = field(default_factory=list)
    page_errors: list[str] = field(default_factory=list)
    csp_violations: list[str] = field(default_factory=list)
    responses: list[Response] = field(default_factory=list)

    def page(self) -> Page:
        page = self.context.new_page()
        page.on("console", self._console)
        page.on("pageerror", lambda error: self.page_errors.append(str(error)))
        page.on("response", lambda response: self.responses.append(response))
        return page

    def _console(self, message: ConsoleMessage) -> None:
        # Signed out, the board asks for the session and gets the 401 that says so; Chromium
        # logs every 4xx resource as an error.
        signed_out = message.location.get("url", "").endswith("/api/session")
        if message.type == "error" and not signed_out:
            self.console_errors.append(message.text)


@pytest.fixture(scope="module")
def board_url() -> Iterator[str]:
    with board_server() as url:
        yield url


@pytest.fixture(scope="module")
def demo(postgres: PostgresContainer, board_url: str) -> Iterator[Demo]:
    with running(databases_of(postgres), board_url) as demo:
        yield demo


@pytest.fixture(scope="module")
def tasks(demo: Demo) -> dict[str, str]:
    return seed(demo)


@pytest.fixture(scope="module")
def browser() -> Iterator[Browser]:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        yield browser
        browser.close()


def watched(browser: Browser, demo: Demo, **options: object) -> Watched:
    context = browser.new_context(base_url=demo.ui_url, **options)  # type: ignore[arg-type]
    context.set_default_timeout(TIMEOUT_MS)
    result = Watched(context)
    context.expose_function("reportCspViolation", lambda text: result.csp_violations.append(text))
    context.add_init_script(REPORT_CSP_VIOLATIONS)
    return result


def sign_in(page: Page, demo: Demo, board: str = BOARD) -> None:
    page.goto("/")
    page.get_by_role("link", name="Sign in").click()
    # An agent and a process to choose from: the rail lists both.
    page.wait_for_url(f"{demo.ui_url}/")
    expect(page.get_by_role("navigation", name="Agents").locator(".rail-link")).to_have_count(2)
    page.goto(board)
    expect(page.get_by_role("heading", name=board.rsplit("/", 1)[-1], level=1)).to_be_visible()


def column(page: Page, title: str):
    return page.get_by_role("region", name=re.compile(rf"^{re.escape(title)}"))


@dataclass
class Walk:
    watched: Watched
    pages: list[str]
    topbar_backgrounds: dict[str, str]


@pytest.fixture(scope="module")
def walk(browser: Browser, demo: Demo, tasks: dict[str, str]) -> Iterator[Walk]:
    """alice signs in and opens the board and every seeded task."""
    watch = watched(browser, demo)
    page = watch.page()
    page.goto("/")
    expect(page.get_by_role("link", name="Sign in")).to_be_visible()
    backgrounds = {"/": topbar_background(page)}
    sign_in(page, demo)
    paths = [BOARD, PROCESS_BOARD, *tasks.values()]
    for path in paths:
        page.goto(path)
        expect(page.get_by_role("heading", level=1)).to_be_visible()
        expect(page.get_by_text("This page does not exist.")).to_have_count(0)
        backgrounds[path] = topbar_background(page)
    yield Walk(watch, ["/", *paths], backgrounds)
    watch.context.close()


def topbar_background(page: Page) -> str:
    return page.evaluate("getComputedStyle(document.querySelector('.topbar')).backgroundColor")


def test_pages_log_no_errors(walk: Walk) -> None:
    assert walk.watched.console_errors == []
    assert walk.watched.page_errors == []


def test_the_content_security_policy_blocks_nothing_the_board_uses(walk: Walk) -> None:
    assert walk.watched.csp_violations == []


def test_a_violation_would_be_seen(browser: Browser, demo: Demo) -> None:
    # The check above is only as good as the listener: a blocked image must reach it.
    watch = watched(browser, demo)
    page = watch.page()
    page.goto("/")

    page.evaluate(
        "document.body.append(Object.assign(new Image(), {src: 'https://example.com/x.png'}))"
    )

    page.wait_for_timeout(500)
    watch.context.close()
    assert any("example.com" in violation for violation in watch.csp_violations)


def test_the_stylesheet_applies_on_every_page(walk: Walk) -> None:
    assert walk.topbar_backgrounds == dict.fromkeys(walk.pages, TOPBAR_BACKGROUND)


def test_both_containers_send_the_security_headers(walk: Walk, demo: Demo) -> None:
    ours = [r for r in walk.watched.responses if r.url.startswith(demo.ui_url)]
    kinds = {r.request.resource_type for r in ours}
    assert {"document", "script", "stylesheet", "fetch"} <= kinds

    for response in ours:
        headers = response.all_headers()
        assert headers["content-security-policy"] == CSP, response.url
        for name, value in SECURITY_HEADERS.items():
            assert headers.get(name) == value, (response.url, name)


def test_caching_follows_what_each_answer_is(walk: Walk, demo: Demo) -> None:
    ours = [r for r in walk.watched.responses if r.url.startswith(demo.ui_url) and r.ok]

    for response in ours:
        path = response.url.removeprefix(demo.ui_url)
        cache = response.all_headers().get("cache-control")
        if path.startswith("/api/"):
            assert cache == "no-store", path
        elif path.startswith("/assets/"):
            assert cache == "public, max-age=31536000, immutable", path
        elif response.request.resource_type == "document":
            assert cache == "no-cache", path


def test_every_seeded_task_is_in_its_column(
    browser: Browser, demo: Demo, tasks: dict[str, str]
) -> None:
    watch = watched(browser, demo)
    page = watch.page()
    sign_in(page, demo)

    expect(column(page, "In progress").locator(".card")).to_have_count(1)
    expect(column(page, "To review").locator(".card")).to_have_count(1)
    expect(column(page, "Failed").locator(".card")).to_have_count(2)
    expect(column(page, "Waiting for me").locator(".card")).to_have_count(0)
    review = column(page, "To review")
    expect(review.get_by_role("link", name="Merge request")).to_have_attribute(
        "href", MERGE_REQUEST_URL
    )
    expect(column(page, "Failed")).to_contain_text("status changes are human decisions")
    page.locator(".archive summary").click()
    expect(page.locator(".archive .card")).to_have_count(1)
    assert watch.console_errors == watch.csp_violations == []
    watch.context.close()


def test_the_form_starts_exactly_one_run_and_shows_the_goal_as_text(
    browser: Browser, demo: Demo, tasks: dict[str, str]
) -> None:
    watch = watched(browser, demo)
    page = watch.page()
    sign_in(page, demo)
    runs_before, launched_before = run_count(demo), len(demo.launcher.launched)
    goal = "Check <b>H-4</b> & report"

    page.get_by_label(f"New task for {AGENT}").fill(goal)
    page.get_by_role("button", name="Start").click()

    card = column(page, "In progress").locator(".card", has_text="Check <b>H-4</b> & report")
    expect(card).to_have_count(1)
    assert run_count(demo) == runs_before + 1
    assert len(demo.launcher.launched) == launched_before + 1
    assert page.locator("main b").count() == 0
    assert watch.console_errors == watch.csp_violations == []
    watch.context.close()


def test_a_card_moves_when_its_task_changes_elsewhere(
    browser: Browser, demo: Demo, tasks: dict[str, str]
) -> None:
    watch = watched(browser, demo)
    page = watch.page()
    sign_in(page, demo)
    goal = "Moved by a poll, not by a reload."
    page.get_by_label(f"New task for {AGENT}").fill(goal)
    page.get_by_role("button", name="Start").click()
    card = page.locator(".card", has_text=goal)
    expect(column(page, "In progress").locator(".card", has_text=goal)).to_have_count(1)
    task_id = card.get_by_role("link").get_attribute("href").rsplit("/", 1)[-1]  # type: ignore[union-attr]

    cancel_elsewhere(demo, task_id)

    page.locator(".archive summary").click()
    expect(page.locator(".archive .card", has_text=goal)).to_have_count(1, timeout=POLL_TIMEOUT_MS)
    expect(column(page, "In progress").locator(".card", has_text=goal)).to_have_count(0)
    watch.context.close()


def cancel_elsewhere(demo: Demo, task_id: str) -> None:
    """Another client of the edge, with alice's token, cancels the task."""

    async def cancel() -> None:
        async with httpx.AsyncClient(base_url=demo.edge_url, timeout=10) as edge:
            token = demo.idp.access_token(USER)
            await rpc(edge, token, "CancelTask", {"tenant": AGENT, "id": task_id})

    in_own_loop(cancel())


def test_signing_out_returns_to_the_landing_page(browser: Browser, demo: Demo) -> None:
    watch = watched(browser, demo)
    page = watch.page()
    sign_in(page, demo)

    page.get_by_role("button", name="Sign out").click()
    page.wait_for_url(f"{demo.ui_url}/")

    expect(page.get_by_role("link", name="Sign in")).to_be_visible()
    assert [c["name"] for c in watch.context.cookies()] == []
    watch.context.close()


def test_the_board_fits_a_phone_screen(browser: Browser, demo: Demo, tasks: dict[str, str]) -> None:
    watch = watched(browser, demo, viewport={"width": 390, "height": 844})
    page = watch.page()
    sign_in(page, demo)

    for path in (BOARD, tasks["completed"], tasks["failed"], "/"):
        page.goto(path)
        expect(page.locator(".topbar")).to_be_visible()
        overflow = page.evaluate("document.documentElement.scrollWidth - window.innerWidth")
        assert overflow <= 0, path
    watch.context.close()


# --- Processes (ADR 0019) --------------------------------------------------------------------


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
