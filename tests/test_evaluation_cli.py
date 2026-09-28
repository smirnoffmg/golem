import io
import json
import shutil
from dataclasses import dataclass
from pathlib import Path

import pytest

from golem.evaluation.cli import ConfigError, deepagents_runner, main
from golem.runtime.ports import Brief, RoleResult

EXAMPLES = Path(__file__).parent.parent / "examples"
CASE = """\
goal: Work the discovery backlog
expect:
  outcome: proposed
  role: researcher
  target: H-2
  checks:
    sections_filled: {H-2: [Evidence]}
"""


@dataclass(frozen=True)
class Researcher:
    stray: bool = False

    async def run(self, brief: Brief) -> RoleResult:
        brief.target_path.write_text(brief.target_text + "- Interviews: 5 of 7 teams.\n")
        if self.stray:
            (brief.workspace / "notes.md").write_text("scratch\n")
        return RoleResult(summary="done")


@dataclass(frozen=True)
class Outputs:
    code: int
    out: str
    err: str


@pytest.fixture
def catalog(tmp_path: Path) -> Path:
    catalog_dir = Path(shutil.copytree(EXAMPLES / "discovery", tmp_path / "catalog"))
    shutil.rmtree(catalog_dir / "evals")
    case_dir = catalog_dir / "evals" / "researcher"
    shutil.copytree(EXAMPLES / "context" / "hypotheses", case_dir / "context" / "hypotheses")
    (case_dir / "case.yaml").write_text(CASE)
    return catalog_dir


def cli(argv: list[str], runner: Researcher | None = None, environ=None) -> Outputs:
    out, err = io.StringIO(), io.StringIO()
    factory = deepagents_runner if runner is None else (lambda *_: runner)
    code = main(argv, environ or {}, factory, out, err)
    return Outputs(code, out.getvalue(), err.getvalue())


def run_args(catalog: Path, *extra: str) -> list[str]:
    return ["run", "--catalog", str(catalog), "--cases", str(catalog / "evals"), *extra]


def test_a_passing_gate_exits_zero_and_writes_the_report(catalog, tmp_path):
    report = tmp_path / "out" / "report.json"

    result = cli(run_args(catalog, "--report", str(report)), Researcher())

    assert result.code == 0, result.err
    assert "researcher  pass" in result.out
    assert result.out.rstrip().endswith("Gate: passed")
    data = json.loads(report.read_text())
    assert data["passed"] is True
    assert data["threshold"] == 0.8
    assert [case["id"] for case in data["cases"]] == ["researcher"]


def test_a_failing_gate_exits_one(catalog):
    result = cli(run_args(catalog), Researcher(stray=True))

    assert result.code == 1
    assert "researcher  FAIL" in result.out
    assert "Gate: FAILED" in result.out


def test_a_regression_against_the_baseline_exits_one(catalog, tmp_path):
    baseline = tmp_path / "baseline.json"
    baseline.write_text('{"researcher": true}')

    result = cli(
        run_args(catalog, "--threshold", "0", "--baseline", str(baseline)), Researcher(stray=True)
    )

    assert result.code == 1
    assert "Regressions: researcher." in result.out


def test_a_missing_baseline_leaves_only_the_threshold(catalog, tmp_path):
    result = cli(run_args(catalog, "--baseline", str(tmp_path / "none.json")), Researcher())

    assert result.code == 0
    assert "no baseline" in result.err


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda c, t: ["run", "--catalog", str(c), "--cases", str(t / "no")], "not a directory"),
        (lambda c, t: ["run", "--catalog", str(t), "--cases", str(c / "evals")], "agent.yaml"),
        (lambda c, t: [*run_args(c), "--threshold", "1.5"], "between 0 and 1"),
        (lambda c, t: [*run_args(c), "--baseline", str(c / "agent.yaml")], "not valid JSON"),
    ],
)
def test_configuration_errors_exit_two(catalog, tmp_path, change, message):
    result = cli(change(catalog, tmp_path), Researcher())

    assert result.code == 2
    assert message in result.err


def test_usage_errors_exit_two():
    assert cli([]).code == 2
    assert cli(["run", "--catalog", "x"]).code == 2
    assert cli(["judge"]).code == 2


def test_the_production_runner_needs_the_model_gateway_settings(catalog):
    result = cli(run_args(catalog))

    assert result.code == 2
    assert "GOLEM_MODEL_GATEWAY_URL, GOLEM_MODEL_KEY, GOLEM_MODEL" in result.err


def test_the_production_runner_is_deepagents_on_the_gateway():
    runner = deepagents_runner(
        {
            "GOLEM_MODEL_GATEWAY_URL": "http://gateway:4000/v1",
            "GOLEM_MODEL_KEY": "key",
            "GOLEM_MODEL": "discovery-model",
        }
    )

    assert type(runner).__name__ == "DeepAgentsRunner"


def test_missing_gateway_settings_are_all_named():
    with pytest.raises(ConfigError, match=r"GOLEM_MODEL_KEY, GOLEM_MODEL$"):
        deepagents_runner({"GOLEM_MODEL_GATEWAY_URL": "http://gateway"})


def test_baseline_is_written_from_a_report(catalog, tmp_path):
    report, baseline = tmp_path / "report.json", tmp_path / "evals" / "baseline.json"
    cli(run_args(catalog, "--report", str(report)), Researcher())

    result = cli(["baseline", "--report", str(report), "--out", str(baseline)])

    assert result.code == 0
    assert json.loads(baseline.read_text()) == {"researcher": True}


def test_baseline_from_a_missing_or_malformed_report_exits_two(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("[]")

    missing = cli(["baseline", "--report", str(tmp_path / "no.json"), "--out", "b.json"])
    malformed = cli(["baseline", "--report", str(bad), "--out", str(tmp_path / "b.json")])

    assert (missing.code, malformed.code) == (2, 2)
    assert "not an evaluation report" in malformed.err


def test_the_production_runner_gets_the_platform_tools_from_the_environment(tmp_path) -> None:
    from golem.evaluation.cli import deepagents_runner

    registry = tmp_path / "mcp-registry.yaml"
    registry.write_text("tracker.read:\n  url: http://mcp-tracker.test/mcp\n  tools: [get_issue]\n")
    runner = deepagents_runner(
        {
            "GOLEM_MODEL_GATEWAY_URL": "http://gateway.test/v1",
            "GOLEM_MODEL_KEY": "k",
            "GOLEM_MODEL": "m",
            "GOLEM_MCP_REGISTRY": str(registry),
            "GOLEM_RUN_TOKEN": "run-token",
        }
    )

    assert [group.name for group in runner.toolbox.registry.groups] == ["tracker.read"]
    assert runner.toolbox.run_token == "run-token"


async def test_the_production_runner_records_delegation_instead_of_calling_the_edge(tmp_path):
    from golem.catalog import DELEGATE_GROUP, Neighbour, Role
    from golem.evaluation.delegations import DelegationRecorder

    registry = tmp_path / "mcp-registry.yaml"
    registry.write_text(
        f"{DELEGATE_GROUP}:\n  url: http://edge.test/a2a\n  tools: [delegate_to_agent]\n"
    )
    recorder = DelegationRecorder()
    runner = deepagents_runner(
        {
            "GOLEM_MODEL_GATEWAY_URL": "http://gateway.test/v1",
            "GOLEM_MODEL_KEY": "k",
            "GOLEM_MODEL": "m",
            "GOLEM_MCP_REGISTRY": str(registry),
        },
        recorder,
    )
    role = Role(name="planner", writes="plans/", tools=(DELEGATE_GROUP,))

    [tool] = await runner.toolbox.tools_for(role, (Neighbour(agent="checker", when="w"),))
    await tool.ainvoke({"agent": "checker", "goal": "check"})

    assert recorder.take() == ("checker",)
