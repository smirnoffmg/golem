"""The tool groups a platform MCP server can serve: the system behind it and its tools.

A write group (ADR 0015) is served only to proposal tokens, and each of its tools to one token
scope: a preview reads the live page for a person, an apply writes what a person accepted.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field

from golem.proposal_token import APPLY, PREVIEW


@dataclass(frozen=True)
class Group:
    name: str
    system: str
    tools: tuple[str, ...]
    # The token scope each tool of a write group needs; empty for a read group.
    scopes: Mapping[str, str] = field(default_factory=dict)

    @property
    def writes(self) -> bool:
        return bool(self.scopes)


GROUPS = {
    group.name: group
    for group in (
        Group(name="tracker.read", system="jira", tools=("search_issues", "get_issue")),
        Group(
            name="wiki.read",
            system="confluence",
            tools=("search_pages", "get_page", "get_page_source"),
        ),
        Group(
            name="wiki.write",
            system="confluence",
            tools=("preview_page_edit", "apply_page_edit"),
            scopes={"preview_page_edit": PREVIEW, "apply_page_edit": APPLY},
        ),
        Group(
            name="desk.write",
            system="jira",
            tools=("apply_reply",),
            scopes={"apply_reply": APPLY},
        ),
        Group(
            name="tracker.write",
            system="jira",
            tools=("apply_issue", "apply_comment"),
            scopes={"apply_issue": APPLY, "apply_comment": APPLY},
        ),
    )
}
