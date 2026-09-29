"""The tool groups a platform MCP server can serve: the system behind it and its tools."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Group:
    name: str
    system: str
    tools: tuple[str, ...]


GROUPS = {
    group.name: group
    for group in (
        Group(name="tracker.read", system="jira", tools=("search_issues", "get_issue")),
        Group(
            name="wiki.read",
            system="confluence",
            tools=("search_pages", "get_page", "get_page_source"),
        ),
    )
}
