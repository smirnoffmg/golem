"""golem.metrics: RED measured the same way for every listener, with bounded label values."""

import asyncio
from decimal import Decimal

import httpx
import pytest
from prometheus_client import CollectorRegistry
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response
from starlette.routing import Route
from starlette.testclient import TestClient

from golem.metrics import (
    OTHER,
    UNMATCHED,
    Instrumented,
    Metrics,
    ReconcilerMetrics,
    metrics_app,
    process_registry,
    status_class,
)
from golem.serving import serve_all, with_metrics

REQUESTS = "golem_http_requests_total"
DURATION = "golem_http_request_duration_seconds"


async def task(request: Request) -> Response:
    return PlainTextResponse(request.path_params["task_id"])


async def broken(request: Request) -> Response:
    raise RuntimeError("boom")


def app_with(metrics: Metrics) -> Instrumented:
    inner = Starlette(
        routes=[
            Route("/tasks/{task_id}", task, methods=["GET"]),
            Route("/broken", broken, methods=["GET"]),
        ]
    )
    return Instrumented(inner, routes=inner.routes, metrics=metrics)


def label_values(registry: CollectorRegistry, sample: str, label: str) -> set[str]:
    return {
        s.labels[label] for family in registry.collect() for s in family.samples if s.name == sample
    }


def count(registry: CollectorRegistry, sample: str, **labels: str) -> float:
    return registry.get_sample_value(sample, labels) or 0.0


def test_a_request_is_counted_under_its_route_template_not_its_path() -> None:
    metrics = Metrics("tasks")
    client = TestClient(app_with(metrics))

    for task_id in ("t-1", "t-2", "0b5c9a3e-4b52-4f7a-9e0c-3c1f4b2a7d11"):
        assert client.get(f"/tasks/{task_id}").status_code == 200

    assert label_values(metrics.registry, REQUESTS, "route") == {"/tasks/{task_id}"}
    labels = {"process": "tasks", "route": "/tasks/{task_id}", "method": "GET"}
    assert count(metrics.registry, REQUESTS, **labels, status_class="2xx") == 3
    assert count(metrics.registry, f"{DURATION}_count", **labels) == 3


def test_an_id_in_the_path_adds_no_label_value() -> None:
    metrics = Metrics("tasks")
    client = TestClient(app_with(metrics))
    client.get("/tasks/first")
    before = {
        label: label_values(metrics.registry, REQUESTS, label)
        for label in ("process", "route", "method", "status_class")
    }

    client.get("/tasks/a-task-id-never-seen-before")

    after = {label: label_values(metrics.registry, REQUESTS, label) for label in before}
    assert after == before


def test_unknown_paths_and_methods_collapse_to_one_value_each() -> None:
    metrics = Metrics("ui")
    client = TestClient(app_with(metrics))

    for path in ("/", "/etc/passwd", "/tasks", "/tasks/a/b"):
        assert client.get(path).status_code == 404
    client.request("BREW", "/tasks/t-1")
    client.request("PROPFIND", "/tasks/t-1")

    assert (
        count(
            metrics.registry,
            REQUESTS,
            process="ui",
            route=UNMATCHED,
            method="GET",
            status_class="4xx",
        )
        == 4
    )
    assert (
        count(
            metrics.registry,
            REQUESTS,
            process="ui",
            route="/tasks/{task_id}",
            method=OTHER,
            status_class="4xx",
        )
        == 2
    )
    assert label_values(metrics.registry, REQUESTS, "method") == {"GET", OTHER}


def test_a_handler_that_raises_is_counted_as_a_server_error() -> None:
    metrics = Metrics("edge")
    client = TestClient(app_with(metrics), raise_server_exceptions=False)

    assert client.get("/broken").status_code == 500

    assert (
        count(
            metrics.registry,
            REQUESTS,
            process="edge",
            route="/broken",
            method="GET",
            status_class="5xx",
        )
        == 1
    )


def test_a_refusal_in_front_of_the_routes_counts_under_the_route_it_was_aimed_at() -> None:
    metrics = Metrics("tasks")
    inner = Starlette(routes=[Route("/tasks/{task_id}", task, methods=["GET"])])

    async def guard(scope, receive, send) -> None:
        await PlainTextResponse("no", status_code=401)(scope, receive, send)

    client = TestClient(Instrumented(guard, routes=inner.routes, metrics=metrics))
    client.get("/tasks/t-9")

    assert (
        count(
            metrics.registry,
            REQUESTS,
            process="tasks",
            route="/tasks/{task_id}",
            method="GET",
            status_class="4xx",
        )
        == 1
    )


def test_status_classes_are_bounded() -> None:
    assert [status_class(s) for s in (101, 200, 302, 429, 503)] == [
        "1xx",
        "2xx",
        "3xx",
        "4xx",
        "5xx",
    ]
    assert status_class(0) == status_class(999) == OTHER


def test_an_unconfigured_agent_is_recorded_as_other() -> None:
    metrics = Metrics("tasks", agents={"discovery"})

    metrics.run_started("discovery", Decimal("1.5"))
    metrics.run_started("made-up-by-a-caller", Decimal("1"))
    metrics.run_ended("another-made-up-one", "failed", 3.0)

    assert label_values(metrics.registry, "golem_runs_started_total", "agent") == {
        "discovery",
        OTHER,
    }
    assert count(metrics.registry, "golem_run_reserved_cost_total", agent="discovery") == 1.5
    assert label_values(metrics.registry, "golem_run_outcomes_total", "agent") == {OTHER}


def test_a_tool_outside_the_group_is_recorded_as_other() -> None:
    metrics = Metrics("mcp")
    tools = frozenset({"search_issues", "get_issue"})

    metrics.tool_called("tracker.read", "get_issue", "allow", tools=tools)
    metrics.tool_called("tracker.read", "drop_all_tables", "deny", tools=tools)

    assert label_values(metrics.registry, "golem_mcp_tool_calls_total", "tool") == {
        "get_issue",
        OTHER,
    }


def test_the_metrics_app_serves_the_registry_at_metrics_only() -> None:
    metrics = Metrics("edge")
    metrics.authentication_failed()
    with TestClient(metrics_app(metrics.registry)) as client:
        served = client.get("/metrics")
        elsewhere = client.get("/")
        posted = client.post("/metrics")

    assert served.status_code == 200
    assert served.headers["content-type"].startswith("text/plain")
    assert 'golem_authentication_failures_total{process="edge"} 1.0' in served.text
    assert elsewhere.status_code == 404
    assert posted.status_code == 405


async def test_the_metrics_app_runs_its_lifespan_under_a_server() -> None:
    # uvicorn sends lifespan events; the exposition app alone asserts on them.
    app = metrics_app(Metrics("edge").registry)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://metrics"
    ) as client:
        assert (await client.get("/metrics")).status_code == 200


def test_the_reconciler_metrics_share_the_run_metrics_registry() -> None:
    runs = Metrics("reconciler", agents={"discovery"})
    reconciler = ReconcilerMetrics(runs)

    reconciler.pass_finished(0.2, failed=True)
    reconciler.pending(outbox=3, proposals=1)
    reconciler.merge_request_failed()
    runs.run_ended("discovery", "idle", 42.0)

    r = runs.registry
    assert count(r, "golem_reconcile_pass_errors_total") == 1
    assert count(r, "golem_reconcile_pass_duration_seconds_count") == 1
    assert count(r, "golem_outbox_pending") == 3
    assert count(r, "golem_proposals_pending") == 1
    assert count(r, "golem_merge_request_failures_total") == 1
    assert count(r, "golem_run_outcomes_total", agent="discovery", outcome="idle") == 1


def test_a_process_registry_carries_the_platform_collector() -> None:
    assert "python_info" in {family.name for family in process_registry().collect()}


async def test_metrics_are_served_on_the_metrics_port_and_never_on_the_public_one() -> None:
    metrics = Metrics("edge")
    metrics.authentication_failed()
    servers = with_metrics(
        app_with(metrics), 0, registry=metrics.registry, metrics_port=1, log_level="warning"
    )
    for server in servers:
        server.config.port = 0
    serving = asyncio.create_task(serve_all(servers))
    while not all(s.started for s in servers):
        assert not serving.done(), serving
        await asyncio.sleep(0.05)
    public, exposed = (
        f"http://127.0.0.1:{s.servers[0].sockets[0].getsockname()[1]}" for s in servers
    )
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            on_public = await client.get(f"{public}/metrics")
            on_metrics = await client.get(f"{exposed}/metrics")
    finally:
        servers[0].should_exit = True
        await asyncio.wait_for(serving, 10)

    assert on_public.status_code == 404
    assert "golem_authentication_failures_total" in on_metrics.text


def test_metrics_never_share_the_public_port() -> None:
    with pytest.raises(ValueError, match="public port"):
        with_metrics(app_with(Metrics("ui")), 8000, registry=CollectorRegistry(), metrics_port=8000)
