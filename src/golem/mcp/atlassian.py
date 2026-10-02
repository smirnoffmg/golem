"""Read-only Jira and Confluence calls, answered as compact text for a model.

Jira: REST API v2, the version the Jira adapter uses, served by Jira Cloud and Jira Data Center
under the same path; on v2 an issue's description is a plain string, not a document tree. Only
the search path differs: Cloud removed ``/rest/api/2/search`` in favour of
``/rest/api/2/search/jql``, which Data Center does not have.

Confluence: REST API v1, because CQL search exists only there (v2 has none) and both Cloud and
Data Center serve ``/rest/api/content/search``. A page is read through the same search with
``id = <page id>``: Cloud's v1 no longer documents ``GET /rest/api/content/{id}``, so one
endpoint serves both deployments.

The client passed in carries the server's own credentials; nothing from the caller reaches it.
"""

import json
import re
from collections.abc import Mapping
from enum import StrEnum
from html.parser import HTMLParser
from typing import Any

import httpx

from golem.proposal_payload import MAX_PAGE_BODY

MAX_LIMIT = 50
BODY_CHARS = 8_000
DETAIL_CHARS = 300
SEARCH_FIELDS = "summary,status,issuetype,assignee,updated"
ISSUE_FIELDS = (
    "summary,status,issuetype,priority,assignee,reporter,labels,created,updated,description"
)
ISSUE_KEY = re.compile(r"^[A-Za-z][A-Za-z0-9_]*-[0-9]+$|^[0-9]+$")
PAGE_ID = re.compile(r"^[0-9]+$")
BLOCK_TAGS = frozenset(
    {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "pre", "blockquote"}
    | {"table", "ul", "ol", "ac:plain-text-body", "ac:rich-text-body"}
)
SKIPPED_TAGS = frozenset({"script", "style"})


class Deployment(StrEnum):
    CLOUD = "cloud"
    DATA_CENTER = "data-center"


JiraDeployment = Deployment


JIRA_SEARCH_PATHS = {
    JiraDeployment.CLOUD: "/rest/api/2/search/jql",
    JiraDeployment.DATA_CENTER: "/rest/api/2/search",
}


class UpstreamError(Exception):
    pass


async def search_issues(
    client: httpx.AsyncClient, deployment: JiraDeployment, jql: str, limit: int
) -> str:
    body = await get_json(
        client,
        JIRA_SEARCH_PATHS[deployment],
        {"jql": jql, "maxResults": bounded_limit(limit), "fields": SEARCH_FIELDS},
    )
    issues = [i for i in body.get("issues") or [] if isinstance(i, dict)]
    if not issues:
        return "No issues match the JQL."
    lines = [issue_line(i) for i in issues]
    if more_issues(body, len(issues)):
        lines.append("(more issues match; narrow the JQL or raise the limit)")
    return "\n".join(lines)


def more_issues(body: Mapping[str, Any], returned: int) -> bool:
    if "isLast" in body:
        return body.get("isLast") is False
    total = body.get("total")
    return isinstance(total, int) and total > returned


async def get_issue(client: httpx.AsyncClient, key: str) -> str:
    if not ISSUE_KEY.match(key):
        raise UpstreamError(f"{key!r} is not an issue key such as PROJ-123")
    return issue_text(await get_json(client, f"/rest/api/2/issue/{key}", {"fields": ISSUE_FIELDS}))


def issue_line(issue: Mapping[str, Any]) -> str:
    fields = issue.get("fields") or {}
    return (
        f"{issue.get('key')} [{named(fields, 'status')}] {named(fields, 'issuetype')}:"
        f" {fields.get('summary') or ''} (assignee: {person(fields, 'assignee')};"
        f" updated {fields.get('updated') or 'unknown'})"
    )


def issue_text(issue: Mapping[str, Any]) -> str:
    fields = issue.get("fields") or {}
    labels = ", ".join(str(label) for label in fields.get("labels") or []) or "none"
    description = fields.get("description")
    return "\n".join(
        [
            f"{issue.get('key')}: {fields.get('summary') or ''}",
            f"Type: {named(fields, 'issuetype')} | Status: {named(fields, 'status')}"
            f" | Priority: {named(fields, 'priority')}",
            f"Assignee: {person(fields, 'assignee')} | Reporter: {person(fields, 'reporter')}",
            f"Labels: {labels}",
            f"Created: {fields.get('created') or 'unknown'}"
            f" | Updated: {fields.get('updated') or 'unknown'}",
            "",
            bounded(description, BODY_CHARS)
            if isinstance(description, str) and description.strip()
            else "No description.",
        ]
    )


def named(fields: Mapping[str, Any], name: str) -> str:
    value = fields.get(name)
    return str(value.get("name")) if isinstance(value, dict) and value.get("name") else "none"


def person(fields: Mapping[str, Any], name: str) -> str:
    value = fields.get(name)
    if isinstance(value, dict) and value.get("displayName"):
        return str(value["displayName"])
    return "unassigned" if name == "assignee" else "unknown"


async def search_pages(client: httpx.AsyncClient, cql: str, limit: int) -> str:
    body = await get_json(
        client,
        "/rest/api/content/search",
        {"cql": cql, "limit": bounded_limit(limit), "expand": "space,version"},
    )
    pages = [p for p in body.get("results") or [] if isinstance(p, dict)]
    if not pages:
        return "No pages match the CQL."
    lines = [page_line(p) for p in pages]
    if (body.get("_links") or {}).get("next"):
        lines.append("(more pages match; narrow the CQL or raise the limit)")
    return "\n".join(lines)


async def get_page(client: httpx.AsyncClient, page_id: str) -> str:
    # The id goes into CQL, so anything but digits could widen the query.
    if not PAGE_ID.match(page_id):
        raise UpstreamError(f"{page_id!r} is not a page id (digits only)")
    body = await get_json(
        client,
        "/rest/api/content/search",
        {"cql": f"id = {page_id}", "limit": 1, "expand": "body.storage,space,version"},
    )
    pages = [p for p in body.get("results") or [] if isinstance(p, dict)]
    if not pages:
        raise UpstreamError(f"no page with id {page_id}")
    base = (body.get("_links") or {}).get("base") or ""
    return page_text(pages[0], base)


async def get_page_source(client: httpx.AsyncClient, page_id: str) -> str:
    """The page as Confluence stores it, for a role that proposes it back: never cut, since a
    cut body applied as the whole would delete the rest of the page (ADR 0015)."""
    if not PAGE_ID.match(page_id):
        raise UpstreamError(f"{page_id!r} is not a page id (digits only)")
    body = await get_json(
        client,
        "/rest/api/content/search",
        {"cql": f"id = {page_id}", "limit": 1, "expand": "body.storage,version"},
    )
    pages = [p for p in body.get("results") or [] if isinstance(p, dict)]
    if not pages:
        raise UpstreamError(f"no page with id {page_id}")
    found = pages[0]
    storage = ((found.get("body") or {}).get("storage") or {}).get("value") or ""
    if len(storage) > MAX_PAGE_BODY:
        raise UpstreamError(
            f"page {page_id} holds {len(storage)} characters of storage format;"
            f" at most {MAX_PAGE_BODY} can be proposed back"
        )
    return json.dumps(
        {
            "page_id": page_id,
            "title": found.get("title") or "",
            "version": (found.get("version") or {}).get("number"),
            "body": storage,
        },
        ensure_ascii=False,
    )


def page_line(page: Mapping[str, Any]) -> str:
    space = (page.get("space") or {}).get("key") or "unknown"
    version = page.get("version") or {}
    return (
        f"{page.get('id')}: {page.get('title') or ''} (space {space},"
        f" version {version.get('number', '?')}, {version.get('when') or 'unknown'})"
    )


def page_text(page: Mapping[str, Any], base: str) -> str:
    webui = (page.get("_links") or {}).get("webui") or ""
    storage = ((page.get("body") or {}).get("storage") or {}).get("value") or ""
    text = storage_to_text(storage)
    return "\n".join(
        [
            page_line(page),
            f"URL: {base}{webui}" if webui else "URL: unknown",
            "",
            bounded(text, BODY_CHARS) if text else "The page is empty.",
        ]
    )


class _PlainText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skipping = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in SKIPPED_TAGS:
            self.skipping += 1
        elif tag in BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in SKIPPED_TAGS:
            self.skipping = max(0, self.skipping - 1)
        elif tag in BLOCK_TAGS:
            self.parts.append("\n")
        elif tag in {"td", "th"}:
            self.parts.append(" ")

    def handle_data(self, data: str) -> None:
        if not self.skipping:
            self.parts.append(data)

    def unknown_decl(self, data: str) -> None:
        # Confluence keeps code macro bodies in CDATA sections.
        if data.startswith("CDATA[") and not self.skipping:
            self.parts.append(data.removeprefix("CDATA["))


def storage_to_text(html: str) -> str:
    parser = _PlainText()
    parser.feed(html)
    parser.close()
    lines = (" ".join(line.split()) for line in "".join(parser.parts).splitlines())
    return "\n".join(line for line in lines if line)


def bounded(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return f"{text[:limit]}\n[cut: {len(text)} characters, the first {limit} shown]"


def bounded_limit(limit: int) -> int:
    return max(1, min(MAX_LIMIT, limit))


async def get_json(
    client: httpx.AsyncClient, path: str, params: Mapping[str, str | int]
) -> dict[str, Any]:
    try:
        response = await client.get(path, params=dict(params))
    except httpx.HTTPError as error:
        raise UpstreamError(f"upstream unreachable: {type(error).__name__}") from error
    if response.status_code != 200:
        raise UpstreamError(f"upstream answered {response.status_code}: {error_detail(response)}")
    try:
        body = response.json()
    except ValueError as error:
        raise UpstreamError("upstream answer is not JSON") from error
    if not isinstance(body, dict):
        raise UpstreamError("upstream answer is not a JSON object")
    return body


def error_detail(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return "no details"
    if not isinstance(body, dict):
        return "no details"
    messages = [str(m) for m in body.get("errorMessages") or []]
    errors = body.get("errors")
    if isinstance(errors, dict):
        messages += [f"{field}: {message}" for field, message in errors.items()]
    # Jira Service Management names its one error errorMessage.
    for single in ("message", "errorMessage"):
        if isinstance(body.get(single), str):
            messages.append(body[single])
    return bounded("; ".join(messages) or "no details", DETAIL_CHARS)
