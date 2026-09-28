"""Merge requests for succeeded runs, through the GitLab REST API (v4).

The runtime pushes a run's result to branch ``golem/<target_id>/<run_id>`` of the agent's
context repository; the orchestrator does not know the target id, so it finds the branch by
the run id suffix, then opens the merge request idempotently per source branch. The merge
request is the run's proposal (ADR 0015): a person decides it in GitLab, and the reconciler reads
the decision back from there. A goal run that found nothing to propose has no merge request: its
target record, read at the branch's head commit, is its report, and then the branch goes
(ADR 0017).
"""

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx

from golem.orchestrator.proposals import OpenedMergeRequest, PendingMergeRequest, Transition
from golem.orchestrator.reconcile import Settlement, SucceededRun, idle_detail

BRANCH_PREFIX = "golem"


class GitLabError(RuntimeError):
    pass


class ProjectNotConfigured(GitLabError):
    """Configuration, not an outage: retrying would fail the same way every pass."""


class NotFound(GitLabError):
    pass


@dataclass(frozen=True)
class GitLabProject:
    path: str
    target_branch: str


def run_branch(names: Iterable[str], run_id: str) -> str | None:
    pattern = re.compile(rf"{BRANCH_PREFIX}/[^/]+/{re.escape(run_id)}")
    return min((name for name in names if pattern.fullmatch(name)), default=None)


def target_of(branch: str) -> str:
    return branch.split("/")[1]


@dataclass(frozen=True)
class GitLabMergeRequests:
    """``client`` carries the API base URL (``<gitlab>/api/v4``) and the PRIVATE-TOKEN header."""

    client: httpx.AsyncClient
    project_for_agent: Mapping[str, GitLabProject]

    async def find_branch(self, agent: str, run_id: str) -> str | None:
        # `search` treats a trailing `$` as "ends with"; the exact shape is checked locally.
        branches = await self._get(
            agent, "repository/branches", {"search": f"/{run_id}$", "per_page": "100"}
        )
        return run_branch((branch["name"] for branch in branches), run_id)

    async def open(
        self, agent: str, run_id: str, branch: str, title: str, description: str
    ) -> OpenedMergeRequest:
        existing = await self._get(
            agent, "merge_requests", {"source_branch": branch, "state": "opened"}
        )
        if existing:
            return opened(existing[0], branch)
        project = self._project(agent)
        created = await self._request(
            "POST",
            self._url(project, "merge_requests"),
            json={
                "source_branch": branch,
                "target_branch": project.target_branch,
                "title": title,
                "description": description,
                "remove_source_branch": True,
            },
        )
        return opened(created, branch)

    async def merge_request(self, agent: str, iid: int) -> Any:
        return await self._get(agent, f"merge_requests/{iid}", {})

    async def head_commit(self, agent: str, branch: str) -> str:
        found = await self._get(agent, f"repository/branches/{quote(branch, safe='')}", {})
        return str(found["commit"]["id"])

    async def raw_file(self, agent: str, path: str, commit: str) -> str:
        url = self._url(self._project(agent), f"repository/files/{quote(path, safe='')}/raw")
        response = await self._send("GET", url, params={"ref": commit})
        return response.text

    async def delete_branch(self, agent: str, branch: str) -> None:
        url = self._url(self._project(agent), f"repository/branches/{quote(branch, safe='')}")
        await self._send("DELETE", url)

    def _project(self, agent: str) -> GitLabProject:
        project = self.project_for_agent.get(agent)
        if project is None:
            raise ProjectNotConfigured(f"no GitLab project is configured for agent {agent!r}")
        return project

    def _url(self, project: GitLabProject, resource: str) -> str:
        return f"/projects/{quote(project.path, safe='')}/{resource}"

    async def _get(self, agent: str, resource: str, params: dict[str, str]) -> Any:
        return await self._request("GET", self._url(self._project(agent), resource), params=params)

    async def _request(self, method: str, url: str, **kwargs: Any) -> Any:
        return (await self._send(method, url, **kwargs)).json()

    async def _send(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        try:
            response = await self.client.request(method, url, **kwargs)
        except httpx.HTTPError as error:
            raise GitLabError(f"{method} {url}: {error}") from error
        if not response.is_success:
            error_type = NotFound if response.status_code == 404 else GitLabError
            raise error_type(f"{method} {url}: {response.status_code} {response.text[:200]}")
        return response


def opened(merge_request: Mapping[str, Any], branch: str) -> OpenedMergeRequest:
    return OpenedMergeRequest(
        url=merge_request["web_url"], iid=merge_request["iid"], target=target_of(branch)
    )


def merge_request_transition(merge_request: Mapping[str, Any]) -> Transition | None:
    """Where a merge request's state moves its pending proposal; None while it stays pending.

    ``locked`` is "short-lived and transitional" in GitLab's words, so it waits like ``opened``.
    Deciders are GitLab users, named as such: their GitLab name is not a Golem principal.
    """
    match merge_request.get("state"):
        case "merged":
            return Transition(
                "applied", _user(merge_request.get("merge_user")), merge_request.get("merged_at")
            )
        case "closed":
            return Transition(
                "rejected", _user(merge_request.get("closed_by")), merge_request.get("closed_at")
            )
    return None


def _user(user: Any) -> str | None:
    name = user.get("username") if isinstance(user, dict) else None
    return f"gitlab:{name}" if isinstance(name, str) and name else None


async def check_merge_request(
    gitlab: GitLabMergeRequests, pending: PendingMergeRequest
) -> Transition | None:
    return merge_request_transition(await gitlab.merge_request(pending.agent, pending.iid))


async def has_proposal(gitlab: GitLabMergeRequests, run: SucceededRun) -> bool:
    """Whether the run pushed its branch; raises GitLabError when GitLab cannot say."""
    try:
        return await gitlab.find_branch(run.agent, run.run_id) is not None
    except ProjectNotConfigured:
        return False


async def propose_merge_request(gitlab: GitLabMergeRequests, run: SucceededRun) -> Settlement:
    try:
        branch = await gitlab.find_branch(run.agent, run.run_id)
    except ProjectNotConfigured as error:
        return Settlement(f"Run {run.run_id} succeeded, but {error}; its branch was not proposed.")
    if branch is None:
        return Settlement(idle_detail(run.run_id))
    target = target_of(branch)
    merge_request = await gitlab.open(
        run.agent,
        run.run_id,
        branch,
        title=f"{run.agent}: {target}",
        description=f"Proposed by agent {run.agent} in run {run.run_id} for {target}.",
    )
    return Settlement(
        f"Run {run.run_id} succeeded; merge request: {merge_request.url}", merge_request
    )


async def read_report(gitlab: GitLabMergeRequests, run: SucceededRun) -> str | None:
    """A reported run's record at its branch's head commit, never by the branch name, so the
    report is what the run pushed; None when the branch or the record is gone."""
    if run.record is None:
        return None
    try:
        branch = await gitlab.find_branch(run.agent, run.run_id)
        if branch is None:
            return None
        commit = await gitlab.head_commit(run.agent, branch)
        return await gitlab.raw_file(run.agent, run.record, commit)
    except (NotFound, ProjectNotConfigured):
        return None


async def discard_branch(gitlab: GitLabMergeRequests, run: SucceededRun) -> None:
    try:
        branch = await gitlab.find_branch(run.agent, run.run_id)
        if branch is not None:
            await gitlab.delete_branch(run.agent, branch)
    except (NotFound, ProjectNotConfigured):
        return
