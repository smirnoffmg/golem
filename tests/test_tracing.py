import asyncio
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from a2a.types.a2a_pb2 import AgentCapabilities, AgentCard, AgentInterface
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind, StatusCode
from starlette.testclient import TestClient

from golem.catalog import Role
from golem.orchestrator.jobs import CatalogRef, build_job_manifest
from golem.orchestrator.service import JobTemplate, job_spec_for
from golem.runtime.deepagents_runner import DeepAgentsRunner
from golem.runtime.lead import Record
from golem.runtime.ports import Brief, RoleRunner
from golem.runtime.tracing import (
    CAPTURE_CONTENT,
    ExporterSettings,
    configure_tracing,
    exporter_settings,
    traced_main,
)
from golem.tasks.app import create_app
from golem.tasks.ports import Refused, RunStart, Started

TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"
PARENT_ID = "00f067aa0ba902b7"
TRACEPARENT = f"00-{TRACE_ID}-{PARENT_ID}-01"
GOAL = "Find evidence for faster onboarding"
SECRET_CONTENT = "the quarterly numbers are 42"
ENV = {"GOLEM_RUN_ID": "run-1", "GOLEM_AGENT": "discovery"}


class ScriptedModel(GenericFakeChatModel):
    model: str = "scripted-model"

    def bind_tools(self, tools: Sequence[Any], **kwargs: Any) -> "ScriptedModel":
        return self


def tool_call(name: str, **args: Any) -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": f"call-{name}"}])


def final(text: str = "Done.", **metadata: Any) -> AIMessage:
    return AIMessage(content=text, **metadata)


def make_brief(workspace: Path) -> Brief:
    (workspace / "hypotheses").mkdir(parents=True, exist_ok=True)
    target_text = "---\nid: H-1\nkind: hypothesis\nstatus: draft\n---\n## Evidence\n"
    (workspace / "hypotheses" / "H-1.md").write_text(target_text)
    return Brief(
        run_id="run-1",
        goal=GOAL,
        role=Role(name="researcher", writes="hypotheses/"),
        instructions="You are a careful researcher.",
        target=Record(
            id="H-1",
            kind="hypothesis",
            status="draft",
            links=frozenset(),
            empty_sections=frozenset({"Evidence"}),
        ),
        target_path=Path("hypotheses/H-1.md"),
        target_text=target_text,
        linked=(),
        workspace=workspace,
        skills_dir=None,
    )


def memory_provider() -> tuple[TracerProvider, InMemorySpanExporter]:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider, exporter


@dataclass
class FakeMain:
    """Stands in for runtime.main.main: builds the runner and runs one brief."""

    brief: Brief
    code: int = 0
    runners: list[RoleRunner] = field(default_factory=list)

    def __call__(self, environ: Mapping[str, str], factory: Callable[..., RoleRunner]) -> int:
        runner = factory(environ)
        self.runners.append(runner)
        asyncio.run(runner.run(self.brief))
        return self.code


def deepagents_factory(
    *replies: AIMessage,
) -> Callable[[Mapping[str, str], Sequence[BaseCallbackHandler]], RoleRunner]:
    model = ScriptedModel(messages=iter(replies))

    def factory(environ: Mapping[str, str], callbacks: Sequence[BaseCallbackHandler]) -> RoleRunner:
        return DeepAgentsRunner(model=model, callbacks=tuple(callbacks))

    return factory


def run_traced(
    tmp_path: Path,
    *replies: AIMessage,
    env: Mapping[str, str] = ENV,
    code: int = 0,
) -> list[ReadableSpan]:
    provider, exporter = memory_provider()
    traced_main(
        env, deepagents_factory(*replies), provider, run_main=FakeMain(make_brief(tmp_path), code)
    )
    return list(exporter.get_finished_spans())


def named(spans: list[ReadableSpan], prefix: str) -> list[ReadableSpan]:
    return [span for span in spans if span.name.startswith(prefix)]


def root_of(spans: list[ReadableSpan]) -> ReadableSpan:
    [root] = named(spans, "invoke_agent")
    return root


def all_attribute_text(spans: list[ReadableSpan]) -> str:
    return "\n".join(str(value) for span in spans for value in (span.attributes or {}).values())


# --- Spans of a run ------------------------------------------------------------------------------


def test_one_model_call_and_one_tool_call_nest_under_the_agent_span(tmp_path: Path) -> None:
    spans = run_traced(tmp_path, tool_call("ls", path="/"), final())

    root = root_of(spans)
    chats = named(spans, "chat ")
    [tool] = named(spans, "execute_tool ")
    assert len(chats) == 2
    assert root.name == "invoke_agent discovery"
    assert root.kind == SpanKind.INTERNAL
    assert {span.parent.span_id for span in [*chats, tool]} == {root.context.span_id}
    assert {span.context.trace_id for span in spans} == {root.context.trace_id}
    assert tool.name == "execute_tool ls"
    assert tool.kind == SpanKind.INTERNAL
    assert tool.attributes["gen_ai.operation.name"] == "execute_tool"
    assert tool.attributes["gen_ai.tool.name"] == "ls"
    assert tool.attributes["gen_ai.tool.call.id"] == "call-ls"
    assert tool.status.status_code != StatusCode.ERROR


def test_agent_span_carries_run_agent_role_and_target(tmp_path: Path) -> None:
    root = root_of(run_traced(tmp_path, final()))

    assert root.attributes["gen_ai.operation.name"] == "invoke_agent"
    assert root.attributes["gen_ai.agent.name"] == "discovery"
    assert root.attributes["golem.run.id"] == "run-1"
    assert root.attributes["golem.role"] == "researcher"
    assert root.attributes["golem.target.id"] == "H-1"


def test_chat_span_names_the_model_and_its_provider(tmp_path: Path) -> None:
    [chat] = named(run_traced(tmp_path, final()), "chat ")

    assert chat.name == "chat scripted-model"
    assert chat.kind == SpanKind.CLIENT
    assert chat.attributes["gen_ai.operation.name"] == "chat"
    assert chat.attributes["gen_ai.request.model"] == "scripted-model"
    assert "gen_ai.provider.name" in chat.attributes


def test_chat_span_records_usage_finish_reason_and_response_model(tmp_path: Path) -> None:
    reply = final(
        usage_metadata={"input_tokens": 120, "output_tokens": 30, "total_tokens": 150},
        response_metadata={"finish_reason": "stop", "model_name": "scripted-model-2026"},
    )

    [chat] = named(run_traced(tmp_path, reply), "chat ")

    assert chat.attributes["gen_ai.usage.input_tokens"] == 120
    assert chat.attributes["gen_ai.usage.output_tokens"] == 30
    assert chat.attributes["gen_ai.response.finish_reasons"] == ("stop",)
    assert chat.attributes["gen_ai.response.model"] == "scripted-model-2026"


def test_chat_span_without_usage_has_no_token_attributes(tmp_path: Path) -> None:
    [chat] = named(run_traced(tmp_path, final()), "chat ")

    assert "gen_ai.usage.input_tokens" not in chat.attributes


def test_a_failing_tool_call_marks_its_span_as_error(tmp_path: Path) -> None:
    spans = run_traced(tmp_path, tool_call("read_file"), final())

    [tool] = named(spans, "execute_tool ")
    assert tool.status.status_code == StatusCode.ERROR
    assert tool.attributes["error.type"] == "ValidationError"
    assert root_of(spans).status.status_code != StatusCode.ERROR


def test_a_tool_that_reports_an_error_marks_its_span_as_error(tmp_path: Path) -> None:
    spans = run_traced(
        tmp_path, tool_call("write_file", file_path="/README.md", content="x"), final()
    )

    [tool] = named(spans, "execute_tool ")
    assert tool.status.status_code == StatusCode.ERROR
    assert tool.attributes["error.type"] == "tool_error"


def test_a_failed_run_marks_the_agent_span_as_error(tmp_path: Path) -> None:
    root = root_of(run_traced(tmp_path, final(), code=1))

    assert root.status.status_code == StatusCode.ERROR


# --- Content is not recorded by default ----------------------------------------------------------


def test_spans_record_no_prompt_reply_or_tool_content_by_default(tmp_path: Path) -> None:
    spans = run_traced(
        tmp_path,
        tool_call("write_file", file_path="/hypotheses/H-2.md", content=SECRET_CONTENT),
        final(f"Wrote {SECRET_CONTENT}."),
    )

    text = all_attribute_text(spans)
    assert GOAL not in text
    assert "careful researcher" not in text
    assert SECRET_CONTENT not in text
    assert not any(
        key in (span.attributes or {})
        for span in spans
        for key in (
            "gen_ai.input.messages",
            "gen_ai.output.messages",
            "gen_ai.tool.call.arguments",
            "gen_ai.tool.call.result",
        )
    )


def test_content_is_recorded_when_opted_in(tmp_path: Path) -> None:
    spans = run_traced(
        tmp_path,
        tool_call("write_file", file_path="/hypotheses/H-2.md", content=SECRET_CONTENT),
        final("All written."),
        env={**ENV, CAPTURE_CONTENT: "true"},
    )

    chats = named(spans, "chat ")
    [tool] = named(spans, "execute_tool ")
    assert GOAL in chats[0].attributes["gen_ai.input.messages"]
    assert "All written." in chats[-1].attributes["gen_ai.output.messages"]
    assert SECRET_CONTENT in tool.attributes["gen_ai.tool.call.arguments"]
    assert "gen_ai.tool.call.result" in tool.attributes


# --- Trace context from the request ---------------------------------------------------------------


def test_the_agent_span_continues_the_trace_of_the_request(tmp_path: Path) -> None:
    root = root_of(run_traced(tmp_path, final(), env={**ENV, "TRACEPARENT": TRACEPARENT}))

    assert format(root.context.trace_id, "032x") == TRACE_ID
    assert root.parent is not None
    assert root.parent.is_remote
    assert format(root.parent.span_id, "016x") == PARENT_ID


@pytest.mark.parametrize("traceparent", [None, "", "not-a-traceparent"])
def test_without_a_valid_traceparent_the_run_starts_a_new_trace(
    tmp_path: Path, traceparent: str | None
) -> None:
    env = dict(ENV) if traceparent is None else {**ENV, "TRACEPARENT": traceparent}

    root = root_of(run_traced(tmp_path, final(), env=env))

    assert root.parent is None


# --- Export --------------------------------------------------------------------------------------


def test_no_endpoint_means_no_exporter() -> None:
    assert exporter_settings({}) is None
    assert exporter_settings({"OTEL_EXPORTER_OTLP_ENDPOINT": "  "}) is None


def test_configure_tracing_without_endpoint_still_traces_without_crashing() -> None:
    provider = configure_tracing({})

    with provider.get_tracer("test").start_as_current_span("span"):
        pass
    provider.shutdown()


def test_endpoint_and_headers_come_from_the_standard_otlp_variables() -> None:
    settings = exporter_settings(
        {
            "OTEL_EXPORTER_OTLP_ENDPOINT": "https://traces.example/api/public/otel",
            "OTEL_EXPORTER_OTLP_HEADERS": "Authorization=Basic%20cGs6c2s=,x-extra=1",
        }
    )

    assert settings == ExporterSettings(
        endpoint="https://traces.example/api/public/otel/v1/traces",
        headers={"authorization": "Basic cGs6c2s=", "x-extra": "1"},
    )


def test_a_traces_specific_endpoint_is_used_as_is() -> None:
    settings = exporter_settings(
        {
            "OTEL_EXPORTER_OTLP_ENDPOINT": "https://ignored.example",
            "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT": "https://traces.example/custom",
        }
    )

    assert settings is not None
    assert settings.endpoint == "https://traces.example/custom"


def test_settings_do_not_show_headers_in_repr() -> None:
    settings = ExporterSettings(endpoint="https://x.example/v1/traces", headers={"a": "secret"})

    assert "secret" not in repr(settings)


def test_main_flushes_spans_before_the_job_exits(tmp_path: Path) -> None:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    # A long delay: without an explicit flush nothing would be exported before exit.
    provider.add_span_processor(BatchSpanProcessor(exporter, schedule_delay_millis=600_000))

    code = traced_main(
        ENV, deepagents_factory(final()), provider, run_main=FakeMain(make_brief(tmp_path))
    )

    assert code == 0
    assert named(list(exporter.get_finished_spans()), "invoke_agent")


def test_main_flushes_even_when_the_run_raises(tmp_path: Path) -> None:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(BatchSpanProcessor(exporter, schedule_delay_millis=600_000))

    def crashing_main(environ: Mapping[str, str], factory: Callable[..., RoleRunner]) -> int:
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        traced_main(ENV, deepagents_factory(final()), provider, run_main=crashing_main)

    [root] = named(list(exporter.get_finished_spans()), "invoke_agent")
    assert root.status.status_code == StatusCode.ERROR


# --- Carrying the trace context into the Job -----------------------------------------------------


def run_start(**changes: str) -> RunStart:
    base = {
        "task_id": "task-1",
        "context_id": "ctx-1",
        "agent": "discovery",
        "goal": "go",
        "caller": "user:alice",
        "message_id": "m-1",
    }
    return RunStart(**{**base, **changes})


TEMPLATE = JobTemplate(
    image="registry.example/golem:1.0",
    namespace="team-a-jobs",
    secret_name="golem-run-secrets",
    active_deadline_seconds=1800,
    ttl_seconds_after_finished=600,
    cpu="500m",
    memory="1Gi",
)
CATALOG = CatalogRef(url="https://git.example/catalog.git", revision="a1b2c3d")


def job_env(run: RunStart) -> dict[str, str]:
    manifest = build_job_manifest(job_spec_for("run-1", run, CATALOG, TEMPLATE, "a.b.c"))
    [container] = manifest["spec"]["template"]["spec"]["containers"]
    return {item["name"]: item["value"] for item in container["env"]}


def test_the_job_env_carries_the_trace_context_of_the_run() -> None:
    env = job_env(run_start(traceparent=TRACEPARENT, tracestate="vendor=1"))

    assert env["TRACEPARENT"] == TRACEPARENT
    assert env["TRACESTATE"] == "vendor=1"


def test_a_run_without_trace_context_has_none_in_the_job_env() -> None:
    env = job_env(run_start())

    assert "TRACEPARENT" not in env
    assert "TRACESTATE" not in env


@dataclass
class RecordingOrchestrator:
    started: list[RunStart] = field(default_factory=list)

    async def start(self, run: RunStart) -> Started | Refused:
        self.started.append(run)
        return Started(run_id="run-1")

    async def cancel(self, task_id: str) -> None:
        return None

    async def status(self, run_id: str) -> str | None:
        return None


def send_with_headers(headers: dict[str, str]) -> RunStart:
    card = AgentCard(
        name="golem",
        description="Runs catalog agents as A2A tasks.",
        version="0.1.0",
        supported_interfaces=[
            AgentInterface(
                url="http://testserver/a2a", protocol_binding="JSONRPC", protocol_version="1.0"
            )
        ],
        capabilities=AgentCapabilities(streaming=False),
        default_input_modes=["text/plain"],
        default_output_modes=["text/plain"],
    )
    orchestrator = RecordingOrchestrator()
    with TestClient(create_app(card, orchestrator)) as client:
        client.post(
            "/a2a",
            headers={"A2A-Version": "1.0", **headers},
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "SendMessage",
                "params": {
                    "tenant": "discovery",
                    "message": {"role": "ROLE_USER", "messageId": "m1", "parts": [{"text": "go"}]},
                },
            },
        )
    [run] = orchestrator.started
    return run


def test_the_task_service_passes_the_request_trace_context_to_the_run() -> None:
    run = send_with_headers({"traceparent": TRACEPARENT, "tracestate": "vendor=1"})

    assert run.traceparent == TRACEPARENT
    assert run.tracestate == "vendor=1"


def test_a_request_without_trace_context_starts_a_run_without_one() -> None:
    run = send_with_headers({})

    assert run.traceparent == ""
    assert run.tracestate == ""
