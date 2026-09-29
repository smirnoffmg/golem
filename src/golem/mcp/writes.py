"""The write servers' calls to Confluence, Jira Service Management and Jira (ADR 0015).

Each apply answers ``{"state": "applied" | "stale" | "failed", "detail"}`` and is idempotent per
proposal, so the task service may ask again after a lost answer without writing twice:

- a page edit is marked ``golem:<proposal id>`` in its version message;
- a reply is recognised by its text, visibility and author after the decision, since no marker
  may go into what the customer reads;
- a new issue carries the label ``golem-<first 12 hex of the id>``, a comment ends with the line
  ``Golem proposal <id>``.

Where a server may write is checked here, before any write: the page's space, the request's or
the issue's project. The client passed in carries the server's own write account.

Confluence: Cloud's REST API v2 (``/wiki/api/v2/pages/{id}``, relative to the ``/wiki`` base the
read server uses) and Data Center's ``/rest/api/content/{id}``. Cloud documents 409 on a
version conflict and Data Center none, so after any refused write the page is read again and
the same rules decide.
"""

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import quote

import httpx

from golem.adapters.jira import comment_exists
from golem.mcp.atlassian import JIRA_SEARCH_PATHS, Deployment, UpstreamError, error_detail

MARKER_HEX = 12
COMMENTS_PAGE = 100
# A request with more comments than this is not searched for the reply: failing is safer than
# posting it twice.
MAX_COMMENT_PAGES = 50

Result = dict[str, str]


def _result(state: str, detail: str) -> Result:
    return {"state": state, "detail": detail}


def _allowed(key: str, allowed: frozenset[str], what: str) -> None:
    if key not in allowed:
        raise UpstreamError(f"{what} {key} is not one this server writes to")


def _project_of(issue_key: str) -> str:
    return issue_key.split("-", 1)[0]


async def _send(
    client: httpx.AsyncClient,
    method: str,
    path: str,
    *,
    params: Mapping[str, str | int] | None = None,
    json: Any = None,
) -> httpx.Response:
    try:
        return await client.request(method, path, params=dict(params or {}), json=json)
    except httpx.HTTPError as error:
        raise UpstreamError(f"upstream unreachable: {type(error).__name__}") from error


def _object(response: httpx.Response) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError as error:
        raise UpstreamError("upstream answer is not JSON") from error
    if not isinstance(body, dict):
        raise UpstreamError("upstream answer is not a JSON object")
    return body


async def _get(
    client: httpx.AsyncClient, path: str, params: Mapping[str, str | int] | None = None
) -> dict[str, Any]:
    response = await _send(client, "GET", path, params=params)
    if response.status_code != 200:
        raise UpstreamError(f"upstream answered {response.status_code}: {error_detail(response)}")
    return _object(response)


# Confluence


@dataclass(frozen=True)
class Page:
    title: str
    version: int
    message: str
    body: str
    space: str


async def read_page(client: httpx.AsyncClient, deployment: Deployment, page_id: str) -> Page:
    if deployment is Deployment.CLOUD:
        found = await _get(
            client, f"/api/v2/pages/{quote(page_id, safe='')}", {"body-format": "storage"}
        )
        space_id = str(found.get("spaceId") or "")
        space = await _get(client, f"/api/v2/spaces/{quote(space_id, safe='')}")
        space_key = str(space.get("key") or "")
    else:
        found = await _get(
            client,
            f"/rest/api/content/{quote(page_id, safe='')}",
            {"expand": "body.storage,version,space"},
        )
        space_key = str((found.get("space") or {}).get("key") or "")
    version = found.get("version") or {}
    number = version.get("number")
    if not isinstance(number, int) or not space_key:
        raise UpstreamError(f"page {page_id} came back without a version or a space")
    return Page(
        title=str(found.get("title") or ""),
        version=number,
        message=str(version.get("message") or ""),
        body=str(((found.get("body") or {}).get("storage") or {}).get("value") or ""),
        space=space_key,
    )


def _page_edit(
    deployment: Deployment, payload: Mapping[str, Any], page: Page, message: str
) -> dict[str, Any]:
    version = {"number": payload["version"] + 1, "message": message}
    if deployment is Deployment.CLOUD:
        return {
            "id": payload["page_id"],
            "status": "current",
            "title": payload["title"],
            "body": {"representation": "storage", "value": payload["body"]},
            "version": version,
        }
    return {
        "id": payload["page_id"],
        "type": "page",
        "title": payload["title"],
        "space": {"key": page.space},
        "body": {"storage": {"value": payload["body"], "representation": "storage"}},
        "version": version,
    }


def _page_path(deployment: Deployment, page_id: str) -> str:
    quoted = quote(page_id, safe="")
    if deployment is Deployment.CLOUD:
        return f"/api/v2/pages/{quoted}"
    return f"/rest/api/content/{quoted}"


def _decided(page: Page, page_id: str, marker: str, read_version: int) -> Result | None:
    if marker in page.message.split():
        return _result("applied", f"Page {page_id} is at version {page.version}.")
    if page.version != read_version:
        return _result(
            "stale",
            f"Page {page_id} is at version {page.version};"
            f" the proposal was made on version {read_version}.",
        )
    return None


async def preview_page_edit(
    client: httpx.AsyncClient,
    deployment: Deployment,
    spaces: frozenset[str],
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    page = await read_page(client, deployment, str(payload["page_id"]))
    _allowed(page.space, spaces, "space")
    return {"title": page.title, "version": page.version, "body": page.body}


async def apply_page_edit(
    client: httpx.AsyncClient,
    deployment: Deployment,
    spaces: frozenset[str],
    proposal_id: str,
    payload: Mapping[str, Any],
    decider: str,
) -> Result:
    page_id = str(payload["page_id"])
    marker = f"golem:{proposal_id}"
    page = await read_page(client, deployment, page_id)
    _allowed(page.space, spaces, "space")
    decided = _decided(page, page_id, marker, payload["version"])
    if decided is not None:
        return decided
    response = await _send(
        client,
        "PUT",
        _page_path(deployment, page_id),
        json=_page_edit(deployment, payload, page, f"{marker} accepted by {decider}"),
    )
    if response.status_code == 200:
        return _result("applied", f"Page {page_id} is at version {payload['version'] + 1}.")
    refused = f"Confluence answered {response.status_code}: {error_detail(response)}"
    try:
        again = await read_page(client, deployment, page_id)
    except UpstreamError:
        return _result("failed", refused)
    return _decided(again, page_id, marker, payload["version"]) or _result("failed", refused)


# Jira Service Management


def _milliseconds(iso: str) -> int:
    try:
        at = datetime.fromisoformat(iso)
    except (TypeError, ValueError) as error:
        raise UpstreamError(f"decided_at {iso!r} is not an ISO 8601 time") from error
    if at.tzinfo is None:
        raise UpstreamError(f"decided_at {iso!r} has no time zone")
    return int(at.timestamp() * 1000)


def _is(author: Any, me: Mapping[str, Any]) -> bool:
    # Cloud names an account by accountId; Data Center by key and name.
    if not isinstance(author, dict):
        return False
    return any(me.get(name) and author.get(name) == me.get(name) for name in ("accountId", "key"))


def _is_reply(comment: Any, text: str, public: bool, me: Mapping[str, Any], after: int) -> bool:
    if not isinstance(comment, dict):
        return False
    created = (comment.get("created") or {}).get("epochMillis")
    return (
        str(comment.get("body", "")).rstrip() == text.rstrip()
        and comment.get("public") is public
        and _is(comment.get("author"), me)
        and isinstance(created, int)
        and created >= after
    )


async def _replied(
    client: httpx.AsyncClient, request: str, text: str, public: bool, after: int
) -> bool:
    me = await _get(client, "/rest/api/2/myself")
    path = f"/rest/servicedeskapi/request/{quote(request, safe='')}/comment"
    start = 0
    for _ in range(MAX_COMMENT_PAGES):
        page = await _get(client, path, {"start": start, "limit": COMMENTS_PAGE})
        values = page.get("values") or []
        if any(_is_reply(comment, text, public, me, after) for comment in values):
            return True
        start += len(values)
        if not values or page.get("isLastPage", True):
            return False
    raise UpstreamError(f"{request} has too many comments to check for the reply")


async def apply_reply(
    client: httpx.AsyncClient,
    projects: frozenset[str],
    proposal_id: str,
    payload: Mapping[str, Any],
    decided_at: str,
) -> Result:
    request, text, public = str(payload["request"]), str(payload["text"]), payload["public"]
    _allowed(_project_of(request), projects, "project")
    after = _milliseconds(decided_at)
    kind = "Replied on" if public else "Added an internal note on"
    if await _replied(client, request, text, public, after):
        return _result("applied", f"{kind} {request} already.")
    response = await _send(
        client,
        "POST",
        f"/rest/servicedeskapi/request/{quote(request, safe='')}/comment",
        json={"body": text, "public": public},
    )
    if response.status_code == 201:
        return _result("applied", f"{kind} {request}.")
    refused = f"Jira Service Management answered {response.status_code}: {error_detail(response)}"
    try:
        found = await _replied(client, request, text, public, after)
    except UpstreamError:
        return _result("failed", refused)
    return _result("applied", f"{kind} {request}.") if found else _result("failed", refused)


# Jira


def proposal_label(proposal_id: str) -> str:
    return f"golem-{uuid.UUID(proposal_id).hex[:MARKER_HEX]}"


async def _labelled(client: httpx.AsyncClient, deployment: Deployment, label: str) -> str | None:
    found = await _get(
        client,
        JIRA_SEARCH_PATHS[deployment],
        {"jql": f'labels = "{label}"', "maxResults": 1, "fields": "key"},
    )
    issues = [issue for issue in found.get("issues") or [] if isinstance(issue, dict)]
    return str(issues[0].get("key")) if issues else None


async def apply_issue(
    client: httpx.AsyncClient,
    deployment: Deployment,
    projects: frozenset[str],
    proposal_id: str,
    payload: Mapping[str, Any],
    target: str | None,
) -> Result:
    project = str(payload["project"])
    _allowed(project, projects, "project")
    label = proposal_label(proposal_id)
    existing = await _labelled(client, deployment, label)
    if existing is not None:
        return _result("applied", f"{existing} was created already.")
    # The target comes from the proposal's row, not from the Job's payload (ADR 0015).
    labels = [label, *([f"golem-{target}"] if target else [])]
    response = await _send(
        client,
        "POST",
        "/rest/api/2/issue",
        json={
            "fields": {
                "project": {"key": project},
                "issuetype": {"name": payload["issue_type"]},
                "summary": payload["summary"],
                "description": payload["description"],
                "labels": labels,
            }
        },
    )
    if response.status_code == 201:
        return _result("applied", f"Created {_object(response).get('key')}.")
    refused = f"Jira answered {response.status_code}: {error_detail(response)}"
    try:
        existing = await _labelled(client, deployment, label)
    except UpstreamError:
        return _result("failed", refused)
    return _result("applied", f"Created {existing}.") if existing else _result("failed", refused)


def comment_marker(proposal_id: str) -> str:
    return f"Golem proposal {proposal_id}"


async def _commented(client: httpx.AsyncClient, issue: str, marker: str) -> bool:
    try:
        return await comment_exists(client, issue, marker)
    except httpx.HTTPStatusError as error:
        raise UpstreamError(f"upstream answered {error.response.status_code}") from error
    except (httpx.HTTPError, ValueError) as error:
        raise UpstreamError(f"upstream unreachable: {type(error).__name__}") from error


async def apply_comment(
    client: httpx.AsyncClient,
    projects: frozenset[str],
    proposal_id: str,
    payload: Mapping[str, Any],
) -> Result:
    issue = str(payload["issue"])
    _allowed(_project_of(issue), projects, "project")
    marker = comment_marker(proposal_id)
    if await _commented(client, issue, marker):
        return _result("applied", f"Commented on {issue} already.")
    response = await _send(
        client,
        "POST",
        f"/rest/api/2/issue/{quote(issue, safe='')}/comment",
        json={"body": f"{str(payload['comment']).rstrip()}\n\n{marker}"},
    )
    if response.status_code == 201:
        return _result("applied", f"Commented on {issue}.")
    refused = f"Jira answered {response.status_code}: {error_detail(response)}"
    try:
        found = await _commented(client, issue, marker)
    except UpstreamError:
        return _result("failed", refused)
    return _result("applied", f"Commented on {issue}.") if found else _result("failed", refused)
