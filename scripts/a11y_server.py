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
import random
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

from dashboard.__main__ import WORLD_CSV, load_airports
from dashboard.app import create_app
from dashboard.gate import LOCAL_BIND, Gate
from dashboard.service import BoardService
from flightops_mcp.config import Settings
from flightops_mcp.server import AIRSPACE_COLUMNS, DESTINATION_NO_SOURCE, WAYPOINT_COLUMNS

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

    def __init__(
        self, scenario: str, clock: Callable[[], float] = time.time, aircraft: int = 0
    ) -> None:
        self._scenario = scenario
        self._clock = clock
        # Extra synthetic aircraft for measuring clutter and frame time (docs/DASHBOARD.md).
        self._extra = aircraft

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

    def _overview(self, airports: list[str]) -> Payload:
        return {
            "ok": True,
            "upstream_time_epoch": self._upstream_time(),
            "airports": {code: self._weather(code) for code in airports},
        }

    def _airspace(self) -> Payload:
        heard = int(self._clock()) - 20
        rows: list[list[Any]] = [
            ["4ca1f2", "TEST1", 57.7, 12.3, 3000.0, False, 200.0, 90.0, heard],
            ["4ca1f3", "TEST2", 52.3, 4.8, 9000.0, False, 230.0, 180.0, heard],
        ]
        rows.extend(self._crowd(heard))
        return {
            "ok": True,
            "upstream_time_epoch": self._upstream_time(),
            "region": "europe",
            "credentials": "client_credentials",
            "snapshot_time_epoch": heard,
            "snapshot_current": True,
            "columns": list(AIRSPACE_COLUMNS),
            "rows": rows,
            "bookkeeping": {"credits_spent": 4},
            "unknowns": [],
        }

    def _crowd(self, heard: int) -> list[list[Any]]:
        """`--aircraft` more, seeded, over Europe, with every gap the real feed has."""
        rng = random.Random(1183)  # noqa: S311 - a seeded fixture, not a secret
        prefixes = ("SAS", "NOZ", "RYR", "DLH", "BAW", "QQQ")
        rows = []
        for n in range(self._extra):
            altitude = None if n % 11 == 0 else round(rng.uniform(0, 12_000))
            rows.append(
                [
                    f"{0x500000 + n:06x}",
                    f"{prefixes[n % len(prefixes)]}{100 + n}",
                    round(rng.uniform(36, 70), 4),
                    round(rng.uniform(-20, 40), 4),
                    altitude,
                    n % 17 == 0,
                    round(rng.uniform(60, 260), 1),
                    None if n % 23 == 0 else round(rng.uniform(0, 360)),
                    heard - rng.randint(0, 150),
                ]
            )
        return rows

    def _track(self, icao24: str, estimate: bool) -> Payload:
        """A transit of eight waypoints ending where the aircraft is, no estimate on record."""
        now = int(self._clock())
        path = [
            [now - 3600 + 450 * n, 55.0 + n * 0.35, 8.0 + n * 0.6, 9000.0, 40.0, False]
            for n in range(8)
        ]
        return {
            "ok": True,
            "upstream_time_epoch": self._upstream_time(),
            "icao24": icao24,
            "transit": {
                "state": "observed",
                "callsign": "TEST1",
                "columns": list(WAYPOINT_COLUMNS),
                "waypoints": path,
                "first_waypoint_epoch": path[0][0],
                "last_waypoint_epoch": path[-1][0],
                "received": len(path),
                "unreadable": 0,
            },
            "estimated_arrival": (
                {"state": "no_record_for_this_flight", "previous_flight": None}
                if estimate
                else {"state": "not_requested"}
            ),
            "destination": {"state": "no_source", "text": DESTINATION_NO_SOURCE},
            "bookkeeping": {"credits_spent": 34 if estimate else 4},
            "unknowns": [],
        }

    def _triage(self, airport: str) -> Payload:
        """A triage with every panel field filled, so the walk meets the longest panel."""
        upstream = self._upstream_time()
        return {
            "ok": True,
            "assessment": {
                "severity": "elevated",
                "reasons": [f"{airport}: flight category IFR (in 3 of 3 METARs from the last 2 h)"],
                "why_not_higher": [
                    "1 distinct reason(s); high needs 3.",
                    "Gusts 18 kt, below the 35 kt flag.",
                    "12 aircraft in the search box, not more than 25.",
                ],
                "why_not_lower": [
                    "Low needs no reason; this triage has 1.",
                    f"{airport}: flight category IFR (in 3 of 3 METARs from the last 2 h)",
                ],
                "unknowns": [],
                "legs_without_evidence": [],
            },
            "counts": {
                "arrivals": {"count": 7, "received": 7, "unreadable": 0},
                "departures": {"count": 9, "received": 10, "unreadable": 1},
                "movements_window_minutes": 60,
                "aircraft_in_search_box": {
                    "count": 12,
                    "received": 12,
                    "unreadable": 0,
                    "listed": 12,
                    "airborne": 9,
                    "on_ground": 3,
                    "truncated": False,
                },
                "search_radius_nm": 40.0,
            },
            "weather": {
                "category": "IFR",
                "current": True,
                "age_s": FRESH_AGE_S if upstream else None,
                "ceiling": {"ft": 800, "basis": "OVC"},
                "visibility": {"statute_mi": 2.5, "at_least": False},
                "wind": {"direction_deg": 240, "variable": False, "speed_kt": 12, "gust_kt": 18},
            },
            "legs": {
                leg: {
                    "status": "observed",
                    "source": source,
                    "age_s": 300 if upstream else None,
                    "error": None,
                }
                for leg, source in (
                    ("weather", "AviationWeather.gov (NOAA) /metar"),
                    ("traffic", "OpenSky Network /states/all"),
                    ("movements", "OpenSky Network /flights/arrival, /flights/departure"),
                )
            },
            "evidence": {"movements_summary": {"arrivals": 7, "departures": 9}},
        }

    def _status(self) -> Payload:
        return {
            "credentials": {"opensky": "client_credentials"},
            "budget": {"credits_spent_this_hour": 4, "local_budget_per_hour": 400},
            "cache": {"hit_rate": 0.5},
        }

    async def call(self, tool: str, arguments: Payload) -> Payload:
        answers: dict[str, Callable[[], Payload]] = {
            "airport_weather": lambda: self._weather(arguments["airport"]),
            "airport_live_traffic": self._traffic,
            "upstream_status": self._status,
            "weather_overview": lambda: self._overview(arguments["airports"]),
            "airspace_snapshot": self._airspace,
            "disruption_triage": lambda: self._triage(arguments["airport"]),
            "aircraft_track": lambda: self._track(
                arguments["icao24"], bool(arguments.get("estimate_arrival"))
            ),
        }
        if tool in answers:
            return answers[tool]()
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
        settings = Settings(_env_file=None, airports_db=database)  # type: ignore[call-arg]
        return load_airports(settings, WORLD_CSV)


def build_app(scenario: str, passcode: str | None, aircraft: int = 0) -> FastAPI:
    service = BoardService(SyntheticSource(scenario, aircraft=aircraft), listed_airports())
    return create_app(service, Gate(passcode))


def _exit_when_parent_closes_stdin() -> None:
    # Killing `uv run` does not reliably reach this process on Windows. A closed pipe does.
    sys.stdin.read()
    os._exit(0)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", choices=SCENARIOS, required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--aircraft", type=int, default=0, help="extra synthetic aircraft")
    args = parser.parse_args(argv)
    threading.Thread(target=_exit_when_parent_closes_stdin, daemon=True).start()
    app = build_app(args.scenario, os.environ.get("A11Y_PASSCODE"), args.aircraft)
    uvicorn.run(app, host=LOCAL_BIND, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
