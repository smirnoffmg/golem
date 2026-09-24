"""``python -m golem.evaluation``: the quality gate of a catalog merge request.

``run`` evaluates the catalog over its golden set and exits 0 when the gate passes, 1 when it
fails and 2 on a usage or configuration error. ``baseline`` turns a report into the baseline
the next merge requests are compared with.
"""

import argparse
import asyncio
import contextlib
import json
import sys
import tempfile
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import TextIO

from golem.evaluation.cases import CaseError, load_cases
from golem.evaluation.gate import BaselineError, judge, parse_baseline
from golem.evaluation.report import ReportError, baseline_of, format_table, report_data
from golem.evaluation.run import CATALOG_FILE, run_cases
from golem.runtime.ports import RoleRunner

GATEWAY_SETTINGS = ("GOLEM_MODEL_GATEWAY_URL", "GOLEM_MODEL_KEY", "GOLEM_MODEL")
DEFAULT_THRESHOLD = 0.8
PASSED, FAILED, USAGE = 0, 1, 2

RunnerFactory = Callable[[Mapping[str, str]], RoleRunner]


class ConfigError(ValueError):
    pass


def deepagents_runner(environ: Mapping[str, str]) -> RoleRunner:
    missing = [name for name in GATEWAY_SETTINGS if not environ.get(name, "").strip()]
    if missing:
        raise ConfigError(f"missing model gateway settings: {', '.join(missing)}")
    # Imported here so that `baseline` and the tests do not load the agent stack.
    from golem.runtime.deepagents_runner import DeepAgentsRunner, gateway_model

    return DeepAgentsRunner(model=gateway_model(environ))


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="python -m golem.evaluation", description=__doc__)
    commands = root.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="evaluate a catalog over its golden set")
    run.add_argument("--catalog", type=Path, required=True, help="catalog directory (agent.yaml)")
    run.add_argument("--cases", type=Path, required=True, help="golden set directory")
    run.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD)
    run.add_argument("--baseline", type=Path, help="baseline JSON; a missing file is no baseline")
    run.add_argument("--report", type=Path, help="where to write the JSON report")
    baseline = commands.add_parser("baseline", help="write a baseline from a report")
    baseline.add_argument("--report", type=Path, required=True)
    baseline.add_argument("--out", type=Path, required=True)
    return root


def main(
    argv: Sequence[str],
    environ: Mapping[str, str],
    runner_factory: RunnerFactory = deepagents_runner,
    out: TextIO = sys.stdout,
    err: TextIO = sys.stderr,
) -> int:
    try:
        with contextlib.redirect_stderr(err):
            args = parser().parse_args(argv)
    except SystemExit as exit:
        return USAGE if exit.code else PASSED
    try:
        if args.command == "baseline":
            return write_baseline(args.report, args.out)
        return evaluate(args, environ, runner_factory, out, err)
    except (ConfigError, CaseError, BaselineError, ReportError, OSError) as error:
        print(f"error: {error}", file=err)
        return USAGE


def evaluate(
    args: argparse.Namespace,
    environ: Mapping[str, str],
    runner_factory: RunnerFactory,
    out: TextIO,
    err: TextIO,
) -> int:
    if not 0 <= args.threshold <= 1:
        raise ConfigError(f"--threshold must be between 0 and 1, got {args.threshold}")
    if not (args.catalog / CATALOG_FILE).is_file():
        raise ConfigError(f"{args.catalog / CATALOG_FILE}: missing: --catalog is not a catalog")
    cases = load_cases(args.cases)
    baseline = read_baseline(args.baseline, err)
    runner = runner_factory(environ)
    with tempfile.TemporaryDirectory(prefix="golem-evaluation-") as workdir:
        results = asyncio.run(run_cases(cases, args.catalog, runner, Path(workdir)))
    verdict = judge({r.case_id: r.passed for r in results}, args.threshold, baseline)
    print(format_table(results, verdict), file=out)
    if args.report:
        write_json(args.report, report_data(results, verdict))
    return PASSED if verdict.passed else FAILED


def read_baseline(path: Path | None, err: TextIO) -> dict[str, bool] | None:
    if path is None:
        return None
    if not path.is_file():
        print(f"{path}: no baseline yet, only the threshold applies", file=err)
        return None
    return parse_baseline(path.read_text(encoding="utf-8"), str(path))


def write_baseline(report: Path, out: Path) -> int:
    try:
        data = json.loads(report.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ReportError(f"{report}: not valid JSON: {error}") from error
    write_json(out, dict(sorted(baseline_of(data, str(report)).items())))
    return PASSED


def write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
