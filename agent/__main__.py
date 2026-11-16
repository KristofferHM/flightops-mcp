"""Command line for the agent loop.

    uv run python -m agent scenarios
    uv run python -m agent run --scenario rebook-approved --backend offline --out run.json
    uv run python -m agent hash run.json

`run` prints one JSON line: where the transcript went, its hash, how the run
ended, and what the mock operations API counted. `hash` verifies a transcript
and prints its hash, which is what CI compares between runs.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from flightops_mcp.telemetry import configure_logging

from .runner import BackendUnavailable, build_provider, run_clock, run_scenario
from .scenarios import ScenarioInvalid, load_scenario, scenario_ids
from .transcript import BACKENDS, TranscriptInvalid, load


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m agent")
    commands = parser.add_subparsers(dest="command", required=True)

    run = commands.add_parser("run", help="run one scenario and write its transcript")
    run.add_argument("--scenario", required=True, help="a bundled scenario id, or a .json path")
    run.add_argument("--backend", default="offline", choices=BACKENDS)
    run.add_argument("--out", type=Path, help="transcript path (default: agent-runs/)")

    check = commands.add_parser("hash", help="verify a transcript and print its content hash")
    check.add_argument("path", type=Path)

    commands.add_parser("scenarios", help="list the bundled scenarios")

    args = parser.parse_args(argv)
    if args.command == "scenarios":
        print("\n".join(scenario_ids()))
        return 0
    if args.command == "hash":
        return _hash(args.path)
    return asyncio.run(_run(args.scenario, args.backend, args.out))


async def _run(scenario_name: str, backend: str, out: Path | None) -> int:
    # The server logs to stdout by default. Here stdout is the result, so the
    # logs go to stderr, and only the ones an operator would act on.
    configure_logging("WARNING")
    logging.basicConfig(format="%(message)s", stream=sys.stderr, level=logging.WARNING, force=True)
    try:
        scenario = load_scenario(scenario_name)
        provider = build_provider(backend, scenario, os.environ)
    except (ScenarioInvalid, BackendUnavailable) as exc:
        return _refuse("cannot_run", str(exc))

    # The one place a wall clock is named. An offline run does not use it.
    clock = run_clock(backend, scenario, time.time)
    result = await run_scenario(scenario, provider, clock=clock)
    path = out or Path("agent-runs") / f"{scenario.id}.{backend}.json"
    result.transcript.write(path)
    transcript = result.transcript
    _emit(
        {
            "ok": True,
            "scenario": transcript.scenario_id,
            "backend": transcript.backend,
            "model": transcript.model,
            "transcript": path.as_posix(),
            "content_hash": transcript.content_hash,
            "outcome": {name: transcript.outcome[name] for name in ("status", "reason", "detail")},
            "tools_called": transcript.tool_sequence(),
            "budget": transcript.budget,
            "ops_api": result.ops_api,
        }
    )
    return 0


def _hash(path: Path) -> int:
    try:
        transcript = load(path)
    except OSError as exc:
        return _refuse("unreadable", f"{path}: {type(exc).__name__}")
    except TranscriptInvalid as exc:
        return _refuse("invalid_transcript", "; ".join(exc.problems))
    print(transcript.content_hash)
    return 0


def _refuse(code: str, message: str) -> int:
    _emit({"ok": False, "error": {"code": code, "message": message}}, stream=sys.stderr)
    return 2


def _emit(payload: dict[str, Any], *, stream: Any = None) -> None:
    print(json.dumps(payload, sort_keys=True), file=stream or sys.stdout)


if __name__ == "__main__":
    sys.exit(main())
