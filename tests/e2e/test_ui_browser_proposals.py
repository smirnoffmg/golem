"""Proposals and reports on the board in a real browser (ADR 0015, ADR 0018): alice reviews three
goal agents whose runs bob started. The reconciler records each run's proposal from its branch,
and accepting one applies it through a real write server to Confluence, Jira Service Management
or Jira, faked at their HTTP boundary. Its own module, so its stack follows the others' rather
than sharing their databases.
"""

import re
from collections.abc import Iterator

import pytest
from playwright.sync_api import Browser, Page, expect
from support.demo import REVIEWED, RUNBOOK_EDIT, Demo, databases_of, running, seed
from test_ui_browser import column, sign_in, watched
from testcontainers.community.postgres import PostgresContainer

pytestmark = pytest.mark.e2e


@pytest.fixture(scope="module")
def demo(postgres: PostgresContainer, board_url: str) -> Iterator[Demo]:
    with running(databases_of(postgres), board_url, proposals=True) as demo:
        yield demo


@pytest.fixture(scope="module")
def paths(demo: Demo) -> dict[str, str]:
    return seed(demo)


def signed_in(browser: Browser, demo: Demo):
    watch = watched(browser, demo)
    page = watch.page()
    sign_in(page, demo, "/agents/docs", agents=len(REVIEWED))
    return watch, page


def outcome(page: Page):
    return page.get_by_role("status")


def test_the_reviewer_sees_what_waits_for_her(
    browser: Browser, demo: Demo, paths: dict[str, str]
) -> None:
    watch, page = signed_in(browser, demo)

    review_link = page.get_by_role("navigation", name="Agents").get_by_role(
        "link", name=re.compile(r"^To review")
    )
    expect(review_link).to_contain_text("4")
    review_link.click()
    cards = page.locator(".proposal-card")
    expect(cards).to_have_count(4)
    expect(cards.filter(has_text="Page edit")).to_have_count(2)
    expect(cards.filter(has_text="Service desk reply")).to_contain_text("Reply to SD-12")
    expect(cards.filter(has_text="Tracker issue")).to_contain_text(
        "New OPS issue: Exporter volume grows 4% a day"
    )
    expect(cards.first).to_contain_text("for bob")
    # On the docs board they are cards of their own in To review: the tasks are bob's.
    page.goto("/agents/docs")
    expect(column(page, "To review").locator(".proposal-card")).to_have_count(2)
    assert watch.console_errors == watch.page_errors == watch.csp_violations == []
    watch.context.close()


def test_accepting_a_page_edit_applies_it_and_the_other_edit_goes_stale(
    browser: Browser, demo: Demo, paths: dict[str, str]
) -> None:
    watch, page = signed_in(browser, demo)
    page.goto(paths["wiki"])

    diff = page.get_by_role("list", name="Changes to the page")
    expect(diff.locator(".diff-delete")).to_have_count(2)
    expect(diff.locator(".diff-insert")).to_have_count(2)
    expect(diff.locator(".diff-insert").first).to_contain_text("kubectl rollout restart")
    # Storage-format markup is shown as text: nothing of the payload becomes an element.
    expect(diff.locator("code").first).to_contain_text("<h1>")
    expect(page.locator(".diff h1, .diff code p")).to_have_count(0)
    page.get_by_role("button", name="Accept").click()

    expect(outcome(page)).to_have_text("Accepted and applied.")
    assert demo.confluence.body == RUNBOOK_EDIT
    assert demo.confluence.version == 8

    # The other edit was made against version 7, which is no longer the page.
    page.goto(paths["wiki-stale"])
    expect(outcome(page)).to_contain_text("Its target changed")
    expect(page.get_by_role("button", name="Accept")).to_have_count(0)
    assert watch.console_errors == watch.page_errors == watch.csp_violations == []
    watch.context.close()


def test_rejecting_a_reply_keeps_it_from_the_customer(
    browser: Browser, demo: Demo, paths: dict[str, str]
) -> None:
    watch, page = signed_in(browser, demo)
    page.goto(paths["reply"])
    expect(page.locator(".preview")).to_contain_text("the customer")
    expect(page.locator(".preview")).to_contain_text("lost its volume")

    page.get_by_label("Reason, if you reject it").fill("Do not tell customers about volumes.")
    page.get_by_role("button", name="Reject").click()

    expect(outcome(page)).to_have_text("Rejected.")
    expect(page.locator(".facts")).to_contain_text("Do not tell customers about volumes.")
    assert demo.desk.comments == []
    assert watch.console_errors == watch.page_errors == watch.csp_violations == []
    watch.context.close()


def test_a_report_is_read_from_the_reports_lane(
    browser: Browser, demo: Demo, paths: dict[str, str]
) -> None:
    watch, page = signed_in(browser, demo)
    page.goto("/agents/triage")
    lane = page.locator("details.reports")

    lane.locator("summary").click()
    report = lane.get_by_role("link", name=re.compile("Error rate of the export API"))
    expect(report).to_have_count(1)
    report.click()

    expect(page.locator(".report-text")).to_contain_text("the nightly restart")
    assert page.url.endswith(paths["report"])
    assert watch.console_errors == watch.page_errors == watch.csp_violations == []
    watch.context.close()
