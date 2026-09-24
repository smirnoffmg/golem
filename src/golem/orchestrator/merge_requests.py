"""Merge requests for succeeded runs, through the GitLab REST API (v4).

The runtime pushes a run's result to branch ``golem/<target_id>/<run_id>`` of the agent's
context repository; the orchestrator does not know the target id, so it finds the branch by
the run id suffix, then opens the merge request idempotently per source branch.
"""

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx

from golem.orchestrator.reconcile import SucceededRun

BRANCH_PREFIX = "golem"


class GitLabError(RuntimeError):
    pass


class ProjectNotConfigured(GitLabError):
    """Configuration, not an outage: retrying would fail the same way every pass."""


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

    async def open(self, agent: str, run_id: str, branch: str, title: str, description: str) -> str:
        existing = await self._get(
            agent, "merge_requests", {"source_branch": branch, "state": "opened"}
        )
        if existing:
            return existing[0]["web_url"]
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
        return created["web_url"]

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
        try:
            response = await self.client.request(method, url, **kwargs)
        except httpx.HTTPError as error:
            raise GitLabError(f"{method} {url}: {error}") from error
        if not response.is_success:
            raise GitLabError(f"{method} {url}: {response.status_code} {response.text[:200]}")
        return response.json()


def idle_detail(run_id: str) -> str:
    return f"Run {run_id} succeeded and proposed no changes."


async def propose_merge_request(gitlab: GitLabMergeRequests, run: SucceededRun) -> str:
    try:
        branch = await gitlab.find_branch(run.agent, run.run_id)
    except ProjectNotConfigured as error:
        return f"Run {run.run_id} succeeded, but {error}; its branch was not proposed."
    if branch is None:
        return idle_detail(run.run_id)
    target = target_of(branch)
    url = await gitlab.open(
        run.agent,
        run.run_id,
        branch,
        title=f"{run.agent}: {target}",
        description=f"Proposed by agent {run.agent} in run {run.run_id} for {target}.",
    )
    return f"Run {run.run_id} succeeded; merge request: {url}"
