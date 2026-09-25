"""The guides say what the code does: links, settings, metrics, alerts, paths and commands.

Cheap checks only; the walkthroughs that need a cluster are described in each guide's "How
this was checked" section.
"""

import os
import re
import shlex
import shutil
import subprocess
import sys
from collections.abc import Callable, Iterator, Mapping
from functools import cache
from pathlib import Path

import pytest
import yaml
from cryptography.fernet import Fernet
from prometheus_client import CollectorRegistry
from test_k8s_render import K8S, SETTINGS, container, find, render

from golem.evaluation.cli import GATEWAY_SETTINGS
from golem.mcp.settings import mcp_settings
from golem.metrics import Metrics, ReconcilerMetrics
from golem.runtime.deepagents_runner import model_timeout
from golem.runtime.main import parse_settings
from golem.settings import (
    SettingsError,
    adapter_settings,
    edge_settings,
    mattermost_adapter_settings,
    parse_signing_key,
    reconciler_settings,
    task_service_settings,
    ui_settings,
)

ROOT = Path(__file__).parent.parent
DOCS = ROOT / "docs"
OPERATIONS = DOCS / "operations"
CONFIGURATION = OPERATIONS / "configuration.md"
EXAMPLE_OVERLAY = K8S / "overlays" / "example"

FENCE = re.compile(r"^```.*?^```", re.MULTILINE | re.DOTALL)
INLINE_CODE = re.compile(r"`[^`\n]+`")
LINK = re.compile(r"!?\[[^\]]*\]\(([^)\s]+)\)")
HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*$", re.MULTILINE)
RUN_BLOCK = re.compile(r"<!-- run: ([a-z0-9-]+) -->\n```sh\n(.*?)```", re.DOTALL)
SETTINGS_TABLE = re.compile(r"<!-- settings: ([a-z-]+) -->\n((?:\|.*\n)+)")
ENV_NAME = re.compile(r"\b(?:GOLEM|OTEL)_[A-Z0-9_]*[A-Z0-9]\b")
REPO_PATH = re.compile(r"^(?:src|deploy|docs|examples|tests|scripts)/[\w./-]+$")
METRIC_NAME = re.compile(r"\bgolem_[a-z_]+\b")
ALERT_NAME = re.compile(r"\bGolem[A-Z][A-Za-z]+\b")
# Paths the install guide tells the operator to create.
CREATED_BY_THE_READER = ("deploy/k8s/overlays/prod",)
# golem_* words that are databases, roles or fields, not metrics.
NOT_METRICS = frozenset(
    {"golem_tasks", "golem_runs", "golem_audit", "golem_ui", "golem_edge", "golem_mcp"}
    | {"golem_audit_owner", "golem_task_id"}
)
SAMPLE_SUFFIXES = {"counter": ("_total",), "histogram": ("", "_bucket", "_sum", "_count")}
PLACEHOLDERS = re.compile(r"192\.0\.2\.|198\.51\.100\.|203\.0\.113\.|example\.com|replace-with")


def markdown_files() -> list[Path]:
    return [ROOT / "README.md", *sorted(DOCS.rglob("*.md"))]


def prose(text: str) -> str:
    """The text without fenced code, where links and headings are not markup."""
    return FENCE.sub("", text)


def github_slug(heading: str) -> str:
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", heading).strip().lower()
    return re.sub(r"[^\w\- ]", "", text).replace(" ", "-")


@cache
def anchors(path: Path) -> frozenset[str]:
    seen: dict[str, int] = {}
    found = set()
    for _, heading in HEADING.findall(prose(path.read_text(encoding="utf-8"))):
        slug = github_slug(heading)
        count = seen.get(slug, 0)
        found.add(slug if count == 0 else f"{slug}-{count}")
        seen[slug] = count + 1
    return frozenset(found)


def links(path: Path) -> Iterator[str]:
    text = INLINE_CODE.sub("", prose(path.read_text(encoding="utf-8")))
    yield from LINK.findall(text)


def relative(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


@pytest.mark.parametrize("path", markdown_files(), ids=relative)
def test_every_relative_link_and_image_resolves(path: Path) -> None:
    broken = []
    for target in links(path):
        if re.match(r"^[a-z]+:", target):
            continue
        file_part, _, anchor = target.partition("#")
        resolved = (path.parent / file_part).resolve() if file_part else path
        if not resolved.exists():
            broken.append(f"{target}: no such file")
        elif anchor and resolved.suffix == ".md" and anchor not in anchors(resolved):
            broken.append(f"{target}: no heading #{anchor}")
    assert broken == []


@pytest.mark.parametrize("path", markdown_files(), ids=relative)
def test_every_repository_path_in_code_exists(path: Path) -> None:
    text = prose(path.read_text(encoding="utf-8"))
    mentioned = {span.strip("`").rstrip(".,:") for span in INLINE_CODE.findall(text)}
    missing = [
        name
        for name in sorted(mentioned)
        if REPO_PATH.match(name)
        and not name.startswith(CREATED_BY_THE_READER)
        and not (ROOT / name).exists()
    ]
    assert missing == []


def missing_names(parse: Callable[[Mapping[str, str]], object], env: Mapping[str, str]) -> set:
    with pytest.raises((SettingsError, ValueError)) as error:
        parse(env)
    return set(ENV_NAME.findall(str(error.value)))


def evaluation_settings(env: Mapping[str, str]) -> None:
    missing = [name for name in GATEWAY_SETTINGS if not env.get(name)]
    if missing:
        raise SettingsError(f"missing model gateway settings: {', '.join(missing)}")


PARSERS: dict[str, Callable[[Mapping[str, str]], object]] = {
    "edge": edge_settings,
    "tasks": task_service_settings,
    "reconciler": reconciler_settings,
    "jira-adapter": adapter_settings,
    "mattermost-adapter": mattermost_adapter_settings,
    "mcp": mcp_settings,
    "ui": ui_settings,
    "runtime": parse_settings,
    "evaluation": evaluation_settings,
}


@cache
def settings_tables() -> dict[str, dict[str, str]]:
    """Process to setting to its "Required or default" cell, from configuration.md."""
    tables = {}
    for process, table in SETTINGS_TABLE.findall(CONFIGURATION.read_text(encoding="utf-8")):
        rows: dict[str, str] = {}
        for line in table.splitlines()[2:]:
            cells = [cell.strip() for cell in line.strip("|").split("|")]
            for name in ENV_NAME.findall(cells[0]):
                rows[name] = cells[1]
        tables[process] = rows
    return tables


def test_the_reference_has_a_table_for_every_process() -> None:
    assert set(settings_tables()) == set(PARSERS)


@pytest.mark.parametrize("process", PARSERS)
def test_required_settings_in_the_reference_are_those_the_parser_requires(process: str) -> None:
    documented = {name for name, cell in settings_tables()[process].items() if cell == "required"}

    assert documented == missing_names(PARSERS[process], {})


def test_conditionally_required_settings_are_required_under_their_condition() -> None:
    assert "GOLEM_MCP_JIRA_DEPLOYMENT" in missing_names(
        mcp_settings, {"GOLEM_MCP_GROUP": "tracker.read"}
    )
    assert settings_tables()["mcp"]["GOLEM_MCP_JIRA_DEPLOYMENT"] == "required for `tracker.read`"
    with pytest.raises(SettingsError, match="GOLEM_PUSH_CONFIG_KEY is required"):
        task_service_settings(
            dict.fromkeys(missing_names(task_service_settings, {}), "1")
            | {"GOLEM_KUBERNETES": "none", "GOLEM_PUSH_ALLOWED_PREFIXES": "http://adapter/"}
        )


@cache
def names_in_source() -> frozenset[str]:
    names = set()
    for path in (ROOT / "src" / "golem").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        names |= set(re.findall(r'"((?:GOLEM|OTEL)_[A-Z0-9_]+)"', text))
        for rate in re.findall(r'rate_setting\(\s*env,\s*"(GOLEM_RATE_[A-Z_]+)"', text):
            names |= {f"{rate}_PER_MINUTE", f"{rate}_BURST"}
    return frozenset(names)


def test_every_setting_the_code_reads_is_in_the_reference() -> None:
    documented = set().union(*(set(rows) for rows in settings_tables().values()))
    rates = {name for name in names_in_source() if name.startswith("GOLEM_RATE_")}
    missing = sorted(
        name
        for name in names_in_source()
        # Rate prefixes appear only with their suffixes.
        if name not in documented and not any(r.startswith(f"{name}_") for r in rates)
    )
    assert missing == []


def test_no_setting_in_the_reference_is_unknown_to_the_code() -> None:
    mentioned = set(ENV_NAME.findall(CONFIGURATION.read_text(encoding="utf-8")))

    assert sorted(mentioned - names_in_source()) == []


@cache
def metric_names() -> frozenset[str]:
    registry = CollectorRegistry()
    runs = Metrics("docs", registry=registry)
    ReconcilerMetrics(runs)
    names = set()
    for family in registry.collect():
        # A labelled metric has no samples before its first use, so names come from the type.
        names |= {family.name + suffix for suffix in SAMPLE_SUFFIXES.get(family.type, ("",))}
    return frozenset(name for name in names if name.startswith("golem_"))


@pytest.mark.parametrize("path", sorted(OPERATIONS.glob("*.md")), ids=relative)
def test_every_metric_named_in_the_operations_guides_exists(path: Path) -> None:
    named = set(METRIC_NAME.findall(path.read_text(encoding="utf-8"))) - NOT_METRICS

    assert sorted(named - metric_names()) == []


@cache
def alert_rules() -> list[dict]:
    text = (OPERATIONS / "alerts.md").read_text(encoding="utf-8")
    [rule] = [
        doc
        for block in re.findall(r"```yaml\n(.*?)```", text, re.DOTALL)
        if (doc := yaml.safe_load(block)).get("kind") == "PrometheusRule"
    ]
    return [r for group in rule["spec"]["groups"] for r in group["rules"]]


def test_every_alert_routes_by_a_documented_severity() -> None:
    assert {rule["labels"]["severity"] for rule in alert_rules()} <= {"critical", "warning"}


def test_every_alert_has_a_runbook_entry() -> None:
    entries = anchors(OPERATIONS / "runbooks.md")

    assert [r["alert"] for r in alert_rules() if r["alert"].lower() not in entries] == []


@pytest.mark.parametrize("path", sorted(OPERATIONS.glob("*.md")), ids=relative)
def test_every_alert_named_in_the_operations_guides_exists(path: Path) -> None:
    named = set(ALERT_NAME.findall(prose(path.read_text(encoding="utf-8"))))

    assert sorted(named - {rule["alert"] for rule in alert_rules()}) == []


def run_blocks(path: Path) -> dict[str, str]:
    return dict(RUN_BLOCK.findall(path.read_text(encoding="utf-8")))


# What the operator exports before writing the environment files (install.md, step 6).
OPERATOR_INPUTS = {
    "PGHOST": "10.20.0.5",
    "PGPORT": "5432",
    "JIRA_ADAPTER_CLIENT_SECRET": "jira-client-secret",
    "MATTERMOST_ADAPTER_CLIENT_SECRET": "mattermost-client-secret",
    "UI_CLIENT_SECRET": "ui-client-secret",
    "JIRA_TOKEN": "jira-token",
    "CONFLUENCE_TOKEN": "confluence-token",
    "MATTERMOST_BOT_TOKEN": "bot-token",
    "MATTERMOST_COMMAND_TOKEN": "command-token",
    "GIT_TOKEN": "git-token",
    "GITLAB_API_TOKEN": "gitlab-api-token",
    "MODEL_GATEWAY_URL": "https://llm.internal/v1",
    "MODEL": "discovery-default",
    "MODEL_KEY": "model-key",
    "OTLP_ENDPOINT": "",
    "OTLP_HEADERS": "",
}


@pytest.fixture(scope="module")
def generated(tmp_path_factory: pytest.TempPathFactory) -> Path:
    if shutil.which("openssl") is None:
        pytest.skip("openssl is needed to run the install guide's secret generation")
    workdir = tmp_path_factory.mktemp("install")
    blocks = run_blocks(OPERATIONS / "install.md")
    for name in ("secrets", "env-files"):
        subprocess.run(
            ["bash", "-c", "set -eo pipefail\n" + blocks[name]],
            cwd=workdir,
            env={"PATH": os.environ["PATH"], **OPERATOR_INPUTS},
            check=True,
        )
    return workdir / "golem-secrets"


def env_file(path: Path) -> dict[str, str]:
    pairs = (line.split("=", 1) for line in path.read_text().splitlines() if line)
    return {key: value for key, value in pairs}


def test_the_example_overlay_leaves_no_placeholder() -> None:
    rendered = yaml.safe_dump_all(render(EXAMPLE_OVERLAY))

    assert PLACEHOLDERS.findall(rendered) == []


def test_new_runs_use_the_image_the_example_overlay_deploys() -> None:
    objects = render(EXAMPLE_OVERLAY)
    job_image = find(objects, "ConfigMap", "golem-tasks-env")["data"]["GOLEM_JOB_IMAGE"]

    assert {container(d)["image"] for d in objects if d["kind"] == "Deployment"} == {job_image}


@pytest.mark.parametrize("name", SETTINGS)
def test_the_generated_secrets_and_the_example_overlay_satisfy_each_parser(
    name: str, generated: Path
) -> None:
    objects = render(EXAMPLE_OVERLAY)
    deployment = find(objects, "Deployment", name)
    env: dict[str, str] = {}
    for source in container(deployment)["envFrom"]:
        if "configMapRef" in source:
            env |= find(objects, "ConfigMap", source["configMapRef"]["name"])["data"]
        else:
            env |= env_file(generated / f"{source['secretRef']['name']}.env")

    SETTINGS[name](env)


def test_the_generated_keys_are_what_the_processes_accept(generated: Path) -> None:
    parse_signing_key((generated / "run-token-key.pem").read_text(), "golem-1")
    parse_signing_key(
        (generated / "card-signing-key.pem").read_text(),
        "golem-cards-1",
        variable="GOLEM_CARD_SIGNING_KEY_FILE",
    )
    tasks = env_file(generated / "golem-tasks.env")
    Fernet(tasks["GOLEM_PUSH_CONFIG_KEY"])
    assert env_file(generated / "golem-edge.env")["GOLEM_EDGE_TOKEN"] == tasks["GOLEM_EDGE_TOKEN"]


def test_the_generated_run_secret_serves_a_run(generated: Path) -> None:
    run = env_file(generated / "golem-run-secrets.env")

    assert {name for name, value in run.items() if value} >= set(GATEWAY_SETTINGS)
    assert model_timeout(run) == 120


def test_the_lead_example_prints_what_the_guide_shows() -> None:
    guide = (DOCS / "guide" / "writing-an-agent.md").read_text(encoding="utf-8")
    code = run_blocks(DOCS / "guide" / "writing-an-agent.md")["lead"]
    [expected] = re.findall(r"<!-- run: lead -->\n```sh\n.*?```\n\n```\n(.*?)```", guide, re.DOTALL)
    script = code.replace("uv run python", shlex.quote(sys.executable))

    result = subprocess.run(
        ["bash", "-c", script], cwd=ROOT, capture_output=True, text=True, check=True
    )

    assert result.stdout == expected
