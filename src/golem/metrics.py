"""Platform metrics (ADR 0013): RED for every HTTP process, measured the same way, and the
domain metrics of runs, the reconciler and the guards at the entry points.

Every label value comes from a closed set: route templates, a fixed list of HTTP methods,
status classes, enum values, configured agents and tool names. Anything outside the set is
recorded as ``other`` (or ``unmatched`` for a path no route serves), so no client and no Job
can create a time series by choosing a path, a method, an agent or a tool name. Principals,
task and run ids, addresses and goals never become label values; they belong to the audit log
and the traces.

Each process keeps its metrics in its own registry and serves them on a port of their own
(``metrics_app``), never on a port its callers reach.
"""

import time
from collections.abc import Callable, Collection, Sequence
from decimal import Decimal

from prometheus_client import (
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    PlatformCollector,
    ProcessCollector,
    make_asgi_app,
)
from starlette.applications import Starlette
from starlette.routing import BaseRoute, Match, Route
from starlette.types import ASGIApp, Message, Receive, Scope, Send

METRICS_PATH = "/metrics"
DEFAULT_METRICS_PORT = 9090
OTHER = "other"
UNMATCHED = "unmatched"
METHODS = frozenset({"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"})
PROPOSAL_KINDS = frozenset({"merge_request", "wiki_edit", "desk_reply", "tracker_issue"})
DECISIONS = frozenset({"accept", "reject"})
# "unrecorded": the write server answered, but the row had moved on and kept another state.
APPLY_RESULTS = frozenset({"applied", "stale", "failed", "unanswered", "unrecorded"})
# The edge waits up to 30 s for the task service; the last bucket catches that.
HTTP_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0)
# A Job's deadline is an hour by default.
RUN_BUCKETS = (10.0, 30.0, 60.0, 120.0, 300.0, 600.0, 900.0, 1800.0, 3600.0, 7200.0)
PASS_BUCKETS = (0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0)


def bounded(value: str, allowed: Collection[str]) -> str:
    return value if value in allowed else OTHER


def status_class(status: int) -> str:
    return f"{status // 100}xx" if 100 <= status < 600 else OTHER


def route_of(scope: Scope, routes: Sequence[BaseRoute]) -> str:
    """The template of the route serving the request, never the path itself."""
    for route in routes:
        match, _ = route.matches(scope)
        # PARTIAL: the path is served, the method is not (405); still that route.
        if match is not Match.NONE and isinstance(route, Route):
            return route.path
    return UNMATCHED


class Metrics:
    """The labelled metrics every process may record; ``process`` names the recording one.

    ``agents`` is the configured, bounded set of agent names; any other agent is ``other``.
    """

    def __init__(
        self,
        process: str,
        *,
        registry: CollectorRegistry | None = None,
        agents: Collection[str] = (),
    ) -> None:
        self.process = process
        self.registry = CollectorRegistry() if registry is None else registry
        self._agents = frozenset(agents)
        r = self.registry
        self._requests = Counter(
            "golem_http_requests_total",
            "HTTP requests served, by route template and status class.",
            ["process", "route", "method", "status_class"],
            registry=r,
        )
        self._duration = Histogram(
            "golem_http_request_duration_seconds",
            "Time to serve an HTTP request, by route template.",
            ["process", "route", "method"],
            buckets=HTTP_BUCKETS,
            registry=r,
        )
        self._rate_limited = Counter(
            "golem_rate_limit_refusals_total",
            "Requests refused with 429 by a rate limit (ADR 0012).",
            ["process", "limit"],
            registry=r,
        )
        self._authentication_failures = Counter(
            "golem_authentication_failures_total",
            "Requests refused because their credential was missing or did not verify.",
            ["process"],
            registry=r,
        )
        self._audit_failures = Counter(
            "golem_audit_write_failures_total",
            "Audit rows that could not be written; the request was refused.",
            ["process"],
            registry=r,
        )
        self._policy_denials = Counter(
            "golem_policy_denials_total",
            "Calls refused by the edge's chain policy.",
            ["reason"],
            registry=r,
        )
        self._tool_calls = Counter(
            "golem_mcp_tool_calls_total",
            "MCP tools/call requests decided by a platform MCP server's gate.",
            ["group", "tool", "decision"],
            registry=r,
        )
        self._runs_started = Counter(
            "golem_runs_started_total", "Runs admitted and recorded.", ["agent"], registry=r
        )
        self._admission_rejections = Counter(
            "golem_admission_rejections_total",
            "Run starts refused by admission.",
            ["reason"],
            registry=r,
        )
        self._reserved_cost = Counter(
            "golem_run_reserved_cost_total",
            "Estimated cost reserved by admitted runs, in budget units.",
            ["agent"],
            registry=r,
        )
        self._run_outcomes = Counter(
            "golem_run_outcomes_total",
            "Runs that reached a final status, by outcome.",
            ["agent", "outcome"],
            registry=r,
        )
        self._run_duration = Histogram(
            "golem_run_duration_seconds",
            "Time from recording a run to its final status.",
            ["agent", "outcome"],
            buckets=RUN_BUCKETS,
            registry=r,
        )
        self._proposal_decisions = Counter(
            "golem_proposal_decisions_total",
            "Proposals a person decided in Golem (ADR 0015), by kind and decision.",
            ["kind", "decision"],
            registry=r,
        )
        self._proposal_applies = Counter(
            "golem_proposal_applies_total",
            "Applies of accepted proposals through the write servers, by kind and result;"
            " unanswered ones are asked again by the reconciler.",
            ["kind", "result"],
            registry=r,
        )

    def request_served(self, route: str, method: str, status: int, seconds: float) -> None:
        method = bounded(method, METHODS)
        self._requests.labels(self.process, route, method, status_class(status)).inc()
        self._duration.labels(self.process, route, method).observe(seconds)

    def rate_limit_refused(self, limit: str) -> None:
        self._rate_limited.labels(self.process, limit).inc()

    def authentication_failed(self) -> None:
        self._authentication_failures.labels(self.process).inc()

    def audit_write_failed(self) -> None:
        self._audit_failures.labels(self.process).inc()

    def policy_denied(self, reason: str) -> None:
        self._policy_denials.labels(reason).inc()

    def tool_called(self, group: str, tool: str, decision: str, *, tools: Collection[str]) -> None:
        self._tool_calls.labels(group, bounded(tool, tools), decision).inc()

    def run_started(self, agent: str, reserved_cost: Decimal) -> None:
        self._runs_started.labels(self.agent(agent)).inc()
        self._reserved_cost.labels(self.agent(agent)).inc(float(reserved_cost))

    def admission_rejected(self, reason: str) -> None:
        self._admission_rejections.labels(reason).inc()

    def run_ended(self, agent: str, outcome: str, seconds: float) -> None:
        self._run_outcomes.labels(self.agent(agent), outcome).inc()
        self._run_duration.labels(self.agent(agent), outcome).observe(seconds)

    def proposal_decided(self, kind: str, decision: str) -> None:
        self._proposal_decisions.labels(
            bounded(kind, PROPOSAL_KINDS), bounded(decision, DECISIONS)
        ).inc()

    def proposal_applied(self, kind: str, result: str) -> None:
        self._proposal_applies.labels(
            bounded(kind, PROPOSAL_KINDS), bounded(result, APPLY_RESULTS)
        ).inc()

    def agent(self, name: str) -> str:
        return bounded(name, self._agents)


class ReconcilerMetrics:
    """The reconciler's own metrics, beside the run metrics it records into ``runs``."""

    def __init__(self, runs: Metrics | None = None) -> None:
        self.runs = Metrics("reconciler") if runs is None else runs
        r = self.runs.registry
        self._pass_duration = Histogram(
            "golem_reconcile_pass_duration_seconds",
            "Time a reconcile pass took, failed or not.",
            buckets=PASS_BUCKETS,
            registry=r,
        )
        self._pass_errors = Counter(
            "golem_reconcile_pass_errors_total", "Reconcile passes that failed.", registry=r
        )
        self._outbox_pending = Gauge(
            "golem_outbox_pending",
            "Tasks whose run has a final outcome they have not been told yet.",
            registry=r,
        )
        self._proposals_pending = Gauge(
            "golem_proposals_pending",
            "Succeeded runs whose proposal (merge request or none) is not settled yet.",
            registry=r,
        )
        self._proposals_settled = Counter(
            "golem_proposals_settled_total",
            "Succeeded runs whose proposal was settled.",
            registry=r,
        )
        self._merge_request_failures = Counter(
            "golem_merge_request_failures_total",
            "Attempts to find a run's branch or open its merge request that failed.",
            registry=r,
        )

    def pass_finished(self, seconds: float, *, failed: bool) -> None:
        self._pass_duration.observe(seconds)
        if failed:
            self._pass_errors.inc()

    def pending(self, *, outbox: int, proposals: int) -> None:
        self._outbox_pending.set(outbox)
        self._proposals_pending.set(proposals)

    def proposal_settled(self) -> None:
        self._proposals_settled.inc()

    def merge_request_failed(self) -> None:
        self._merge_request_failures.inc()


class Instrumented:
    """RED for one listener: every request, refused or served, counted by its route template.

    Wraps the outermost app, so refusals by guards in front of the routes (the edge token,
    the MCP gate, the UI's headers) are counted under the route they were aimed at; ``routes``
    are the routes behind them, used only to name the route.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        routes: Sequence[BaseRoute],
        metrics: Metrics,
        clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        self.app = app
        self.routes = list(routes)
        self.metrics = metrics
        self._clock = clock

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        route = route_of(scope, self.routes)
        # An app that returns or raises without a response gets a 500 from the server.
        status = 500

        async def sending(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        started = self._clock()
        try:
            await self.app(scope, receive, sending)
        finally:
            self.metrics.request_served(route, scope["method"], status, self._clock() - started)


class _Exposition:
    # Route serves a class instance as an ASGI app, but would call make_asgi_app's plain
    # function as a request handler.
    def __init__(self, registry: CollectorRegistry) -> None:
        self._app = make_asgi_app(registry)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        await self._app(scope, receive, send)


def metrics_app(registry: CollectorRegistry) -> Starlette:
    """``GET /metrics`` and nothing else, for the metrics port only."""
    return Starlette(routes=[Route(METRICS_PATH, _Exposition(registry), methods=["GET"])])


def process_registry() -> CollectorRegistry:
    """A process's registry, with its CPU, memory and file descriptors and the Python version."""
    registry = CollectorRegistry()
    ProcessCollector(registry=registry)
    PlatformCollector(registry=registry)
    return registry
