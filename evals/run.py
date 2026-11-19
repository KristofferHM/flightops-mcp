"""Run the golden scenarios and score them.

    uv run python -m evals.run --offline                     # what CI runs
    uv run python -m evals.run --offline --update-baseline   # record a baseline, on purpose
    uv run python -m evals.run --backend anthropic --out run.json   # a model run, by hand
    uv run python -m evals.run --backend openai-compatible --repeat 5   # a distribution

`--offline` is the gate. It uses the scripted backend, so it needs no model and
no network, and it exits non-zero when:

* a score is more than `--max-regression` points below `evals/baseline.json`,
* more than `--max-regressed-scenarios` scenarios fail a check they passed there,
* the scenarios are not the ones the baseline was recorded for,
* the README's table is not what the baseline renders to,
* or the run took longer than `--max-seconds`.

A model run is never compared with the offline baseline and never writes the
baseline or the README: they are different things (docs/EVALS.md). `_record` is
the one place either file is written, and it refuses anything but a single
offline run.

`--repeat N` runs the scenarios N times and reports `median (min–max)` per axis.
Offline, each of the N runs still has to pass the gate.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

from agent.provider import Provider
from agent.runner import (
    BackendUnavailable,
    build_provider,
    requested_sampling,
    run_clock,
    run_scenario,
)
from agent.scenarios import ScenarioInvalid
from agent.transcript import BACKENDS, loads
from flightops_mcp.resilience import configure_breakers
from flightops_mcp.telemetry import configure_logging, redact

from . import baseline as baselines
from . import report
from .aggregate import aggregate
from .fixtures import build as build_fixture
from .scenarios import SCENARIO_DIR, EvalScenario, load_all
from .scoring import Evidence, score, summarise

RUN_SCHEMA: Final = "flightops.evals.run/v1"
REPEAT_SCHEMA: Final = "flightops.evals.repeat/v1"
README: Final = Path(__file__).parent.parent / "README.md"
MAX_SECONDS: Final = 60.0


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    backend = "offline" if args.offline else args.backend
    refused = _refused(args, backend)
    if refused:
        return _cannot(refused)

    # stdout is the result. The server's own logs go to stderr, and only when asked
    # for: a fixture that plays a dead upstream makes the server log exactly that.
    configure_logging("WARNING" if args.verbose else "CRITICAL")
    level = logging.WARNING if args.verbose else logging.CRITICAL
    logging.basicConfig(format="%(message)s", stream=sys.stderr, level=level, force=True)

    try:
        scenarios = load_all(args.scenarios)
        for scenario in scenarios:
            build_fixture(scenario.fixture)
    except ScenarioInvalid as exc:
        return _cannot(str(exc))

    recorded_on = args.date or datetime.now(UTC).date().isoformat()
    if args.repeat is not None:
        return _repeat(scenarios, backend, args, recorded_on)

    try:
        run = _one_run(scenarios, backend, args.transcripts, recorded_on)
    except BackendUnavailable as exc:
        return _cannot(str(exc))
    print(report.table(run))
    print(f"\nRan {len(run['results'])} scenarios in {run['duration_s']:.2f} s.")

    failures = _offline_failures(run, args)
    run["failures"] = failures
    if args.out:
        _write_json(args.out, run)
    if args.table_out:
        _write(args.table_out, (report.table(run) + "\n").encode())
    return _finish(failures, backend)


def _refused(args: argparse.Namespace, backend: str | None) -> str | None:
    """Why these arguments cannot run, or None."""
    if backend is None:
        return "say --offline, or --backend for a model run"
    if args.repeat is not None and args.update_baseline:
        return (
            "--repeat cannot be used with --update-baseline: the baseline is one offline run "
            "of the scripted policy, and a distribution over repeated runs is not that"
        )
    if args.update_baseline and backend != "offline":
        return "the baseline is an offline run; --update-baseline needs --offline"
    return None


def _repeat(
    scenarios: Sequence[EvalScenario], backend: str, args: argparse.Namespace, recorded_on: str
) -> int:
    """Run the scenarios `--repeat` times and report the distribution."""
    runs: list[dict[str, Any]] = []
    failures: list[str] = []
    started = time.perf_counter()
    try:
        sampling = requested_sampling(backend, os.environ)
        for number in range(1, args.repeat + 1):
            # One directory per run: the file names are per scenario, so a shared
            # directory would keep only the last run's transcripts.
            kept = args.transcripts / f"run-{number:02d}" if args.transcripts else None
            run = _one_run(scenarios, backend, kept, recorded_on)
            run["failures"] = _offline_failures(run, args)
            failures += [f"run {number}: {failure}" for failure in run["failures"]]
            runs.append(run)
            overall = run["summary"]["overall"]["score"]
            print(
                f"Run {number} of {args.repeat}: {overall:.2f} % of scenarios passed every check, "
                f"in {run['duration_s']:.2f} s.",
                flush=True,
            )
    except BackendUnavailable as exc:
        return _cannot(str(exc))

    document: dict[str, Any] = {
        "schema": REPEAT_SCHEMA,
        "backend": backend,
        "model": runs[0]["model"],
        "recorded_on": recorded_on,
        "repeat": args.repeat,
        "sampling": sampling,
        "duration_s": round(time.perf_counter() - started, 2),
        "aggregate": aggregate(runs),
        "runs": runs,
        "failures": failures,
    }
    table = report.repeat_table(document)
    print(f"\n{report.for_console(table)}")
    if args.out:
        _write_json(args.out, document)
    if args.table_out:
        _write(args.table_out, (table + "\n").encode())
    return _finish(failures, backend)


def _one_run(
    scenarios: Sequence[EvalScenario], backend: str, transcripts: Path | None, recorded_on: str
) -> dict[str, Any]:
    """Every scenario once, as a results document. Raises `BackendUnavailable`."""
    started = time.perf_counter()
    results = asyncio.run(run_all(scenarios, backend, os.environ, transcripts))
    run: dict[str, Any] = redact(
        {
            "schema": RUN_SCHEMA,
            "backend": backend,
            "model": results[0]["run"]["model"],
            "recorded_on": recorded_on,
            "duration_s": round(time.perf_counter() - started, 2),
            "summary": summarise(results),
            "results": results,
        }
    )
    return run


def _offline_failures(run: Mapping[str, Any], args: argparse.Namespace) -> list[str]:
    """Why this run fails the gate. A model run has no gate, so it cannot fail one."""
    if run["backend"] != "offline":
        return []
    failures = _gate(run, args)
    duration = run["duration_s"]
    if duration > args.max_seconds:
        failures.append(f"the run took {duration:.2f} s, over the {args.max_seconds:g} s limit")
    return failures


def _finish(failures: Sequence[str], backend: str) -> int:
    for failure in failures:
        print(f"FAIL: {failure}")
    if not failures and backend == "offline":
        print("OK: no regression against the baseline.")
    return 1 if failures else 0


def _record(run: Mapping[str, Any], args: argparse.Namespace, readme_text: str) -> list[str]:
    """Write the baseline and the README's table. The only code that writes either."""
    if run["backend"] != "offline" or args.repeat is not None:
        return ["only a single offline run can be recorded as the baseline"]
    recorded = baselines.record(run, run["recorded_on"])
    _write(args.baseline, baselines.dump(recorded))
    try:
        updated = report.with_block(readme_text, report.block(recorded))
    except ValueError as exc:
        return [str(exc)]
    _write(args.readme, updated.encode())
    print(f"Baseline recorded in {args.baseline.name}; README table rewritten.")
    return []


def _gate(run: Mapping[str, Any], args: argparse.Namespace) -> list[str]:
    """Compare an offline run with the baseline and the README. Returns why it fails."""
    try:
        readme_text = args.readme.read_text(encoding="utf-8")
    except OSError:
        return [f"there is no README at {args.readme} to carry the table"]
    if args.update_baseline:
        return _record(run, args, readme_text)

    try:
        recorded = baselines.load(args.baseline)
    except baselines.BaselineInvalid as exc:
        return [f"{exc}. Record one with --update-baseline."]
    verdict = baselines.compare(
        recorded,
        run,
        max_regression=args.max_regression,
        max_regressed_scenarios=args.max_regressed_scenarios,
    )
    for note in verdict.notes:
        print(f"note: {note}")
    failures = list(verdict.failures)
    if report.current_block(readme_text) != report.block(recorded):
        failures.append(
            f"the table in {args.readme.name} is not what the baseline renders to. It is "
            f"generated, never edited: {report.REGENERATE}"
        )
    return failures


async def run_all(
    scenarios: Sequence[EvalScenario],
    backend: str,
    env: Mapping[str, str],
    transcripts: Path | None = None,
) -> list[dict[str, Any]]:
    """Run every scenario, one after another, and score each."""
    return [await run_one(scenario, backend, env, transcripts) for scenario in scenarios]


async def run_one(
    scenario: EvalScenario,
    backend: str,
    env: Mapping[str, str],
    transcripts: Path | None = None,
) -> dict[str, Any]:
    provider = build_provider(backend, scenario.run, env)
    # The one place a wall clock is named for a run. An offline run does not use it.
    clock = run_clock(backend, scenario.run, time.time)
    return await run_with(scenario, provider, clock, transcripts=transcripts)


async def run_with(
    scenario: EvalScenario,
    provider: Provider,
    clock: Callable[[], float],
    *,
    transcripts: Path | None = None,
    manifests: dict[str, list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    """Run one scenario with this planner and score it. `manifests` is for tests."""
    # Breakers are one per upstream for the whole process. A scenario that plays a
    # dead upstream must not leave the next one an open circuit.
    configure_breakers(failure_threshold=5, recovery_timeout_s=30.0)
    upstreams, described = build_fixture(scenario.fixture)
    result = await run_scenario(
        scenario.run, provider, clock=clock, manifests=manifests or described, upstreams=upstreams
    )
    document = result.transcript.to_bytes()
    if transcripts is not None:
        _write(transcripts / f"{scenario.id}.{provider.name}.json", document)
    # Scored from the document, through the loader that checks its hash: the
    # harness consumes what a run writes down, not what it had in memory.
    evidence = Evidence(transcript=loads(document), ops_api=result.ops_api, offered=result.offered)
    return score(scenario, evidence)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m evals.run", description=__doc__.splitlines()[0]
    )
    parser.add_argument("--offline", action="store_true", help="scripted backend; the CI gate")
    parser.add_argument("--backend", choices=BACKENDS, help="run with this backend instead")
    parser.add_argument(
        "--update-baseline",
        action="store_true",
        help="record this run as the baseline and rewrite the README table",
    )
    parser.add_argument(
        "--repeat",
        type=_positive,
        metavar="N",
        help="run the scenarios N times and report median (min-max) per axis",
    )
    parser.add_argument("--scenarios", type=Path, default=SCENARIO_DIR, help="scenario directory")
    parser.add_argument("--baseline", type=Path, default=baselines.DEFAULT_PATH)
    parser.add_argument("--readme", type=Path, default=README)
    parser.add_argument("--out", type=Path, help="write the full results as JSON")
    parser.add_argument("--table-out", type=Path, help="write this run's table as Markdown")
    parser.add_argument("--transcripts", type=Path, help="keep every transcript in this directory")
    parser.add_argument("--max-seconds", type=float, default=MAX_SECONDS)
    parser.add_argument("--max-regression", type=float, default=baselines.MAX_REGRESSION)
    parser.add_argument(
        "--max-regressed-scenarios", type=int, default=baselines.MAX_REGRESSED_SCENARIOS
    )
    parser.add_argument("--date", help="the run's date, YYYY-MM-DD (default: today, UTC)")
    parser.add_argument("--verbose", action="store_true", help="show the server's warnings")
    return parser


def _positive(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        value = 0
    if value < 1:
        raise argparse.ArgumentTypeError(f"must be a whole number of at least 1, not {text!r}")
    return value


def _write_json(path: Path, document: Mapping[str, Any]) -> None:
    _write(path, (json.dumps(document, sort_keys=True, indent=2) + "\n").encode())


def _write(path: Path, content: bytes) -> None:
    # Bytes, not text: text mode would write "\r\n" on Windows.
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def _cannot(message: str) -> int:
    error = {"ok": False, "error": {"code": "cannot_run", "message": message}}
    print(json.dumps(error), file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
