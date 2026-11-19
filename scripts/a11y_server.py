#!/usr/bin/env python
"""The real board over synthetic tool answers, for scripts/a11y.mjs.

    uv run python scripts/a11y_server.py --scenario data --port 8791

Only the tools are replaced. The app, the gate, the display rules and the page are the ones
that ship. `--replay` cannot serve this purpose: a recording always shows its own time, so
the "no data yet" badge is unreachable through it. Nothing here opens a connection.
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

import uvicorn
from fastapi import FastAPI

from dashboard.__main__ import BOARD_SIZE, SEED_CSV
from dashboard.app import create_app
from dashboard.gate import LOCAL_BIND, Gate
from dashboard.service import BoardService
from flightops_mcp.airports import AirportDB, build_from_csv

Payload = dict[str, Any]

# `no_time` is the data scenario from upstreams that send no `Date`: METARs are on the
# page, and no age is claimed for them.
SCENARIOS: Final = ("data", "empty", "no_time")
# The last entry has no category, so the unknown marker and its wording are on the page too.
CATEGORIES: Final = ("VFR", "MVFR", "IFR", "LIFR", None)
FRESH_AGE_S: Final = 240
OLD_AGE_S: Final = 9000


class SyntheticSource:
    """Answers in the tools' own shape. `empty` is a board nothing has reported to yet."""

    recorded_at: None = None

    def __init__(self, scenario: str, clock: Callable[[], float] = time.time) -> None:
        self._scenario = scenario
        self._clock = clock

    def _as_of(self) -> str:
        return datetime.fromtimestamp(self._clock(), UTC).isoformat()

    def _upstream_time(self) -> int | None:
        """The upstream's `Date`, as the tools carry it. None when it sent none."""
        return None if self._scenario == "no_time" else int(self._clock())

    def _weather(self, airport: str) -> Payload:
        if self._scenario == "empty":
            return {
                "ok": True,
                "upstream_time_epoch": self._upstream_time(),
                "metar": [],
                "latest_metar": {"age_s": None, "current": False},
                "unknowns": ["No METAR has been returned for this airport."],
            }
        category = CATEGORIES[sum(map(ord, airport)) % len(CATEGORIES)]
        age = FRESH_AGE_S if category else OLD_AGE_S
        dated = self._upstream_time() is not None
        return {
            "ok": True,
            "upstream_time_epoch": self._upstream_time(),
            "metar": [
                {
                    "flight_category": category,
                    "observation_time": self._as_of(),
                    "raw_text": f"{airport} 182320Z 24008KT 9999 BKN030 08/05 Q1008",
                }
            ],
            "latest_metar": {
                "age_s": age if dated else None,
                "current": dated and category is not None,
            },
            "unknowns": [],
        }

    def _traffic(self) -> Payload:
        heard = int(self._clock()) - 30
        aircraft = [
            {
                "icao24": "4ca1f2",
                "callsign": "TEST1",
                "latitude": 57.7,
                "longitude": 12.3,
                "on_ground": False,
                "true_track_deg": 90.0,
                "last_contact": heard,
            }
        ]
        return {
            "ok": True,
            "upstream_time_epoch": self._upstream_time(),
            "counts": {"returned": len(aircraft)},
            "snapshot_current": True,
            "aircraft": aircraft,
            "unknowns": ["Coverage near the ground is not proven."],
        }

    def _status(self) -> Payload:
        return {
            "credentials": {"opensky": "client_credentials"},
            "budget": {"credits_spent_this_hour": 4, "local_budget_per_hour": 400},
            "cache": {"hit_rate": 0.5},
        }

    async def call(self, tool: str, arguments: Payload) -> Payload:
        if tool == "airport_weather":
            return self._weather(arguments["airport"])
        if tool == "airport_live_traffic":
            return self._traffic()
        if tool == "upstream_status":
            return self._status()
        # Never an invented result: the states under test do not call the other tools.
        return {
            "ok": False,
            "error": {
                "code": "not_synthesised",
                "message": f"The accessibility fixture does not answer {tool}.",
                "hint": "Use the real board for this.",
            },
        }


def listed_airports() -> list[Payload]:
    """The airports the real board lists, so the page holds its real names and lengths."""
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
        database = Path(directory) / "airports.sqlite3"
        build_from_csv(SEED_CSV, database)
        return AirportDB(database).board(limit=BOARD_SIZE)


def build_app(scenario: str, passcode: str | None) -> FastAPI:
    service = BoardService(SyntheticSource(scenario), listed_airports())
    return create_app(service, Gate(passcode))


def _exit_when_parent_closes_stdin() -> None:
    # Killing `uv run` does not reliably reach this process on Windows. A closed pipe does.
    sys.stdin.read()
    os._exit(0)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", choices=SCENARIOS, required=True)
    parser.add_argument("--port", type=int, required=True)
    args = parser.parse_args(argv)
    threading.Thread(target=_exit_when_parent_closes_stdin, daemon=True).start()
    app = build_app(args.scenario, os.environ.get("A11Y_PASSCODE"))
    uvicorn.run(app, host=LOCAL_BIND, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
