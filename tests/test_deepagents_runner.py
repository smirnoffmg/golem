from collections.abc import Iterator, Sequence
from itertools import count, islice
from pathlib import Path
from typing import Any

import pytest
from langchain.agents.middleware.model_call_limit import ModelCallLimitExceededError
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, BaseMessage, SystemMessage
from langchain_core.outputs import ChatResult
from langchain_core.utils.function_calling import convert_to_openai_tool
from langchain_openai import ChatOpenAI
from langgraph.errors import GraphRecursionError
from pydantic import Field

from golem.catalog import Role
from golem.runtime.deepagents_runner import (
    DEFAULT_MODEL_TIMEOUT_SECONDS,
    DeepAgentsRunner,
    Limits,
    ModelResponseError,
    gateway_model,
    preamble,
)
from golem.runtime.lead import Record
from golem.runtime.ports import Brief, LinkedRecord


class ScriptedModel(GenericFakeChatModel):
    """Replays scripted replies and records what the agent bound and sent."""

    bound_tools: list[str] = Field(default_factory=list)
    prompts: list[list[BaseMessage]] = Field(default_factory=list)

    def bind_tools(self, tools: Sequence[Any], **kwargs: Any) -> "ScriptedModel":
        self.bound_tools[:] = [convert_to_openai_tool(t)["function"]["name"] for t in tools]
        return self

    def _generate(self, messages: list[BaseMessage], *args: Any, **kwargs: Any) -> ChatResult:
        self.prompts.append(messages)
        return super()._generate(messages, *args, **kwargs)


def tool_call(name: str, **args: Any) -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": f"call-{name}"}])


def scripted(*replies: AIMessage | str) -> ScriptedModel:
    return ScriptedModel(messages=iter(replies))


def looping() -> Iterator[AIMessage]:
    for n in count():
        yield AIMessage(
            content="",
            tool_calls=[{"name": "ls", "args": {"path": "/"}, "id": f"call-{n}"}],
        )


def make_brief(workspace: Path, skills_dir: Path | None = None) -> Brief:
    (workspace / "hypotheses").mkdir(parents=True, exist_ok=True)
    target_text = "---\nid: H-1\nkind: hypothesis\nstatus: draft\n---\n## Evidence\n"
    (workspace / "hypotheses" / "H-1.md").write_text(target_text)
    return Brief(
        run_id="run-1",
        goal="Find evidence for faster onboarding",
        role=Role(name="researcher", writes="hypotheses/"),
        instructions="You are a careful researcher.",
        target=Record(
            id="H-1",
            kind="hypothesis",
            status="draft",
            links=frozenset({"P-7"}),
            empty_sections=frozenset({"Evidence"}),
        ),
        target_path=Path("hypotheses/H-1.md"),
        target_text=target_text,
        linked=(LinkedRecord(id="P-7", text="Problem P-7: onboarding takes two weeks."),),
        workspace=workspace,
        skills_dir=skills_dir,
    )


def system_text(model: ScriptedModel) -> str:
    first = model.prompts[0]
    return "\n".join(m.text for m in first if isinstance(m, SystemMessage))


def test_preamble_carries_goal_target_rules_and_linked_records(tmp_path: Path) -> None:
    text = preamble(make_brief(tmp_path))

    assert "Find evidence for faster onboarding" in text
    assert "H-1" in text
    assert "hypothesis" in text
    assert "/hypotheses/H-1.md" in text
    assert "## Evidence" in text
    assert "fill the empty sections of the target" in text.lower()
    assert "do not change any `status`" in text.lower()
    assert "/hypotheses/" in text
    assert "Problem P-7: onboarding takes two weeks." in text


async def test_system_prompt_is_role_instructions_then_preamble(tmp_path: Path) -> None:
    brief = make_brief(tmp_path)
    model = scripted("Done.")

    await DeepAgentsRunner(model=model).run(brief)

    prompt = system_text(model)
    assert prompt.startswith(brief.instructions)
    assert preamble(brief) in prompt


async def test_returns_last_ai_message_as_summary(tmp_path: Path) -> None:
    model = scripted("Filled Evidence with two sources.")

    result = await DeepAgentsRunner(model=model).run(make_brief(tmp_path))

    assert result.summary == "Filled Evidence with two sources."


async def test_write_inside_role_writes_succeeds(tmp_path: Path) -> None:
    model = scripted(
        tool_call("write_file", file_path="/hypotheses/H-2.md", content="new hypothesis"),
        "Wrote H-2.",
    )

    await DeepAgentsRunner(model=model).run(make_brief(tmp_path))

    assert (tmp_path / "hypotheses" / "H-2.md").read_text() == "new hypothesis"


@pytest.mark.parametrize("path", ["/problems/P-7.md", "/README.md", "/.git/config", "/.env"])
async def test_write_outside_role_writes_is_denied_and_run_continues(
    tmp_path: Path, path: str
) -> None:
    model = scripted(tool_call("write_file", file_path=path, content="tampered"), "Gave up.")

    result = await DeepAgentsRunner(model=model).run(make_brief(tmp_path))

    assert not (tmp_path / path.lstrip("/")).exists()
    assert result.summary == "Gave up."
    tool_reply = model.prompts[1][-1]
    assert "permission denied" in tool_reply.text


async def test_edit_outside_role_writes_leaves_file_unchanged(tmp_path: Path) -> None:
    (tmp_path / "problems").mkdir()
    problem = tmp_path / "problems" / "P-7.md"
    problem.write_text("status: open\n")
    model = scripted(
        tool_call("read_file", file_path="/problems/P-7.md"),
        tool_call(
            "edit_file", file_path="/problems/P-7.md", old_string="open", new_string="closed"
        ),
        "Tried.",
    )

    await DeepAgentsRunner(model=model).run(make_brief(tmp_path))

    assert problem.read_text() == "status: open\n"


async def test_role_reads_anything_in_the_workspace(tmp_path: Path) -> None:
    (tmp_path / "problems").mkdir()
    (tmp_path / "problems" / "P-7.md").write_text("onboarding takes two weeks")
    model = scripted(tool_call("read_file", file_path="/problems/P-7.md"), "Read it.")

    await DeepAgentsRunner(model=model).run(make_brief(tmp_path))

    assert "onboarding takes two weeks" in model.prompts[1][-1].text


async def test_no_execute_tool_since_path_permissions_hold_only_without_a_shell(
    tmp_path: Path,
) -> None:
    model = scripted("Done.")

    await DeepAgentsRunner(model=model).run(make_brief(tmp_path))

    assert "write_file" in model.bound_tools
    assert "execute" not in model.bound_tools
    assert not any("shell" in name for name in model.bound_tools)


async def test_middleware_offloads_stay_out_of_the_workspace(tmp_path: Path) -> None:
    brief = make_brief(tmp_path)
    before = {p.relative_to(tmp_path) for p in tmp_path.rglob("*")}
    model = scripted(
        tool_call("task", description="collect evidence", subagent_type="general-purpose"),
        "x" * 200_000,
        "Done.",
    )

    await DeepAgentsRunner(model=model).run(brief)

    assert "large_tool_results" in model.prompts[2][-1].text
    assert {p.relative_to(tmp_path) for p in tmp_path.rglob("*")} == before


async def test_skills_from_skills_dir_are_listed_in_the_prompt(tmp_path: Path) -> None:
    skills = tmp_path / "catalog" / "skills"
    (skills / "evidence-search").mkdir(parents=True)
    (skills / "evidence-search" / "SKILL.md").write_text(
        "---\nname: evidence-search\ndescription: How to find evidence in the wiki\n---\n# Steps\n"
    )
    model = scripted("Done.")

    await DeepAgentsRunner(model=model).run(make_brief(tmp_path / "ws", skills_dir=skills))

    prompt = system_text(model)
    assert "evidence-search" in prompt
    assert "How to find evidence in the wiki" in prompt


async def test_model_call_limit_stops_a_model_that_loops_forever(tmp_path: Path) -> None:
    model = ScriptedModel(messages=looping())
    runner = DeepAgentsRunner(model=model, limits=Limits(max_model_calls=3, recursion_limit=100))

    with pytest.raises(ModelCallLimitExceededError):
        await runner.run(make_brief(tmp_path))

    assert len(model.prompts) == 3


async def test_recursion_limit_stops_the_graph(tmp_path: Path) -> None:
    model = ScriptedModel(messages=looping())
    runner = DeepAgentsRunner(model=model, limits=Limits(max_model_calls=1000, recursion_limit=10))

    with pytest.raises(GraphRecursionError):
        await runner.run(make_brief(tmp_path))


class ChatStarts(BaseCallbackHandler):
    def __init__(self) -> None:
        self.count = 0

    def on_chat_model_start(self, *args: Any, **kwargs: Any) -> None:
        self.count += 1


async def test_callbacks_are_passed_through(tmp_path: Path) -> None:
    handler = ChatStarts()

    await DeepAgentsRunner(model=scripted("Done."), callbacks=(handler,)).run(make_brief(tmp_path))

    assert handler.count == 1


def test_gateway_model_points_chat_openai_at_the_gateway() -> None:
    model = gateway_model(
        {
            "GOLEM_MODEL_GATEWAY_URL": "http://gateway.local/v1",
            "GOLEM_MODEL_KEY": "agent-key",
            "GOLEM_MODEL": "team-default",
        }
    )

    assert isinstance(model, ChatOpenAI)
    assert model.openai_api_base == "http://gateway.local/v1"
    assert model.model_name == "team-default"
    assert model.openai_api_key is not None
    assert model.openai_api_key.get_secret_value() == "agent-key"
    assert model.use_responses_api is False


GATEWAY = {
    "GOLEM_MODEL_GATEWAY_URL": "http://gateway.local/v1",
    "GOLEM_MODEL_KEY": "agent-key",
    "GOLEM_MODEL": "team-default",
}


def test_gateway_model_has_a_timeout_by_default() -> None:
    # ChatOpenAI hands the client timeout=None, which waits for a silent gateway forever.
    assert gateway_model(GATEWAY).request_timeout == DEFAULT_MODEL_TIMEOUT_SECONDS


def test_gateway_model_timeout_is_a_setting() -> None:
    model = gateway_model({**GATEWAY, "GOLEM_MODEL_TIMEOUT_SECONDS": "7.5"})

    assert model.request_timeout == 7.5


@pytest.mark.parametrize("value", ["soon", "0", "-1", "nan", "inf"])
def test_gateway_model_refuses_a_timeout_that_is_not_a_positive_number(value: str) -> None:
    with pytest.raises(ValueError, match="GOLEM_MODEL_TIMEOUT_SECONDS"):
        gateway_model({**GATEWAY, "GOLEM_MODEL_TIMEOUT_SECONDS": value})


def test_gateway_model_requires_every_setting() -> None:
    with pytest.raises(KeyError, match="GOLEM_MODEL_KEY"):
        gateway_model({"GOLEM_MODEL_GATEWAY_URL": "http://gateway.local/v1", "GOLEM_MODEL": "m"})


async def test_subagent_inherits_the_role_permissions(tmp_path: Path) -> None:
    model = scripted(
        tool_call("task", description="tidy up", subagent_type="general-purpose"),
        tool_call("write_file", file_path="/README.md", content="tampered"),
        "Subagent gave up.",
        "Done.",
    )

    await DeepAgentsRunner(model=model).run(make_brief(tmp_path))

    assert not (tmp_path / "README.md").exists()
    assert "permission denied" in model.prompts[2][-1].text


def delegating_then_looping() -> Iterator[AIMessage]:
    yield tool_call("task", description="loop", subagent_type="general-purpose")
    yield from looping()


async def test_model_call_limit_also_bounds_a_subagent(tmp_path: Path) -> None:
    model = ScriptedModel(messages=islice(delegating_then_looping(), 100))
    runner = DeepAgentsRunner(model=model, limits=Limits(max_model_calls=3, recursion_limit=1000))

    with pytest.raises(ModelCallLimitExceededError):
        await runner.run(make_brief(tmp_path))

    assert len(model.prompts) == 3


def delegating_again_and_again() -> Iterator[AIMessage]:
    for n in count():
        yield tool_call("task", description=f"step {n}", subagent_type="general-purpose")
        yield AIMessage(content="done")


async def test_the_model_call_limit_is_one_budget_for_the_run_and_all_its_subagents(
    tmp_path: Path,
) -> None:
    # Per-agent limits would let each of the lead's calls start a subagent with a fresh limit.
    model = ScriptedModel(messages=islice(delegating_again_and_again(), 100))
    runner = DeepAgentsRunner(model=model, limits=Limits(max_model_calls=5, recursion_limit=1000))

    with pytest.raises(ModelCallLimitExceededError):
        await runner.run(make_brief(tmp_path))

    assert len(model.prompts) == 5


async def test_each_run_gets_its_own_budget(tmp_path: Path) -> None:
    runner = DeepAgentsRunner(
        model=ScriptedModel(messages=iter(["one", "two"])),
        limits=Limits(max_model_calls=1, recursion_limit=100),
    )

    first = await runner.run(make_brief(tmp_path / "a"))
    second = await runner.run(make_brief(tmp_path / "b"))

    assert (first.summary, second.summary) == ("one", "two")


class Unreachable(GenericFakeChatModel):
    def bind_tools(self, tools: Sequence[Any], **kwargs: Any) -> "Unreachable":
        return self

    def _generate(self, *args: Any, **kwargs: Any) -> ChatResult:
        raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")


async def test_a_failed_model_call_is_reported_as_the_model_response(tmp_path: Path) -> None:
    model = Unreachable(messages=iter(()))

    with pytest.raises(ModelResponseError, match="no usable model response: UnicodeDecodeError"):
        await DeepAgentsRunner(model=model).run(make_brief(tmp_path))


async def test_an_oversized_model_response_is_refused_before_its_tool_call_runs(
    tmp_path: Path,
) -> None:
    brief = make_brief(tmp_path)
    before = (tmp_path / "hypotheses" / "H-1.md").read_text()
    model = scripted(
        tool_call("write_file", file_path="/hypotheses/H-1.md", content="x" * 2_000),
        "Done.",
    )
    runner = DeepAgentsRunner(model=model, limits=Limits(max_response_bytes=1_000))

    with pytest.raises(ModelResponseError, match=r"model response of \d+ bytes exceeds 1000"):
        await runner.run(brief)

    assert (tmp_path / "hypotheses" / "H-1.md").read_text() == before


async def test_the_response_limit_also_holds_in_a_subagent(tmp_path: Path) -> None:
    brief = make_brief(tmp_path)
    before = (tmp_path / "hypotheses" / "H-1.md").read_text()
    model = scripted(
        tool_call("task", description="fill evidence", subagent_type="general-purpose"),
        tool_call("write_file", file_path="/hypotheses/H-1.md", content="x" * 2_000),
        "Subagent done.",
        "Done.",
    )
    runner = DeepAgentsRunner(model=model, limits=Limits(max_response_bytes=1_000))

    with pytest.raises(ModelResponseError):
        await runner.run(brief)

    assert (tmp_path / "hypotheses" / "H-1.md").read_text() == before
