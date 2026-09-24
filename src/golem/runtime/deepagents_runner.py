"""A role runner on deepagents: one agent loop over the context repository clone, no shell."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from deepagents import FilesystemPermission, SubAgent, create_deep_agent
from deepagents.backends import CompositeBackend, FilesystemBackend, StateBackend
from deepagents.backends.protocol import BackendProtocol
from deepagents.middleware.subagents import GENERAL_PURPOSE_SUBAGENT
from langchain.agents.middleware import ModelCallLimitMiddleware
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage
from langchain_core.tools import BaseTool
from langchain_openai import ChatOpenAI
from langgraph.graph.state import CompiledStateGraph

from golem.runtime.ports import Brief, RoleResult
from golem.runtime.tools import McpToolbox, ToolAccessError

SKILLS_ROUTE = "/.golem/skills/"
ARTIFACTS_ROOT = "/.golem/artifacts"
# wcmatch's `**` skips dot-segments, so `/**` alone would leave `/.git/...` writable.
EVERYWHERE = ["/**", "/**/.*", "/**/.*/**"]
# Names deepagents may bind itself; `execute` included so no MCP tool can bring a shell in.
BUILTIN_TOOLS = frozenset(
    {"ls", "read_file", "write_file", "edit_file", "delete", "glob", "grep", "execute"}
    | {"write_todos", "task"}
)


@dataclass(frozen=True)
class Limits:
    max_model_calls: int = 60
    recursion_limit: int = 250


@dataclass(frozen=True)
class DeepAgentsRunner:
    model: BaseChatModel
    limits: Limits = Limits()
    callbacks: Sequence[BaseCallbackHandler] = ()
    toolbox: McpToolbox = field(default_factory=McpToolbox)

    async def run(self, brief: Brief) -> RoleResult:
        tools = await self.toolbox.tools_for(brief.role)
        agent = build_agent(self.model, brief, self.limits, tools)
        state = await agent.ainvoke(
            {"messages": [HumanMessage(task(brief))]},
            config={"recursion_limit": self.limits.recursion_limit, "callbacks": [*self.callbacks]},
        )
        return RoleResult(summary=last_ai_text(state["messages"]))


def gateway_model(settings: Mapping[str, str]) -> ChatOpenAI:
    return ChatOpenAI(
        base_url=settings["GOLEM_MODEL_GATEWAY_URL"],
        api_key=settings["GOLEM_MODEL_KEY"],
        model=settings["GOLEM_MODEL"],
        # Gateway aliases can look like Responses-only model names; the gateway speaks Chat.
        use_responses_api=False,
    )


def build_agent(
    model: BaseChatModel, brief: Brief, limits: Limits, tools: Sequence[BaseTool] = ()
) -> CompiledStateGraph:
    skills = [SKILLS_ROUTE] if has_skills(brief) else None
    return create_deep_agent(
        model=model,
        tools=unshadowed(tools),
        system_prompt=f"{brief.instructions}\n\n{preamble(brief)}",
        backend=workspace_backend(brief.workspace, brief.skills_dir if skills else None),
        permissions=role_permissions(brief.role.writes),
        skills=skills,
        middleware=[call_limit(limits)],
        subagents=[general_purpose(limits, skills)],
        name=brief.role.name,
    )


def unshadowed(tools: Sequence[BaseTool]) -> list[BaseTool]:
    clashing = sorted(tool.name for tool in tools if tool.name in BUILTIN_TOOLS)
    if clashing:
        raise ToolAccessError(f"MCP tools {clashing} would shadow built-in tools of the runner")
    return list(tools)


def call_limit(limits: Limits) -> ModelCallLimitMiddleware:
    return ModelCallLimitMiddleware(run_limit=limits.max_model_calls, exit_behavior="error")


def general_purpose(limits: Limits, skills: list[str] | None) -> SubAgent:
    # The auto-added subagent takes neither the caller's middleware nor the invoke-time
    # recursion_limit, so without its own call limit it could loop unbounded.
    spec: SubAgent = {**GENERAL_PURPOSE_SUBAGENT, "middleware": [call_limit(limits)]}
    if skills:
        spec["skills"] = skills
    return spec


def has_skills(brief: Brief) -> bool:
    return brief.skills_dir is not None and brief.skills_dir.is_dir()


def workspace_backend(workspace: Path, skills_dir: Path | None) -> BackendProtocol:
    # FilesystemBackend is not a SandboxBackendProtocol, so no `execute` tool reaches the model;
    # deepagents enforces path permissions only for backends that cannot run commands.
    # Middleware offloads (large tool results, summarized history) go to agent state, not
    # into the clone that becomes the result branch.
    routes: dict[str, BackendProtocol] = {f"{ARTIFACTS_ROOT}/": StateBackend()}
    if skills_dir is not None:
        routes[SKILLS_ROUTE] = FilesystemBackend(root_dir=skills_dir, virtual_mode=True)
    return CompositeBackend(
        default=FilesystemBackend(root_dir=workspace, virtual_mode=True),
        routes=routes,
        artifacts_root=ARTIFACTS_ROOT,
    )


def role_permissions(writes: str) -> list[FilesystemPermission]:
    root = writes_root(writes)
    return [
        FilesystemPermission(operations=["write"], paths=[f"{root}/**"], mode="allow"),
        FilesystemPermission(operations=["write"], paths=EVERYWHERE, mode="deny"),
    ]


def writes_root(writes: str) -> str:
    return "/" + writes.strip("/")


def preamble(brief: Brief) -> str:
    target = brief.target
    sections = ", ".join(sorted(target.empty_sections)) or "none"
    linked = "\n\n".join(record_block(r.id, r.text) for r in brief.linked) or "None."
    return f"""# Golem run {brief.run_id}

Goal: {brief.goal}

Target: {target.kind} `{target.id}` at `{virtual_path(brief.workspace, brief.target_path)}`, \
status `{target.status}`. Empty sections: {sections}.

Rules:
- Fill the empty sections of the target; do not change any `status`.
- Write only under `{writes_root(brief.role.writes)}/`; every other path is read-only.
- There is no shell; work through the file tools.

## Target

{record_block(target.id, brief.target_text)}

## Linked records

{linked}
"""


def record_block(record_id: str, text: str) -> str:
    return f'<record id="{record_id}">\n{text.strip()}\n</record>'


def virtual_path(workspace: Path, path: Path) -> str:
    relative = path.relative_to(workspace) if path.is_absolute() else path
    return "/" + relative.as_posix()


def task(brief: Brief) -> str:
    return f"Fill the empty sections of {brief.target.id}. Goal: {brief.goal}"


def last_ai_text(messages: Sequence[AnyMessage]) -> str:
    return next((m.text for m in reversed(messages) if isinstance(m, AIMessage)), "")
