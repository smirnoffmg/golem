"""A role's tools from platform MCP servers: the registry, the role's share of it, loading.

The registry belongs to the platform and is mounted into the Job: tool group name to the MCP
server that serves it and the tool names the group allows. A role names tool groups in the
catalog and gets exactly those, deny by default. Every call carries the run token; the servers
hold the secrets to the systems behind them, the Job never does.
"""

import asyncio
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path

import yaml
from langchain_core.tools import BaseTool
from langchain_mcp_adapters.interceptors import MCPToolCallRequest, MCPToolCallResult
from langchain_mcp_adapters.sessions import StreamableHttpConnection
from langchain_mcp_adapters.tools import load_mcp_tools
from mcp.types import CallToolResult, ContentBlock, TextContent

from golem.catalog import Role

GROUP_NAME = re.compile(r"^[a-z][a-z0-9-]*(\.[a-z][a-z0-9-]*)*$")
GROUP_KEYS = frozenset({"url", "tools"})


class RegistryError(ValueError):
    pass


class ToolAccessError(ValueError):
    pass


class ToolLoadError(RuntimeError):
    pass


@dataclass(frozen=True)
class ToolGroup:
    name: str
    url: str
    tools: tuple[str, ...]


@dataclass(frozen=True)
class Registry:
    groups: tuple[ToolGroup, ...] = ()


@dataclass(frozen=True)
class ToolLimits:
    call_timeout: float = 60.0
    # Above deepagents' eviction threshold (80 000 characters), so results between the two are
    # offloaded whole to agent state and only larger ones lose their tail.
    max_result_chars: int = 200_000


def load_registry(path: Path) -> Registry:
    try:
        return parse_registry(yaml.safe_load(path.read_text(encoding="utf-8")))
    except (RegistryError, yaml.YAMLError) as error:
        raise RegistryError(f"MCP registry {path}: {error}") from error


def parse_registry(data: object) -> Registry:
    if data is None:
        return Registry()
    if not isinstance(data, Mapping):
        raise RegistryError("the registry must be a mapping of tool group to server")
    return Registry(groups=tuple(parse_group(name, entry) for name, entry in data.items()))


def parse_group(name: object, entry: object) -> ToolGroup:
    if not isinstance(name, str) or not GROUP_NAME.match(name):
        raise RegistryError(
            f"group name {name!r} must be dotted lowercase words, like 'tracker.read'"
        )
    if not isinstance(entry, Mapping):
        raise RegistryError(f"group {name!r} must be a mapping with url and tools")
    unknown = sorted(set(entry) - GROUP_KEYS)
    if unknown:
        raise RegistryError(f"group {name!r} has unknown keys {unknown}")
    url = parse_url(name, entry.get("url"))
    return ToolGroup(name=name, url=url, tools=parse_tools(name, entry))


def parse_url(group: str, url: object) -> str:
    if url is None:
        raise RegistryError(f"group {group!r} has no url")
    if not isinstance(url, str) or not url.startswith(("http://", "https://")):
        raise RegistryError(f"group {group!r}: url {url!r} must be an http(s) URL")
    return url


def parse_tools(group: str, entry: Mapping[object, object]) -> tuple[str, ...]:
    tools = entry.get("tools")
    if not isinstance(tools, list) or not tools:
        raise RegistryError(f"group {group!r} lists no tools")
    for tool in tools:
        if not isinstance(tool, str) or not tool:
            raise RegistryError(f"group {group!r}: tool name {tool!r} must be a non-empty string")
    repeated = sorted({tool for tool in tools if tools.count(tool) > 1})
    if repeated:
        raise RegistryError(f"group {group!r} lists {repeated[0]!r} twice")
    return tuple(tools)


def allowed_groups(registry: Registry, role: Role) -> tuple[ToolGroup, ...]:
    by_name = {group.name: group for group in registry.groups}
    missing = [name for name in role.tools if name not in by_name]
    if missing:
        raise ToolAccessError(
            f"role {role.name!r} names tool groups {missing} that are not in the MCP registry"
        )
    groups = tuple(by_name[name] for name in dict.fromkeys(role.tools))
    check_unique_tools(groups)
    return groups


def check_unique_tools(groups: Sequence[ToolGroup]) -> None:
    # Tools keep the server's names, which instructions and skills refer to; a prefix would
    # break those references, so two groups of one role must not offer the same name.
    owner: dict[str, str] = {}
    for group in groups:
        for tool in group.tools:
            if tool in owner:
                raise ToolAccessError(
                    f"tool {tool!r} is offered by both group {owner[tool]!r} and group"
                    f" {group.name!r}; a role may take only one of them"
                )
            owner[tool] = group.name


@dataclass(frozen=True)
class McpToolbox:
    registry: Registry = Registry()
    run_token: str | None = field(default=None, repr=False)
    limits: ToolLimits = ToolLimits()

    async def tools_for(self, role: Role) -> list[BaseTool]:
        groups = allowed_groups(self.registry, role)
        if not groups:
            return []
        if not self.run_token:
            raise ToolLoadError(
                f"role {role.name!r} needs MCP tools, but the Job has no GOLEM_RUN_TOKEN"
            )
        loaded = await asyncio.gather(
            *(load_group(group, self.run_token, self.limits) for group in groups)
        )
        return [tool for tools in loaded for tool in tools]


def toolbox_from_env(environ: Mapping[str, str]) -> McpToolbox:
    path = environ.get("GOLEM_MCP_REGISTRY", "").strip()
    return McpToolbox(
        registry=load_registry(Path(path)) if path else Registry(),
        run_token=environ.get("GOLEM_RUN_TOKEN") or None,
    )


async def load_group(group: ToolGroup, token: str, limits: ToolLimits) -> list[BaseTool]:
    try:
        async with asyncio.timeout(limits.call_timeout):
            tools = await load_mcp_tools(
                None,
                connection=connection(group, token, limits),
                server_name=group.name,
                tool_interceptors=[partial(bounded_call, limits)],
            )
    except Exception as error:
        raise ToolLoadError(
            f"tool group {group.name!r}: MCP server {group.url} is unavailable: {describe(error)}"
        ) from error
    return keep_allowed(group, tools)


def connection(group: ToolGroup, token: str, limits: ToolLimits) -> StreamableHttpConnection:
    return {
        "transport": "streamable_http",
        "url": group.url,
        "headers": {"Authorization": f"Bearer {token}"},
        "timeout": limits.call_timeout,
        "sse_read_timeout": limits.call_timeout,
    }


def keep_allowed(group: ToolGroup, tools: Sequence[BaseTool]) -> list[BaseTool]:
    offered = {tool.name for tool in tools}
    absent = [name for name in group.tools if name not in offered]
    if absent:
        raise ToolLoadError(
            f"tool group {group.name!r}: MCP server {group.url} does not offer {absent}"
        )
    return [tool for tool in tools if tool.name in group.tools]


def describe(error: BaseException) -> str:
    if isinstance(error, BaseExceptionGroup):
        return "; ".join(describe(inner) for inner in error.exceptions)
    if isinstance(error, TimeoutError):
        return "timed out"
    return f"{type(error).__name__}: {error}"


async def bounded_call(
    limits: ToolLimits,
    request: MCPToolCallRequest,
    handler: Callable[[MCPToolCallRequest], Awaitable[MCPToolCallResult]],
) -> MCPToolCallResult:
    try:
        async with asyncio.timeout(limits.call_timeout):
            result = await handler(request)
    except TimeoutError:
        return CallToolResult(
            content=[
                TextContent(
                    type="text",
                    text=f"{request.name} timed out after {limits.call_timeout:g} s",
                )
            ],
            isError=True,
        )
    if isinstance(result, CallToolResult):
        return truncate_result(result, limits.max_result_chars)
    return result


def truncate_result(result: CallToolResult, limit: int) -> CallToolResult:
    total = sum(block_size(block) for block in result.content)
    if total <= limit:
        return result
    kept: list[ContentBlock] = []
    room = limit
    for block in result.content:
        size = block_size(block)
        if size <= room:
            kept.append(block)
            room -= size
            continue
        if isinstance(block, TextContent) and room > 0:
            kept.append(block.model_copy(update={"text": block.text[:room]}))
        break
    marker = f"\n[truncated: {total} characters, the first {limit} shown]"
    kept.append(TextContent(type="text", text=marker))
    # The structured copy never reaches the model but would keep the whole result in state.
    return result.model_copy(update={"content": kept, "structuredContent": None})


def block_size(block: ContentBlock) -> int:
    return len(block.text) if isinstance(block, TextContent) else len(block.model_dump_json())
