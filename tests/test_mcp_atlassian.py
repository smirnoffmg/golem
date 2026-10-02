"""Read-only Jira and Confluence calls of the platform MCP servers, against faked REST APIs.

Request and response shapes follow Atlassian's REST documentation:
- Jira Cloud REST API v2, issue search:
  https://developer.atlassian.com/cloud/jira/platform/rest/v2/api-group-issue-search/
  (GET /rest/api/2/search/jql: jql, maxResults, fields; response issues, isLast, nextPageToken)
- Jira Data Center REST API, search:
  https://developer.atlassian.com/server/jira/platform/rest/v10000/api-group-search/
  (GET /rest/api/2/search: jql, maxResults, fields; response startAt, maxResults, total, issues)
- Jira REST API v2, get issue:
  https://developer.atlassian.com/cloud/jira/platform/rest/v2/api-group-issues/
  (GET /rest/api/2/issue/{issueIdOrKey}: fields; errors as errorMessages and errors)
- Confluence REST API v1, search content by CQL:
  https://developer.atlassian.com/cloud/confluence/rest/v1/api-group-content/
  https://docs.atlassian.com/atlassian-confluence/REST/latest-server/
  (GET /rest/api/content/search: cql, limit, expand; response results, size, _links)
"""

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from golem.mcp.atlassian import (
    JiraDeployment,
    UpstreamError,
    get_issue,
    get_page,
    get_page_source,
    search_issues,
    search_pages,
    storage_to_text,
)

JIRA = "https://jira.example.test"
WIKI = "https://wiki.example.test/wiki"

Handler = Callable[[httpx.Request], httpx.Response]


class Upstream:
    def __init__(self, respond: Handler) -> None:
        self.respond = respond
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.respond(request)

    def client(self, base_url: str) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self), base_url=base_url)


def issue(key: str, summary: str, **fields: Any) -> dict[str, Any]:
    return {
        "id": "10001",
        "key": key,
        "self": f"{JIRA}/rest/api/2/issue/10001",
        "fields": {
            "summary": summary,
            "status": {"name": "In Progress"},
            "issuetype": {"name": "Story"},
            "assignee": {"displayName": "Ann Lee"},
            "updated": "2026-09-20T10:00:00.000+0000",
            **fields,
        },
    }


def json_reply(body: Any, status: int = 200) -> Handler:
    return lambda request: httpx.Response(status, json=body)


async def test_search_issues_on_cloud_uses_the_enhanced_search() -> None:
    upstream = Upstream(json_reply({"issues": [issue("DISC-1", "Onboarding")], "isLast": True}))

    async with upstream.client(JIRA) as client:
        text = await search_issues(client, JiraDeployment.CLOUD, "project = DISC", 5)

    [request] = upstream.requests
    assert request.method == "GET"
    assert request.url.path == "/rest/api/2/search/jql"
    assert request.url.params["jql"] == "project = DISC"
    assert request.url.params["maxResults"] == "5"
    assert request.url.params["fields"] == "summary,status,issuetype,assignee,updated"
    assert "DISC-1 [In Progress] Story: Onboarding" in text
    assert "Ann Lee" in text
    assert "more" not in text


async def test_search_issues_on_data_center_uses_the_classic_search() -> None:
    body = {"startAt": 0, "maxResults": 1, "total": 7, "issues": [issue("DISC-2", "Churn")]}
    upstream = Upstream(json_reply(body))

    async with upstream.client(JIRA) as client:
        text = await search_issues(client, JiraDeployment.DATA_CENTER, "project = DISC", 1)

    assert upstream.requests[0].url.path == "/rest/api/2/search"
    assert "DISC-2 [In Progress] Story: Churn" in text
    assert "more issues match" in text


async def test_search_issues_notes_a_next_page_on_cloud() -> None:
    body = {"issues": [issue("DISC-1", "a")], "isLast": False, "nextPageToken": "t"}
    upstream = Upstream(json_reply(body))

    async with upstream.client(JIRA) as client:
        text = await search_issues(client, JiraDeployment.CLOUD, "project = DISC", 1)

    assert "more issues match" in text


async def test_search_issues_with_no_match_says_so() -> None:
    upstream = Upstream(json_reply({"issues": [], "isLast": True}))

    async with upstream.client(JIRA) as client:
        text = await search_issues(client, JiraDeployment.CLOUD, "project = NONE", 5)

    assert text == "No issues match the JQL."


@pytest.mark.parametrize(("asked", "sent"), [(0, "1"), (500, "50"), (20, "20")])
async def test_search_limit_is_kept_between_one_and_fifty(asked: int, sent: str) -> None:
    upstream = Upstream(json_reply({"issues": [], "isLast": True}))

    async with upstream.client(JIRA) as client:
        await search_issues(client, JiraDeployment.CLOUD, "project = DISC", asked)

    assert upstream.requests[0].url.params["maxResults"] == sent


async def test_get_issue_returns_key_fields_and_a_bounded_description() -> None:
    body = issue(
        "DISC-1",
        "Onboarding takes two weeks",
        priority={"name": "High"},
        reporter={"displayName": "Bob Stone"},
        labels=["discovery", "onboarding"],
        created="2026-09-01T09:00:00.000+0000",
        description="h2. Problem\n" + "x" * 20_000,
    )
    upstream = Upstream(json_reply(body))

    async with upstream.client(JIRA) as client:
        text = await get_issue(client, "DISC-1")

    [request] = upstream.requests
    assert request.url.path == "/rest/api/2/issue/DISC-1"
    assert "description" in request.url.params["fields"].split(",")
    assert text.startswith("DISC-1: Onboarding takes two weeks")
    for part in ("Story", "In Progress", "High", "Ann Lee", "Bob Stone", "discovery, onboarding"):
        assert part in text
    assert "h2. Problem" in text
    assert "x" * 20_000 not in text
    assert "[cut: " in text


async def test_get_issue_without_a_description_or_assignee() -> None:
    upstream = Upstream(json_reply(issue("DISC-3", "Empty", assignee=None, description=None)))

    async with upstream.client(JIRA) as client:
        text = await get_issue(client, "DISC-3")

    assert "Assignee: unassigned" in text
    assert "No description." in text


@pytest.mark.parametrize("key", ["../../rest/api/2/myself", "DISC-1?x=1", "", "disc 1"])
async def test_get_issue_refuses_what_is_not_an_issue_key(key: str) -> None:
    upstream = Upstream(json_reply({}))

    async with upstream.client(JIRA) as client:
        with pytest.raises(UpstreamError, match="issue key"):
            await get_issue(client, key)

    assert upstream.requests == []


async def test_a_jira_error_becomes_an_upstream_error_with_its_messages() -> None:
    body = {"errorMessages": ["Issue does not exist or you do not have permission to see it."]}
    upstream = Upstream(json_reply(body, status=404))

    async with upstream.client(JIRA) as client:
        with pytest.raises(UpstreamError, match=r"404.*Issue does not exist"):
            await get_issue(client, "DISC-404")


async def test_a_bad_jql_error_names_the_field_errors() -> None:
    body = {"errorMessages": [], "errors": {"jql": "Field 'bogus' does not exist."}}
    upstream = Upstream(json_reply(body, status=400))

    async with upstream.client(JIRA) as client:
        with pytest.raises(UpstreamError, match=r"400.*bogus"):
            await search_issues(client, JiraDeployment.CLOUD, "bogus = 1", 5)


async def test_an_unreachable_upstream_is_an_upstream_error() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    async with Upstream(refuse).client(JIRA) as client:
        with pytest.raises(UpstreamError, match="unreachable"):
            await get_issue(client, "DISC-1")


async def test_a_non_json_answer_is_an_upstream_error() -> None:
    upstream = Upstream(lambda request: httpx.Response(200, text="<html>login</html>"))

    async with upstream.client(JIRA) as client:
        with pytest.raises(UpstreamError, match="not JSON"):
            await get_issue(client, "DISC-1")


def page(page_id: str, title: str, **extra: Any) -> dict[str, Any]:
    return {
        "id": page_id,
        "type": "page",
        "status": "current",
        "title": title,
        "space": {"key": "DISC", "name": "Discovery"},
        "version": {"number": 7, "when": "2026-09-10T08:00:00.000Z"},
        "_links": {"webui": f"/spaces/DISC/pages/{page_id}"},
        **extra,
    }


def results(*pages: dict[str, Any], more: bool = False) -> dict[str, Any]:
    links: dict[str, str] = {"base": WIKI, "context": "/wiki"}
    if more:
        links["next"] = "/rest/api/content/search?cql=type=page&limit=1&cursor=abc"
    return {"results": list(pages), "start": 0, "limit": 25, "size": len(pages), "_links": links}


async def test_search_pages_uses_cql_content_search() -> None:
    upstream = Upstream(json_reply(results(page("123", "Onboarding research"))))

    async with upstream.client(WIKI) as client:
        text = await search_pages(client, 'space = DISC AND text ~ "onboarding"', 10)

    [request] = upstream.requests
    assert request.url.path == "/wiki/rest/api/content/search"
    assert request.url.params["cql"] == 'space = DISC AND text ~ "onboarding"'
    assert request.url.params["limit"] == "10"
    assert request.url.params["expand"] == "space,version"
    assert "123: Onboarding research (space DISC, version 7" in text


async def test_search_pages_notes_more_results_and_no_results() -> None:
    more = Upstream(json_reply(results(page("1", "a"), more=True)))
    none = Upstream(json_reply(results()))

    async with more.client(WIKI) as client:
        assert "more pages match" in await search_pages(client, "type = page", 1)
    async with none.client(WIKI) as client:
        assert await search_pages(client, "type = nothing", 1) == "No pages match the CQL."


async def test_get_page_returns_the_body_as_bounded_plain_text() -> None:
    storage = (
        "<h1>Findings</h1><p>Onboarding takes <strong>two</strong> weeks.</p>"
        "<ul><li>first</li><li>second</li></ul>"
        '<ac:structured-macro ac:name="code"><ac:plain-text-body><![CDATA[print(1)]]>'
        "</ac:plain-text-body></ac:structured-macro>"
        "<p>" + "y" * 20_000 + "</p>"
    )
    body = page("123", "Onboarding research", body={"storage": {"value": storage}})
    upstream = Upstream(json_reply(results(body)))

    async with upstream.client(WIKI) as client:
        text = await get_page(client, "123")

    [request] = upstream.requests
    assert request.url.path == "/wiki/rest/api/content/search"
    assert request.url.params["cql"] == "id = 123"
    assert request.url.params["expand"] == "body.storage,space,version"
    assert text.startswith("123: Onboarding research")
    assert f"{WIKI}/spaces/DISC/pages/123" in text
    assert "Findings\nOnboarding takes two weeks.\nfirst\nsecond\nprint(1)" in text
    assert "<p>" not in text
    assert "y" * 20_000 not in text
    assert "[cut: " in text


async def test_get_page_that_does_not_exist_is_an_upstream_error() -> None:
    upstream = Upstream(json_reply(results()))

    async with upstream.client(WIKI) as client:
        with pytest.raises(UpstreamError, match="no page with id 999"):
            await get_page(client, "999")


@pytest.mark.parametrize("page_id", ["123 OR type = blogpost", "", "12a"])
async def test_get_page_refuses_what_is_not_a_page_id(page_id: str) -> None:
    upstream = Upstream(json_reply(results()))

    async with upstream.client(WIKI) as client:
        with pytest.raises(UpstreamError, match="page id"):
            await get_page(client, page_id)

    assert upstream.requests == []


def test_storage_to_text_keeps_text_and_block_breaks_only() -> None:
    html = "<p>a &amp; b</p><script>x()</script><table><tr><td>c</td></tr></table>"

    assert storage_to_text(html) == "a & b\nc"


async def test_get_page_source_returns_the_storage_body_whole_with_title_and_version() -> None:
    # A wiki_edit proposes the page back as Confluence stores it (ADR 0015), so nothing is cut.
    storage = "<h1>Findings</h1><p>" + "y" * 20_000 + "</p>"
    body = page("123", "Onboarding research", body={"storage": {"value": storage}})
    upstream = Upstream(json_reply(results(body)))

    async with upstream.client(WIKI) as client:
        text = await get_page_source(client, "123")

    [request] = upstream.requests
    assert request.url.params["cql"] == "id = 123"
    assert request.url.params["expand"] == "body.storage,version"
    assert json.loads(text) == {
        "page_id": "123",
        "title": "Onboarding research",
        "version": 7,
        "body": storage,
    }


async def test_get_page_source_refuses_a_body_over_the_limit_instead_of_cutting_it() -> None:
    body = page("123", "Big", body={"storage": {"value": "x" * 200_001}})
    upstream = Upstream(json_reply(results(body)))

    async with upstream.client(WIKI) as client:
        with pytest.raises(UpstreamError, match="200000"):
            await get_page_source(client, "123")


@pytest.mark.parametrize("page_id", ["123 OR type = blogpost", "", "12a"])
async def test_get_page_source_refuses_what_is_not_a_page_id(page_id: str) -> None:
    upstream = Upstream(json_reply(results()))

    async with upstream.client(WIKI) as client:
        with pytest.raises(UpstreamError, match="page id"):
            await get_page_source(client, page_id)

    assert upstream.requests == []
