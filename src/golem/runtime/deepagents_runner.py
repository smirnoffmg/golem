"""A role runner on deepagents: one agent loop over the context repository clone, no shell."""

import json
import math
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from deepagents import FilesystemPermission, SubAgent, create_deep_agent
from deepagents.backends import CompositeBackend, FilesystemBackend, StateBackend
from deepagents.backends.protocol import BackendProtocol
from deepagents.middleware.subagents import GENERAL_PURPOSE_SUBAGENT
from langchain.agents.middleware.model_call_limit import ModelCallLimitExceededError
from langchain.agents.middleware.types import (
    AgentMiddleware,
    ExtendedModelResponse,
    ModelCallResult,
    ModelRequest,
    ModelResponse,
)
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, AnyMessage, BaseMessage, HumanMessage
from langchain_core.tools import BaseTool, StructuredTool
from langchain_openai import ChatOpenAI
from langgraph.errors import GraphBubbleUp
from langgraph.graph.state import CompiledStateGraph
from pydantic import SecretStr

from golem.proposal_payload import APPLIED_KINDS, MERGE_REQUEST, ProposalError, payload_of
from golem.runtime.ports import Brief, RoleResult
from golem.runtime.tools import McpToolbox, ToolAccessError

SKILLS_ROUTE = "/.golem/skills/"
ARTIFACTS_ROOT = "/.golem/artifacts"
# wcmatch's `**` skips dot-segments, so `/**` alone would leave `/.git/...` writable.
EVERYWHERE = ["/**", "/**/.*", "/**/.*/**"]
# A goal run's role says it found something to act on (ADR 0017); without it the run reports.
PROPOSE_TOOL = "submit_proposal"
# Names deepagents may bind itself; `execute` included so no MCP tool can bring a shell in.
BUILTIN_TOOLS = frozenset(
    {"ls", "read_file", "write_file", "edit_file", "delete", "glob", "grep", "execute"}
    | {"write_todos", "task", PROPOSE_TOOL}
)
DEFAULT_MODEL_TIMEOUT_SECONDS = 120.0


@dataclass(frozen=True)
class Limits:
    max_model_calls: int = 60
    recursion_limit: int = 250
    # Far above what a model's output token limit allows; a larger reply is a broken gateway.
    max_response_bytes: int = 1024 * 1024


class ModelResponseError(RuntimeError):
    """The model gateway answered with nothing the run can use."""


@dataclass(frozen=True)
class DeepAgentsRunner:
    model: BaseChatModel
    limits: Limits = Limits()
    callbacks: Sequence[BaseCallbackHandler] = ()
    toolbox: McpToolbox = field(default_factory=McpToolbox)

    async def run(self, brief: Brief) -> RoleResult:
        tools = await self.toolbox.tools_for(brief.role, brief.delegates)
        proposal = Proposal(brief.proposal_kind, brief.workspace, brief.role.writes)
        proposing = brief.goal_mode or brief.proposal_kind in APPLIED_KINDS
        agent = build_agent(self.model, brief, self.limits, tools, proposal if proposing else None)
        state = await agent.ainvoke(
            {"messages": [HumanMessage(task(brief))]},
            config={"recursion_limit": self.limits.recursion_limit, "callbacks": [*self.callbacks]},
        )
        return RoleResult(
            summary=last_ai_text(state["messages"]),
            proposed=proposal.made,
            proposal=proposal.manifest,
        )


@dataclass
class Proposal:
    """What the role submits through `submit_proposal`: that a goal run found something, and for
    a kind the platform applies, the proposal file's content (ADR 0015)."""

    kind: str = MERGE_REQUEST
    workspace: Path | None = None
    writes: str = ""
    made: bool = False
    manifest: dict[str, Any] | None = None

    def tool(self) -> BaseTool:
        if self.kind not in APPLIED_KINDS:
            return self._found_tool()
        return StructuredTool.from_function(
            func=SUBMITTERS[self.kind](self._submit),
            name=PROPOSE_TOOL,
            description=(
                f"Submit this run's {self.kind} proposal, which a person accepts or rejects and"
                " the platform then applies. Write each body to a file under your directory"
                " first and pass its path; the call says what is wrong if it cannot be"
                f" submitted. {KIND_ARGUMENTS[self.kind]}"
            ),
        )

    def _found_tool(self) -> BaseTool:
        def submit_proposal(reason: str) -> str:
            self.made = True
            return "Noted: this run ends with a proposal a person decides on."

        return StructuredTool.from_function(
            func=submit_proposal,
            name=PROPOSE_TOOL,
            description=(
                "Call once when you found something a person should act on: the run then ends"
                " with a proposal. Do not call it when there is nothing to act on; your record is"
                " the report. `reason` says in one line what you found."
            ),
        )

    def _submit(self, fields: Mapping[str, Any]) -> str:
        manifest = {"kind": self.kind} | {
            name: value.lstrip("/") if name.endswith("_file") and isinstance(value, str) else value
            for name, value in fields.items()
            if value is not None
        }
        try:
            payload_of(self.kind, manifest, self._read, under=self.writes)
        except ProposalError as error:
            return f"Not submitted: {error}. Fix it and call {PROPOSE_TOOL} again."
        self.made, self.manifest = True, manifest
        return "Submitted: this run ends with your proposal; a person decides on it."

    def _read(self, path: str) -> str:
        assert self.workspace is not None
        return (self.workspace / path).read_text(encoding="utf-8")


Submit = Callable[[Mapping[str, Any]], str]


def _wiki_edit(submit: Submit) -> Callable[..., str]:
    def submit_proposal(reason: str, page_id: str, title: str, version: int, body_file: str) -> str:
        return submit(
            {"page_id": page_id, "title": title, "version": version, "body_file": body_file}
        )

    return submit_proposal


def _desk_reply(submit: Submit) -> Callable[..., str]:
    def submit_proposal(reason: str, request: str, public: bool, text_file: str) -> str:
        return submit({"request": request, "public": public, "text_file": text_file})

    return submit_proposal


def _tracker_issue(submit: Submit) -> Callable[..., str]:
    def submit_proposal(
        reason: str,
        action: Literal["create", "comment"],
        project: str | None = None,
        issue_type: str | None = None,
        summary: str | None = None,
        description_file: str | None = None,
        issue: str | None = None,
        comment_file: str | None = None,
    ) -> str:
        return submit(
            {
                "action": action,
                "project": project,
                "issue_type": issue_type,
                "summary": summary,
                "description_file": description_file,
                "issue": issue,
                "comment_file": comment_file,
            }
        )

    return submit_proposal


SUBMITTERS: dict[str, Callable[[Submit], Callable[..., str]]] = {
    "wiki_edit": _wiki_edit,
    "desk_reply": _desk_reply,
    "tracker_issue": _tracker_issue,
}
KIND_ARGUMENTS = {
    "wiki_edit": (
        "Read the page with `get_page_source` and propose it back whole: `page_id`, the"
        " `version` you read, the `title`, and `body_file` holding the new storage-format body."
    ),
    "desk_reply": (
        "`request` is the request key (SD-12), `text_file` holds the reply, `public` is true when"
        " the customer reads it and false for an internal note."
    ),
    "tracker_issue": (
        "`action` is `create` (with `project`, `issue_type`, one-line `summary` and"
        " `description_file`) or `comment` (with `issue` and `comment_file`). Search the tracker"
        " first and comment on an open issue rather than create a duplicate."
    ),
}


def gateway_model(settings: Mapping[str, str]) -> ChatOpenAI:
    return ChatOpenAI(
        base_url=settings["GOLEM_MODEL_GATEWAY_URL"],
        api_key=SecretStr(settings["GOLEM_MODEL_KEY"]),
        model=settings["GOLEM_MODEL"],
        # Gateway aliases can look like Responses-only model names; the gateway speaks Chat.
        use_responses_api=False,
        # Per attempt; the client retries a timeout twice. ChatOpenAI's own default (None)
        # waits for a silent gateway until the Job's deadline.
        timeout=model_timeout(settings),
    )


def model_timeout(settings: Mapping[str, str]) -> float:
    raw = settings.get("GOLEM_MODEL_TIMEOUT_SECONDS", "").strip()
    if not raw:
        return DEFAULT_MODEL_TIMEOUT_SECONDS
    try:
        seconds = float(raw)
    except ValueError:
        seconds = math.nan
    if not (math.isfinite(seconds) and seconds > 0):
        raise ValueError(f"GOLEM_MODEL_TIMEOUT_SECONDS must be a positive number: {raw!r}")
    return seconds


def build_agent(
    model: BaseChatModel,
    brief: Brief,
    limits: Limits,
    tools: Sequence[BaseTool] = (),
    proposal: Proposal | None = None,
) -> CompiledStateGraph:
    skills = [SKILLS_ROUTE] if has_skills(brief) else None
    budget = CallBudget(limits.max_model_calls)
    own = [proposal.tool()] if proposal is not None else []
    return create_deep_agent(
        model=model,
        tools=[*unshadowed(tools), *own],
        system_prompt=f"{brief.instructions}\n\n{preamble(brief)}",
        backend=workspace_backend(brief.workspace, brief.skills_dir if skills else None),
        permissions=role_permissions(brief.role.writes),
        skills=skills,
        middleware=[budget, ModelResponseGuard(limits.max_response_bytes)],
        subagents=[general_purpose(limits, skills, budget)],
        name=brief.role.name,
    )


def unshadowed(tools: Sequence[BaseTool]) -> list[BaseTool]:
    clashing = sorted(tool.name for tool in tools if tool.name in BUILTIN_TOOLS)
    if clashing:
        raise ToolAccessError(f"MCP tools {clashing} would shadow built-in tools of the runner")
    return list(tools)


class CallBudget(AgentMiddleware):
    """One count of model calls for a run: the lead's agent and every subagent it starts share
    it. A limit per agent would give each subagent a fresh allowance, so a lead could multiply
    its calls by delegating."""

    def __init__(self, limit: int) -> None:
        super().__init__()
        self.limit = limit
        self.used = 0

    def _spend(self) -> None:
        if self.used >= self.limit:
            raise ModelCallLimitExceededError(
                thread_count=self.used, run_count=self.used, thread_limit=None, run_limit=self.limit
            )
        self.used += 1

    def wrap_model_call(
        self, request: ModelRequest, handler: Callable[[ModelRequest], ModelResponse]
    ) -> ModelCallResult:
        self._spend()
        return handler(request)

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelCallResult:
        self._spend()
        return await handler(request)


class ModelResponseGuard(AgentMiddleware):
    """Turns a gateway's broken answer into one error that names it, before any tool runs.

    Without it a body that is not a chat completion surfaces as whatever the client library
    tripped over (a decode error, an attribute error), and an oversized reply is acted on:
    megabytes written into the clone and pushed.
    """

    def __init__(self, max_response_bytes: int) -> None:
        super().__init__()
        self.max_response_bytes = max_response_bytes

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelCallResult:
        try:
            response = await handler(request)
        except GraphBubbleUp:
            raise
        except Exception as error:
            raise ModelResponseError(
                f"no usable model response: {type(error).__name__}: {error}"
            ) from error
        size = response_bytes(response)
        if size > self.max_response_bytes:
            raise ModelResponseError(
                f"model response of {size} bytes exceeds {self.max_response_bytes} bytes"
            )
        return response


def response_bytes(response: ModelCallResult) -> int:
    if isinstance(response, ExtendedModelResponse):
        response = response.model_response
    messages: Sequence[BaseMessage] = (
        [response] if isinstance(response, AIMessage) else response.result
    )
    return sum(message_bytes(message) for message in messages)


def message_bytes(message: BaseMessage) -> int:
    calls = (getattr(message, "tool_calls", []), getattr(message, "invalid_tool_calls", []))
    return len(json.dumps([message.content, *calls], default=str).encode())


def general_purpose(limits: Limits, skills: list[str] | None, budget: CallBudget) -> SubAgent:
    # The auto-added subagent takes neither the caller's middleware nor the invoke-time
    # recursion_limit, so without the run's budget it could loop unbounded.
    spec: SubAgent = {
        **GENERAL_PURPOSE_SUBAGENT,
        "middleware": [budget, ModelResponseGuard(limits.max_response_bytes)],
    }
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
    outcome = (goal_rules(brief) if brief.goal_mode else "") + proposal_rules(brief)
    return f"""# Golem run {brief.run_id}

Goal: {brief.goal}

Target: {target.kind} `{target.id}` at `{virtual_path(brief.workspace, brief.target_path)}`, \
status `{target.status}`. Empty sections: {sections}.

Rules:
- Fill the empty sections of the target; do not change any `status`.
- Write only under `{writes_root(brief.role.writes)}/`; every other path is read-only.
- There is no shell; work through the file tools.
{outcome}
## Target

{record_block(target.id, brief.target_text)}

## Linked records

{linked}
"""


def goal_rules(brief: Brief) -> str:
    open_ones = ", ".join(f"`{branch}`" for branch in brief.open_proposals) or "none"
    return f"""- Record what you found in the target either way: it is the report of this run.
- Call `{PROPOSE_TOOL}` only if a person should act on it; otherwise the run just reports.
- Proposals still open on this target: {open_ones}. Extend what they propose, do not repeat it.
"""


def proposal_rules(brief: Brief) -> str:
    if brief.proposal_kind not in APPLIED_KINDS:
        return ""
    when = "If you call" if brief.goal_mode else "End the run by calling"
    return f"""- {when} `{PROPOSE_TOOL}`, it is a `{brief.proposal_kind}` proposal: \
{KIND_ARGUMENTS[brief.proposal_kind]} A person reads it before anything is applied.
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
