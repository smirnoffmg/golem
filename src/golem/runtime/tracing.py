"""One trace per run: OpenTelemetry GenAI spans for the lead's role, exported over OTLP/HTTP.

Spans follow the GenAI semantic conventions, which are in Development status; names and
attributes are taken from https://github.com/open-telemetry/semantic-conventions-genai
(docs/gen-ai/gen-ai-agent-spans.md "Invoke agent internal span" and docs/gen-ai/gen-ai-spans.md
"Inference" and "Execute tool span"; the former opentelemetry.io/docs/specs/semconv/gen-ai pages
now point there):

- ``invoke_agent {agent}`` (INTERNAL), the run's root: ``gen_ai.operation.name``,
  ``gen_ai.agent.name``, and Golem's own ``golem.run.id``, ``golem.role``, ``golem.target.id``;
- ``chat {model}`` (CLIENT) per model call: ``gen_ai.provider.name``, ``gen_ai.request.model``,
  ``gen_ai.response.model``, ``gen_ai.response.finish_reasons``, ``gen_ai.usage.input_tokens``,
  ``gen_ai.usage.output_tokens``;
- ``execute_tool {tool}`` (INTERNAL) per tool call: ``gen_ai.tool.name``, ``gen_ai.tool.call.id``,
  ``gen_ai.tool.type``; a raised error or a tool message with error status sets the span status
  to ERROR and ``error.type`` (the exception class, or ``tool_error``).

Prompts, replies and tool arguments and results are opt-in in the conventions and may carry
secrets, so they are recorded only when ``OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT`` is
``true`` (the flag the conventions name), as ``gen_ai.input.messages``,
``gen_ai.output.messages``, ``gen_ai.tool.call.arguments`` and ``gen_ai.tool.call.result``.
Error messages are left out for the same reason; the status carries only the error type.

The root span continues the request's trace from ``TRACEPARENT``/``TRACESTATE`` (W3C Trace
Context in environment variables, https://opentelemetry.io/docs/specs/otel/context/env-carriers/)
and starts a new trace without a valid one.

Export reads the standard ``OTEL_EXPORTER_OTLP_ENDPOINT`` (``/v1/traces`` is appended) or
``OTEL_EXPORTER_OTLP_TRACES_ENDPOINT`` (used as is) and ``OTEL_EXPORTER_OTLP_HEADERS`` (or
``..._TRACES_HEADERS``); without an endpoint nothing is exported. Langfuse accepts OTLP over
HTTP (protobuf or JSON, not gRPC; self-hosted since v3.22.0) at ``/api/public/otel`` with Basic
auth of its public and secret key (https://langfuse.com/integrations/native/opentelemetry):
``OTEL_EXPORTER_OTLP_ENDPOINT=https://<langfuse>/api/public/otel`` and
``OTEL_EXPORTER_OTLP_HEADERS=Authorization=Basic%20<base64 pk:sk>``. Both belong in the Job's
secret, not in its manifest.
"""

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import BaseMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, LLMResult
from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import Span, SpanKind, Status, StatusCode, Tracer
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
from opentelemetry.util.re import parse_env_headers

from golem.runtime.main import main
from golem.runtime.ports import Brief, RoleResult, RoleRunner

TRACER_NAME = "golem.runtime"
DEFAULT_SERVICE_NAME = "golem-runtime"
CAPTURE_CONTENT = "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT"
TRACES_PATH = "v1/traces"
TOOL_ERROR = "tool_error"
ROLES = {"human": "user", "ai": "assistant", "system": "system", "tool": "tool"}

TracedRunnerFactory = Callable[[Mapping[str, str], Sequence[BaseCallbackHandler]], RoleRunner]


@dataclass(frozen=True)
class ExporterSettings:
    endpoint: str
    headers: Mapping[str, str] = field(default_factory=dict, repr=False)


def exporter_settings(env: Mapping[str, str]) -> ExporterSettings | None:
    endpoint = traces_endpoint(env)
    if endpoint is None:
        return None
    headers = env.get("OTEL_EXPORTER_OTLP_TRACES_HEADERS") or env.get(
        "OTEL_EXPORTER_OTLP_HEADERS", ""
    )
    return ExporterSettings(endpoint=endpoint, headers=dict(parse_env_headers(headers, True)))


def traces_endpoint(env: Mapping[str, str]) -> str | None:
    specific = env.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "").strip()
    if specific:
        return specific
    base = env.get("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip()
    if not base:
        return None
    return f"{base.rstrip('/')}/{TRACES_PATH}"


def configure_tracing(env: Mapping[str, str]) -> TracerProvider:
    service = env.get("OTEL_SERVICE_NAME") or DEFAULT_SERVICE_NAME
    provider = TracerProvider(resource=Resource.create({SERVICE_NAME: service}))
    settings = exporter_settings(env)
    if settings is not None:
        exporter = OTLPSpanExporter(endpoint=settings.endpoint, headers=dict(settings.headers))
        provider.add_span_processor(BatchSpanProcessor(exporter))
    return provider


def capture_content(env: Mapping[str, str]) -> bool:
    return env.get(CAPTURE_CONTENT, "").strip().lower() == "true"


def parent_context(env: Mapping[str, str]) -> Context:
    carrier = {
        key: env[name]
        for key, name in (("traceparent", "TRACEPARENT"), ("tracestate", "TRACESTATE"))
        if env.get(name)
    }
    return TraceContextTextMapPropagator().extract(carrier)


def agent_attributes(env: Mapping[str, str]) -> dict[str, str]:
    attributes = {"gen_ai.operation.name": "invoke_agent"}
    if env.get("GOLEM_AGENT"):
        attributes["gen_ai.agent.name"] = env["GOLEM_AGENT"]
    if env.get("GOLEM_RUN_ID"):
        attributes["golem.run.id"] = env["GOLEM_RUN_ID"]
    return attributes


def span_name(operation: str, subject: str | None) -> str:
    return f"{operation} {subject}" if subject else operation


def mark_error(span: Span, error_type: str) -> None:
    span.set_attribute("error.type", error_type)
    span.set_status(Status(StatusCode.ERROR, error_type))


@dataclass(frozen=True)
class TracedRunner:
    """Names the role and target on the run's span; the lead picks them only inside the run."""

    inner: RoleRunner
    span: Span

    async def run(self, brief: Brief) -> RoleResult:
        self.span.set_attribute("golem.role", brief.role.name)
        self.span.set_attribute("golem.target.id", brief.target.id)
        return await self.inner.run(brief)


def traced_main(
    environ: Mapping[str, str],
    runner_factory: TracedRunnerFactory,
    provider: TracerProvider,
    run_main: Callable[..., int] = main,
) -> int:
    tracer = provider.get_tracer(TRACER_NAME)
    try:
        with tracer.start_as_current_span(
            span_name("invoke_agent", environ.get("GOLEM_AGENT")),
            context=parent_context(environ),
            kind=SpanKind.INTERNAL,
            attributes=agent_attributes(environ),
            record_exception=False,
            set_status_on_exception=False,
        ) as span:
            handler = GenAISpans(tracer, trace.set_span_in_context(span), capture_content(environ))
            try:
                code = run_main(
                    environ,
                    lambda env: TracedRunner(runner_factory(env, (handler,)), span),
                )
            except Exception as error:
                mark_error(span, type(error).__name__)
                raise
            if code != 0:
                span.set_status(Status(StatusCode.ERROR, f"exit code {code}"))
            return code
    finally:
        # The Job's pod is gone right after exit; batched spans would be lost.
        provider.shutdown()


class GenAISpans(BaseCallbackHandler):
    """LangChain callbacks to GenAI spans under the run's span.

    Parents are tracked by LangChain run ids rather than the OpenTelemetry current context,
    which does not follow LangChain's callbacks reliably across its executors.
    """

    run_inline = True

    def __init__(self, tracer: Tracer, root: Context, capture: bool = False) -> None:
        self._tracer = tracer
        self._root = root
        self._capture = capture
        self._contexts: dict[UUID, Context] = {}
        self._spans: dict[UUID, Span] = {}

    def _parent(self, parent_run_id: UUID | None) -> Context:
        if parent_run_id is None:
            return self._root
        return self._contexts.get(parent_run_id, self._root)

    def _start(
        self,
        run_id: UUID,
        parent_run_id: UUID | None,
        name: str,
        kind: SpanKind,
        attributes: dict[str, Any],
    ) -> None:
        span = self._tracer.start_span(
            name, context=self._parent(parent_run_id), kind=kind, attributes=attributes
        )
        self._spans[run_id] = span
        self._contexts[run_id] = trace.set_span_in_context(span)

    def _finish(self, run_id: UUID) -> Span | None:
        self._contexts.pop(run_id, None)
        return self._spans.pop(run_id, None)

    def on_chain_start(
        self,
        serialized: dict[str, Any],
        inputs: dict[str, Any],
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        **kwargs: Any,
    ) -> None:
        self._contexts[run_id] = self._parent(parent_run_id)

    def on_chain_end(self, outputs: Any, *, run_id: UUID, **kwargs: Any) -> None:
        self._contexts.pop(run_id, None)

    def on_chain_error(self, error: BaseException, *, run_id: UUID, **kwargs: Any) -> None:
        self._contexts.pop(run_id, None)

    def on_chat_model_start(
        self,
        serialized: dict[str, Any],
        messages: list[list[BaseMessage]],
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        attributes = chat_attributes(metadata or {}, kwargs.get("invocation_params") or {})
        if self._capture and messages:
            attributes["gen_ai.input.messages"] = json.dumps(
                [message_json(m) for m in messages[0]], ensure_ascii=False
            )
        model = attributes.get("gen_ai.request.model")
        self._start(run_id, parent_run_id, span_name("chat", model), SpanKind.CLIENT, attributes)

    def on_llm_end(self, response: LLMResult, *, run_id: UUID, **kwargs: Any) -> None:
        span = self._finish(run_id)
        if span is None:
            return
        generations = [g for batch in response.generations for g in batch]
        span.set_attributes(response_attributes(generations))
        if self._capture:
            span.set_attribute(
                "gen_ai.output.messages",
                json.dumps([output_json(g) for g in generations], ensure_ascii=False),
            )
        span.end()

    def on_llm_error(self, error: BaseException, *, run_id: UUID, **kwargs: Any) -> None:
        span = self._finish(run_id)
        if span is not None:
            mark_error(span, type(error).__name__)
            span.end()

    def on_tool_start(
        self,
        serialized: dict[str, Any],
        input_str: str,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        **kwargs: Any,
    ) -> None:
        name = serialized.get("name") or kwargs.get("name") or "tool"
        attributes: dict[str, Any] = {
            "gen_ai.operation.name": "execute_tool",
            "gen_ai.tool.name": name,
            "gen_ai.tool.type": "function",
        }
        if kwargs.get("tool_call_id"):
            attributes["gen_ai.tool.call.id"] = kwargs["tool_call_id"]
        if self._capture:
            attributes["gen_ai.tool.call.arguments"] = input_str
        self._start(
            run_id, parent_run_id, span_name("execute_tool", name), SpanKind.INTERNAL, attributes
        )

    def on_tool_end(self, output: Any, *, run_id: UUID, **kwargs: Any) -> None:
        span = self._finish(run_id)
        if span is None:
            return
        if isinstance(output, ToolMessage) and output.status == "error":
            mark_error(span, TOOL_ERROR)
        elif self._capture:
            span.set_attribute("gen_ai.tool.call.result", content_text(output))
        span.end()

    def on_tool_error(self, error: BaseException, *, run_id: UUID, **kwargs: Any) -> None:
        span = self._finish(run_id)
        if span is not None:
            mark_error(span, type(error).__name__)
            span.end()


def chat_attributes(metadata: Mapping[str, Any], params: Mapping[str, Any]) -> dict[str, Any]:
    attributes: dict[str, Any] = {"gen_ai.operation.name": "chat"}
    model = metadata.get("ls_model_name") or params.get("model") or params.get("model_name")
    if model:
        attributes["gen_ai.request.model"] = str(model)
    if metadata.get("ls_provider"):
        attributes["gen_ai.provider.name"] = str(metadata["ls_provider"])
    return attributes


def response_attributes(generations: Sequence[Any]) -> dict[str, Any]:
    attributes: dict[str, Any] = {}
    reasons = [r for r in (finish_reason(g) for g in generations) if r]
    if reasons:
        attributes["gen_ai.response.finish_reasons"] = reasons
    messages = [g.message for g in generations if isinstance(g, ChatGeneration)]
    models = [m.response_metadata.get("model_name") for m in messages]
    if any(models):
        attributes["gen_ai.response.model"] = str(next(m for m in models if m))
    usages = [m.usage_metadata for m in messages if getattr(m, "usage_metadata", None)]
    if usages:
        attributes["gen_ai.usage.input_tokens"] = sum(u.get("input_tokens", 0) for u in usages)
        attributes["gen_ai.usage.output_tokens"] = sum(u.get("output_tokens", 0) for u in usages)
    return attributes


def finish_reason(generation: Any) -> str | None:
    info = generation.generation_info or {}
    if info.get("finish_reason"):
        return str(info["finish_reason"])
    if isinstance(generation, ChatGeneration):
        reason = generation.message.response_metadata.get("finish_reason")
        return str(reason) if reason else None
    return None


def content_text(value: Any) -> str:
    content = getattr(value, "content", value)
    return content if isinstance(content, str) else json.dumps(content, default=str)


def message_json(message: BaseMessage) -> dict[str, Any]:
    parts: list[dict[str, Any]] = []
    if isinstance(message, ToolMessage):
        parts.append(
            {"type": "tool_call_response", "id": message.tool_call_id, "response": message.text}
        )
    elif message.text:
        parts.append({"type": "text", "content": message.text})
    for call in getattr(message, "tool_calls", None) or []:
        parts.append(
            {
                "type": "tool_call",
                "id": call.get("id"),
                "name": call["name"],
                "arguments": call["args"],
            }
        )
    return {"role": ROLES.get(message.type, message.type), "parts": parts}


def output_json(generation: Any) -> dict[str, Any]:
    message = generation.message if isinstance(generation, ChatGeneration) else None
    output = (
        message_json(message)
        if message is not None
        else {
            "role": "assistant",
            "parts": [{"type": "text", "content": generation.text}],
        }
    )
    return {**output, "finish_reason": finish_reason(generation) or "stop"}
