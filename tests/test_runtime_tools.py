import asyncio
import re
import socket
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field, replace
from pathlib import Path

import pytest
import uvicorn
from langchain_core.tools import BaseTool
from mcp.server.fastmcp import Context, FastMCP
from mcp.types import CallToolResult, ImageContent, TextContent
from test_deepagents_runner import ScriptedModel, make_brief, scripted, tool_call

from golem.catalog import Role, load_catalog
from golem.runtime.deepagents_runner import BUILTIN_TOOLS, DeepAgentsRunner
from golem.runtime.ports import Brief
from golem.runtime.tools import (
    McpToolbox,
    Registry,
    RegistryError,
    ToolAccessError,
    ToolGroup,
    ToolLimits,
    ToolLoadError,
    allowed_groups,
    load_registry,
    parse_registry,
    toolbox_from_env,
    truncate_result,
)

TOKEN = "run-token-s3cr3t"
BIG = 90_000


@dataclass
class Tracker:
    url: str
    authorizations: list[str] = field(default_factory=list)


def tracker_server(seen: list[str]) -> FastMCP:
    server = FastMCP("tracker", stateless_http=True, json_response=True, log_level="WARNING")

    def remember(ctx: Context) -> None:
        request = ctx.request_context.request
        seen.append(request.headers.get("authorization", "") if request else "")

    @server.tool()
    async def search_issues(query: str, ctx: Context) -> str:
        """Find issues by text."""
        remember(ctx)
        if query == "slow":
            await asyncio.sleep(5)
        return f"PROJ-1 matches {query}"

    @server.tool()
    def get_issue(key: str, ctx: Context) -> str:
        """Read one issue."""
        remember(ctx)
        if key == "BIG":
            return "b" * BIG
        return f"{key}: onboarding takes two weeks"

    @server.tool()
    def delete_issue(key: str) -> str:
        """Delete an issue."""
        return f"deleted {key}"

    @server.tool()
    def ls(path: str) -> str:
        """Shadows a built-in tool name."""
        return path

    return server


def free_socket() -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    return sock


@pytest.fixture(scope="module")
def tracker() -> Iterator[Tracker]:
    seen: list[str] = []
    sock = free_socket()
    port = sock.getsockname()[1]
    app = tracker_server(seen).streamable_http_app()
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning", lifespan="on"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("MCP test server did not start")
        time.sleep(0.02)
    yield Tracker(url=f"http://127.0.0.1:{port}/mcp", authorizations=seen)
    server.should_exit = True
    thread.join(timeout=10)


def down_url() -> str:
    with free_socket() as sock:
        port = sock.getsockname()[1]
    return f"http://127.0.0.1:{port}/mcp"


def registry_for(url: str, *groups: tuple[str, tuple[str, ...]]) -> Registry:
    return Registry(groups=tuple(ToolGroup(name=n, url=url, tools=t) for n, t in groups))


def tracker_registry(url: str) -> Registry:
    return registry_for(url, ("tracker.read", ("search_issues", "get_issue")))


def researcher(*tools: str) -> Role:
    return Role(name="researcher", writes="hypotheses/", tools=tools)


def names(tools: list[BaseTool]) -> list[str]:
    return sorted(tool.name for tool in tools)


# Registry parsing


REGISTRY_YAML = """\
tracker.read:
  url: http://mcp-tracker.golem.svc/mcp
  tools: [search_issues, get_issue]
wiki.read:
  url: https://mcp-wiki.golem.svc/mcp
  tools: [search_pages]
"""


def test_parse_registry_reads_groups_in_order(tmp_path: Path) -> None:
    path = tmp_path / "mcp-registry.yaml"
    path.write_text(REGISTRY_YAML)

    registry = load_registry(path)

    assert registry == Registry(
        groups=(
            ToolGroup(
                name="tracker.read",
                url="http://mcp-tracker.golem.svc/mcp",
                tools=("search_issues", "get_issue"),
            ),
            ToolGroup(
                name="wiki.read", url="https://mcp-wiki.golem.svc/mcp", tools=("search_pages",)
            ),
        )
    )


def test_empty_registry_has_no_groups() -> None:
    assert parse_registry(None) == Registry()


@pytest.mark.parametrize(
    ("data", "message"),
    [
        (["tracker.read"], "must be a mapping of tool group to server"),
        ({"Tracker": {"url": "http://x/mcp", "tools": ["a"]}}, "group name 'Tracker'"),
        ({"tracker.read": "http://x/mcp"}, "'tracker.read' must be a mapping"),
        ({"tracker.read": {"tools": ["a"]}}, "'tracker.read' has no url"),
        ({"tracker.read": {"url": "ftp://x", "tools": ["a"]}}, "'tracker.read': url 'ftp://x'"),
        ({"tracker.read": {"url": "http://x/mcp"}}, "'tracker.read' lists no tools"),
        ({"tracker.read": {"url": "http://x/mcp", "tools": []}}, "'tracker.read' lists no tools"),
        ({"tracker.read": {"url": "http://x/mcp", "tools": "a"}}, "'tracker.read' lists no tools"),
        ({"tracker.read": {"url": "http://x/mcp", "tools": ["a", ""]}}, "tool name ''"),
        ({"tracker.read": {"url": "http://x/mcp", "tools": ["a", 1]}}, "tool name 1"),
        ({"tracker.read": {"url": "http://x/mcp", "tools": ["a", "a"]}}, "lists 'a' twice"),
        (
            {"tracker.read": {"url": "http://x/mcp", "tools": ["a"], "token": "t"}},
            "'tracker.read' has unknown keys ['token']",
        ),
    ],
)
def test_parse_registry_rejects_malformed_entries(data: object, message: str) -> None:
    with pytest.raises(RegistryError, match=re.escape(message)):
        parse_registry(data)


def test_load_registry_names_the_file(tmp_path: Path) -> None:
    path = tmp_path / "mcp-registry.yaml"
    path.write_text("- not a mapping\n")

    with pytest.raises(RegistryError, match=r"mcp-registry\.yaml"):
        load_registry(path)


EXAMPLES = Path(__file__).parent.parent / "examples"


def test_example_registry_serves_every_role_of_the_example_catalog() -> None:
    registry = load_registry(EXAMPLES / "mcp-registry.yaml")

    for role in load_catalog(EXAMPLES / "discovery" / "agent.yaml").roles:
        assert allowed_groups(registry, role)


# A role's tool groups


def test_role_gets_exactly_the_groups_it_names() -> None:
    registry = parse_registry(
        {
            "tracker.read": {"url": "http://t/mcp", "tools": ["get_issue"]},
            "wiki.read": {"url": "http://w/mcp", "tools": ["search_pages"]},
        }
    )

    assert [g.name for g in allowed_groups(registry, researcher("wiki.read"))] == ["wiki.read"]
    assert allowed_groups(registry, researcher()) == ()


def test_group_missing_from_the_registry_fails_closed() -> None:
    registry = parse_registry({"wiki.read": {"url": "http://w/mcp", "tools": ["search_pages"]}})

    with pytest.raises(ToolAccessError, match=r"researcher.*\['tracker.read'\].*not in the MCP"):
        allowed_groups(registry, researcher("tracker.read", "wiki.read"))


def test_same_tool_name_in_two_groups_of_a_role_is_an_error() -> None:
    registry = parse_registry(
        {
            "tracker.read": {"url": "http://t/mcp", "tools": ["get_issue", "search"]},
            "wiki.read": {"url": "http://w/mcp", "tools": ["search"]},
        }
    )

    with pytest.raises(ToolAccessError, match=r"'search'.*'tracker.read'.*'wiki.read'"):
        allowed_groups(registry, researcher("tracker.read", "wiki.read"))


def test_same_tool_name_in_groups_a_role_does_not_combine_is_fine() -> None:
    registry = parse_registry(
        {
            "tracker.read": {"url": "http://t/mcp", "tools": ["search"]},
            "wiki.read": {"url": "http://w/mcp", "tools": ["search"]},
        }
    )

    assert len(allowed_groups(registry, researcher("wiki.read"))) == 1


def test_toolbox_from_env_reads_the_mounted_registry_and_run_token(tmp_path: Path) -> None:
    path = tmp_path / "mcp-registry.yaml"
    path.write_text(REGISTRY_YAML)

    toolbox = toolbox_from_env({"GOLEM_MCP_REGISTRY": str(path), "GOLEM_RUN_TOKEN": TOKEN})

    assert toolbox.registry == load_registry(path)
    assert toolbox.run_token == TOKEN
    assert TOKEN not in repr(toolbox)


def test_toolbox_from_env_without_a_registry_grants_no_groups() -> None:
    toolbox = toolbox_from_env({})

    assert toolbox.registry == Registry()
    assert toolbox.run_token is None


# Loading from a real MCP server


async def test_role_sees_only_the_tools_the_registry_allows(tracker: Tracker) -> None:
    toolbox = McpToolbox(registry=tracker_registry(tracker.url), run_token=TOKEN)

    tools = await toolbox.tools_for(researcher("tracker.read"))

    assert names(tools) == ["get_issue", "search_issues"]


async def test_role_without_tool_groups_connects_nowhere() -> None:
    toolbox = McpToolbox(registry=tracker_registry(down_url()), run_token=None)

    assert await toolbox.tools_for(researcher()) == []


async def test_run_token_reaches_the_server_as_a_bearer_token(tracker: Tracker) -> None:
    toolbox = McpToolbox(registry=tracker_registry(tracker.url), run_token=TOKEN)
    tools = {tool.name: tool for tool in await toolbox.tools_for(researcher("tracker.read"))}
    before = len(tracker.authorizations)

    await tools["get_issue"].ainvoke({"key": "PROJ-1"})

    assert tracker.authorizations[before:] == [f"Bearer {TOKEN}"]


async def test_tool_groups_without_a_run_token_fail_closed(tracker: Tracker) -> None:
    toolbox = McpToolbox(registry=tracker_registry(tracker.url), run_token=None)

    with pytest.raises(ToolLoadError, match="GOLEM_RUN_TOKEN"):
        await toolbox.tools_for(researcher("tracker.read"))


async def test_server_that_is_down_fails_closed_with_the_group_and_url() -> None:
    url = down_url()
    toolbox = McpToolbox(registry=tracker_registry(url), run_token=TOKEN)

    with pytest.raises(ToolLoadError) as caught:
        await toolbox.tools_for(researcher("tracker.read"))

    assert "tracker.read" in str(caught.value)
    assert url in str(caught.value)
    assert TOKEN not in str(caught.value)


async def test_allowed_tool_missing_on_the_server_fails_closed(tracker: Tracker) -> None:
    registry = registry_for(tracker.url, ("tracker.read", ("get_issue", "link_issues")))
    toolbox = McpToolbox(registry=registry, run_token=TOKEN)

    with pytest.raises(ToolLoadError, match=r"tracker.read.*\['link_issues'\]"):
        await toolbox.tools_for(researcher("tracker.read"))


async def test_slow_call_times_out_as_a_tool_error(tracker: Tracker) -> None:
    toolbox = McpToolbox(
        registry=tracker_registry(tracker.url),
        run_token=TOKEN,
        limits=ToolLimits(call_timeout=0.5),
    )
    tools = {tool.name: tool for tool in await toolbox.tools_for(researcher("tracker.read"))}

    started = time.monotonic()
    reply = await tools["search_issues"].ainvoke(tool_call_dict("search_issues", query="slow"))

    assert time.monotonic() - started < 3
    assert reply.status == "error"
    assert "timed out after 0.5 s" in reply.text


def tool_call_dict(name: str, **args: object) -> dict[str, object]:
    return {"type": "tool_call", "name": name, "args": args, "id": f"call-{name}"}


# Result truncation


def test_result_within_the_cap_is_unchanged() -> None:
    result = CallToolResult(content=[TextContent(type="text", text="short")])

    assert truncate_result(result, 100) is result


def test_long_text_is_cut_with_a_marker_and_structured_content_dropped() -> None:
    result = CallToolResult(
        content=[TextContent(type="text", text="a" * 60), TextContent(type="text", text="b" * 60)],
        structuredContent={"result": "a" * 60},
    )

    clipped = truncate_result(result, 100)

    texts = [block.text for block in clipped.content if isinstance(block, TextContent)]
    assert texts[:2] == ["a" * 60, "b" * 40]
    assert "[truncated: 120 characters, the first 100 shown]" in texts[-1]
    assert clipped.structuredContent is None


def test_non_text_block_over_the_cap_is_dropped() -> None:
    image = ImageContent(type="image", data="x" * 500, mimeType="image/png")
    result = CallToolResult(content=[TextContent(type="text", text="caption"), image])

    clipped = truncate_result(result, 100)

    assert not any(isinstance(block, ImageContent) for block in clipped.content)
    assert clipped.content[0] == TextContent(type="text", text="caption")


# The role runner with MCP tools


DEFAULT_LIMITS = ToolLimits()


def runner_for(
    tracker: Tracker, model: ScriptedModel, limits: ToolLimits = DEFAULT_LIMITS
) -> DeepAgentsRunner:
    toolbox = McpToolbox(registry=tracker_registry(tracker.url), run_token=TOKEN, limits=limits)
    return DeepAgentsRunner(model=model, toolbox=toolbox)


def tracker_brief(workspace: Path) -> Brief:
    return replace(make_brief(workspace), role=researcher("tracker.read"))


async def test_model_calls_an_mcp_tool_and_reads_its_result(
    tracker: Tracker, tmp_path: Path
) -> None:
    model = scripted(tool_call("get_issue", key="PROJ-7"), "Read PROJ-7.")

    result = await runner_for(tracker, model).run(tracker_brief(tmp_path))

    assert result.summary == "Read PROJ-7."
    assert "PROJ-7: onboarding takes two weeks" in model.prompts[1][-1].text


async def test_mcp_tools_add_no_execute_tool(tracker: Tracker, tmp_path: Path) -> None:
    model = scripted("Done.")

    await runner_for(tracker, model).run(tracker_brief(tmp_path))

    assert {"get_issue", "search_issues"} <= set(model.bound_tools)
    assert "delete_issue" not in model.bound_tools
    assert "execute" not in model.bound_tools
    assert not any("shell" in name for name in model.bound_tools)


async def test_builtin_tools_list_covers_what_deepagents_binds(tmp_path: Path) -> None:
    skills = tmp_path / "skills"
    (skills / "search").mkdir(parents=True)
    (skills / "search" / "SKILL.md").write_text("---\nname: search\ndescription: d\n---\n")
    model = scripted("Done.")

    await DeepAgentsRunner(model=model).run(make_brief(tmp_path / "ws", skills_dir=skills))

    assert set(model.bound_tools) <= BUILTIN_TOOLS
    assert "execute" in BUILTIN_TOOLS


async def test_mcp_tool_may_not_shadow_a_builtin_tool(tracker: Tracker, tmp_path: Path) -> None:
    registry = registry_for(tracker.url, ("tracker.read", ("get_issue", "ls")))
    runner = DeepAgentsRunner(
        model=scripted("Done."), toolbox=McpToolbox(registry=registry, run_token=TOKEN)
    )

    with pytest.raises(ToolAccessError, match=r"\['ls'\]"):
        await runner.run(tracker_brief(tmp_path))


async def test_role_naming_an_unregistered_group_fails_before_the_model_runs(
    tmp_path: Path,
) -> None:
    model = scripted("Done.")

    with pytest.raises(ToolAccessError, match=r"tracker\.read"):
        await DeepAgentsRunner(model=model).run(tracker_brief(tmp_path))

    assert model.prompts == []


async def test_large_mcp_result_is_offloaded_outside_the_workspace(
    tracker: Tracker, tmp_path: Path
) -> None:
    brief = tracker_brief(tmp_path)
    before = {p.relative_to(tmp_path) for p in tmp_path.rglob("*")}
    model = scripted(tool_call("get_issue", key="BIG"), "Done.")

    await runner_for(tracker, model).run(brief)

    assert "large_tool_results" in model.prompts[1][-1].text
    assert {p.relative_to(tmp_path) for p in tmp_path.rglob("*")} == before


async def test_result_over_the_cap_reaches_the_model_truncated(
    tracker: Tracker, tmp_path: Path
) -> None:
    model = scripted(tool_call("get_issue", key="BIG"), "Done.")

    await runner_for(tracker, model, ToolLimits(max_result_chars=1_000)).run(
        tracker_brief(tmp_path)
    )

    reply = model.prompts[1][-1].text
    assert "b" * 1_000 in reply
    assert "b" * 1_001 not in reply
    assert f"[truncated: {BIG} characters, the first 1000 shown]" in reply
