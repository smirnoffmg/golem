"""The web UI in a real browser: headless Chromium against the demo stack (tests/support/demo.py),
the real UI, edge, task service and orchestrator on localhost ports.

What only a browser shows: whether the Content-Security-Policy blocks anything the pages need,
whether the stylesheet applies, and whether the pages fit a phone screen.
"""

import re
from collections.abc import Iterator
from dataclasses import dataclass, field

import pytest
from playwright.sync_api import Browser, BrowserContext, Page, Response, sync_playwright
from support.demo import AGENT, Demo, databases_of, run_count, running, seed
from testcontainers.community.postgres import PostgresContainer

pytestmark = pytest.mark.e2e

TIMEOUT_MS = 10_000
# golem.css gives the header this background; the browser default is transparent.
HEADER_BACKGROUND = "rgb(29, 35, 41)"
SECURITY_HEADERS = {
    "x-content-type-options": "nosniff",
    "referrer-policy": "same-origin",
    "cross-origin-opener-policy": "same-origin",
    "cache-control": "no-store",
}
CSP_PREFIX = "default-src 'self'; frame-ancestors 'none'; form-action 'self'; base-uri 'none'"
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
        page.on("console", lambda m: m.type == "error" and self.console_errors.append(m.text))
        page.on("pageerror", lambda error: self.page_errors.append(str(error)))
        page.on("response", lambda response: self.responses.append(response))
        return page


@pytest.fixture(scope="module")
def demo(postgres: PostgresContainer) -> Iterator[Demo]:
    with running(databases_of(postgres)) as demo:
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


def sign_in(page: Page, demo: Demo) -> None:
    page.goto("/")
    page.get_by_role("link", name="Sign in").click()
    page.wait_for_url(f"{demo.ui_url}/tasks")


@dataclass
class Walk:
    watched: Watched
    pages: list[str]
    header_backgrounds: dict[str, str]
    link_statuses: dict[str, int]


@pytest.fixture(scope="module")
def walk(browser: Browser, demo: Demo, tasks: dict[str, str]) -> Iterator[Walk]:
    """alice signs in and opens every page, following every link on them."""
    watch = watched(browser, demo)
    page = watch.page()
    page.goto("/")
    backgrounds = {"/": header_background(page)}
    links = hrefs(page)
    sign_in(page, demo)
    paths = ["/agents", "/tasks/new", "/tasks", *tasks.values()]
    for path in paths:
        page.goto(path)
        backgrounds[path] = header_background(page)
        links |= hrefs(page)
    internal = sorted(link for link in links if link.startswith("/"))
    statuses = {link: watch.context.request.get(link).status for link in internal}
    yield Walk(watch, ["/", *paths], backgrounds, statuses)
    watch.context.close()


def header_background(page: Page) -> str:
    return page.evaluate("getComputedStyle(document.querySelector('header')).backgroundColor")


def hrefs(page: Page) -> set[str]:
    return set(
        page.eval_on_selector_all("a[href]", "links => links.map(a => a.getAttribute('href'))")
    )


def test_pages_log_no_errors(walk: Walk) -> None:
    assert walk.watched.console_errors == []
    assert walk.watched.page_errors == []


def test_the_content_security_policy_blocks_nothing_the_pages_use(walk: Walk) -> None:
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
    assert walk.header_backgrounds == dict.fromkeys(walk.pages, HEADER_BACKGROUND)


def test_every_response_carries_the_security_headers(walk: Walk, demo: Demo) -> None:
    ours = [r for r in walk.watched.responses if r.url.startswith(demo.ui_url)]
    kinds = {r.request.resource_type for r in ours}
    assert {"document", "stylesheet"} <= kinds

    for response in ours:
        headers = response.all_headers()
        assert headers["content-security-policy"].startswith(CSP_PREFIX), response.url
        assert "object-src 'none'" in headers["content-security-policy"], response.url
        for name, value in SECURITY_HEADERS.items():
            assert headers.get(name) == value, (response.url, name)


def test_every_link_on_the_pages_leads_somewhere(walk: Walk) -> None:
    assert {"/", "/agents", "/tasks", "/tasks/new", f"/tasks/new?agent={AGENT}"} <= set(
        walk.link_statuses
    )
    assert {link: status for link, status in walk.link_statuses.items() if status != 200} == {}


def test_the_form_starts_exactly_one_run_and_shows_the_goal_as_text(
    browser: Browser, demo: Demo, tasks: dict[str, str]
) -> None:
    watch = watched(browser, demo)
    page = watch.page()
    sign_in(page, demo)
    runs_before, launched_before = run_count(demo), len(demo.launcher.launched)
    goal = "Check <b>H-4</b> & report"

    page.goto("/tasks/new")
    page.get_by_label("Goal").fill(goal)
    page.get_by_role("button", name="Start").click()
    page.wait_for_url(re.compile(rf"/tasks/{AGENT}/[0-9a-f-]+$"))

    assert run_count(demo) == runs_before + 1
    assert len(demo.launcher.launched) == launched_before + 1
    assert page.locator("dd.state").inner_text() == "working"
    assert goal in page.locator("main").inner_text()
    assert page.locator("main b").count() == 0
    assert watch.console_errors == watch.csp_violations == []
    watch.context.close()


def test_signing_out_returns_to_the_landing_page(browser: Browser, demo: Demo) -> None:
    watch = watched(browser, demo)
    page = watch.page()
    sign_in(page, demo)

    page.get_by_role("button", name="Sign out").click()
    page.wait_for_url(f"{demo.ui_url}/")

    assert page.get_by_role("link", name="Sign in").is_visible()
    assert [c["name"] for c in watch.context.cookies()] == []
    watch.context.close()


def test_the_task_list_fits_a_phone_screen(
    browser: Browser, demo: Demo, tasks: dict[str, str]
) -> None:
    watch = watched(browser, demo, viewport={"width": 390, "height": 844})
    page = watch.page()
    sign_in(page, demo)

    for path in ("/tasks", tasks["completed"], "/tasks/new", "/agents"):
        page.goto(path)
        overflow = page.evaluate("document.documentElement.scrollWidth - window.innerWidth")
        assert overflow <= 0, path
    watch.context.close()
