"""The airport board, through its HTTP surface, against fake upstreams."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import time
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from pydantic import SecretStr

from dashboard import __main__ as cli
from dashboard import board
from dashboard.app import STATIC, create_app
from dashboard.gate import COOKIE_NAME, FAILURE_WINDOW_S, MAX_FAILURES, Gate, bind_host, host_name
from dashboard.service import BoardService
from dashboard.sources import (
    FORMAT,
    REPO_ROOT,
    LiveSource,
    RecordedSource,
    RecordingError,
    RecordingSource,
    default_directory,
)
from flightops_mcp.airports import AirportDB, build_from_csv
from flightops_mcp.config import Settings
from flightops_mcp.runtime import Runtime, set_runtime
from services.mock_upstream.dated import http_date

from .conftest import FIXTURES, FakeClock, LogCapture, SleepRecorder

NOW = int(datetime(2026, 11, 14, 23, 30, tzinfo=UTC).timestamp())
PASSCODE = "correct horse battery"
METAR = {
    "icaoId": "ESGG",
    "reportTime": "2026-11-14T23:20:00Z",
    "rawOb": "ESGG 142320Z 24008KT 9999 BKN030 08/05 Q1008",
    "fltCat": "VFR",
}
LOCAL = "http://127.0.0.1:8765"
AIRCRAFT = ["abc123", "SAS1 ", "Sweden", NOW, NOW - 60, 12.0, 57.7, 3000.0, False, 200.0, 90.0, 0.0]
# What this machine's clock read when the change was made: 21 min 27 s ahead of the world.
HOST_SKEW_S = 21 * 60 + 27


@dataclass
class Upstreams:
    metar: list[dict[str, Any]] = field(default_factory=list)
    states: list[list[Any]] = field(default_factory=list)
    calls: list[str] = field(default_factory=list)
    # The upstreams' own clock, sent as `Date`. None sends no `Date` at all.
    date: int | None = NOW
    snapshot: int = NOW

    def _headers(self) -> dict[str, str]:
        return {} if self.date is None else {"Date": http_date(self.date)}

    def opensky(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request.url.path)
        if "/flights/" in request.url.path:
            return httpx.Response(200, json=[])
        return httpx.Response(
            200,
            json={"time": self.snapshot, "states": self.states},
            headers={"X-Rate-Limit-Remaining": "3900", **self._headers()},
        )

    def weather(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request.url.path)
        if request.url.path.endswith("/metar") and self.metar:
            return httpx.Response(200, json=self.metar, headers=self._headers())
        return httpx.Response(204, headers=self._headers())

    def token(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request.url.path)
        return httpx.Response(200, json={"access_token": "fake", "expires_in": 1800})


@pytest.fixture(autouse=True)
def _clear_runtime() -> Iterator[None]:
    yield
    set_runtime(None)


@pytest.fixture
def database(tmp_path: Path) -> Path:
    path = tmp_path / "airports.sqlite3"
    build_from_csv(FIXTURES / "airports_sample.csv", path)
    return path


def airports(database: Path) -> list[dict[str, Any]]:
    return [a for a in AirportDB(database).board() if a["ident"] == "ESGG"]


def live_runtime(
    database: Path, upstream: Upstreams, *, host_clock: float = NOW, **overrides: Any
) -> Runtime:
    values: dict[str, Any] = {
        "opensky_client_id": "id",
        "opensky_client_secret": "secret",
        "airports_db": database,
        "backoff_base_s": 0.0,
        "max_retries": 0,
    }
    settings = Settings(_env_file=None, **{**values, **overrides})  # type: ignore[call-arg]
    runtime = Runtime.build(
        settings,
        opensky_transport=httpx.MockTransport(upstream.opensky),
        token_transport=httpx.MockTransport(upstream.token),
        weather_transport=httpx.MockTransport(upstream.weather),
        sleep=SleepRecorder(),
        clock=lambda: float(host_clock),
        data_clock=lambda: float(host_clock),
    )
    set_runtime(runtime)
    return runtime


def http(app: FastAPI, base_url: str = LOCAL) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=base_url)


def live_app(database: Path, passcode: str | None = None) -> FastAPI:
    return create_app(BoardService(LiveSource(), airports(database)), Gate(passcode))


@pytest.fixture
async def open_board(database: Path) -> AsyncIterator[httpx.AsyncClient]:
    async with http(live_app(database)) as client:
        yield client


# --- unknown is a rule ---------------------------------------------------------------
@pytest.mark.parametrize(
    ("payload", "summary"),
    [
        ({"ok": False, "error": {"code": "upstream_unavailable"}}, "weather unavailable"),
        ({"ok": True, "metar": [], "latest_metar": {"age_s": None}}, "no METAR"),
        (
            {
                "ok": True,
                "metar": [{"flight_category": "VFR"}],
                "latest_metar": {"age_s": None, "current": False},
            },
            "no readable time",
        ),
        (
            {
                "ok": True,
                "metar": [{"flight_category": "VFR"}],
                "latest_metar": {"age_s": 9000, "current": False},
            },
            "2 h 30 min old",
        ),
        (
            {
                "ok": True,
                "metar": [{"flight_category": "VFR"}],
                "latest_metar": {"age_s": -9000, "current": False},
            },
            "in the future",
        ),
        (
            {
                "ok": True,
                "metar": [{"flight_category": "GREEN"}],
                "latest_metar": {"age_s": 60, "current": True},
            },
            "no flight category",
        ),
    ],
)
def test_weather_without_a_current_category_is_an_unknown_marker(
    payload: dict[str, Any], summary: str
) -> None:
    view = board.weather_view({"upstream_time_epoch": NOW, **payload})

    assert view["state"] == "unknown"
    assert view["category"] is None
    assert view["summary"].startswith("Unknown")
    assert summary in view["summary"]
    assert board.marker(view) == {
        "kind": "unknown",
        "category": None,
        "label": "Unknown",
        "scope": "weather",
    }


def test_a_coloured_marker_claims_weather_and_nothing_more() -> None:
    view = board.weather_view(
        {
            "ok": True,
            "upstream_time_epoch": NOW,
            "metar": [
                {"flight_category": "IFR", "observation_time": "2026-11-14T22:00:00Z"},
                {"flight_category": "VFR", "observation_time": "2026-11-14T23:20:00Z"},
            ],
            "latest_metar": {"age_s": 600, "current": True},
        }
    )

    assert view["summary"] == "VFR, METAR 10 min old"
    assert board.marker(view)["label"] == "Weather VFR"
    assert board.marker(view)["scope"] == "weather"


async def test_no_metar_and_no_aircraft_never_renders_as_weather(
    database: Path, open_board: httpx.AsyncClient
) -> None:
    live_runtime(database, Upstreams())

    overview = (await open_board.get("/api/board")).json()
    detail = (await open_board.get("/api/airport/esgg")).json()
    triage = (await open_board.post("/api/airport/ESGG/triage")).json()

    (airport,) = overview["airports"]
    assert airport["marker"]["kind"] == "unknown"
    assert airport["marker"]["category"] is None
    assert airport["marker"]["label"] == "Unknown"
    assert overview["banner"] == {"mode": "NO DATA", "label": "NO DATA"}
    assert detail["traffic"]["summary"] == "none heard, coverage not proven"
    assert detail["movements"]["arrivals"] == "not requested"
    assert triage["label"] == "Unknown"
    assert triage["legs_without_evidence"] == ["weather", "traffic", "movements"]
    assert triage["movements"] == {
        "arrivals": "not available yet today",
        "departures": "not available yet today",
    }


def test_the_page_can_only_colour_the_four_categories() -> None:
    script = (STATIC / "board.js").read_text(encoding="utf-8")
    table = re.search(r"const CATEGORY = \{(.*?)\n  \};", script, re.DOTALL)

    assert table is not None
    assert re.findall(r"^\s+(\w+): \{ colour", table.group(1), re.MULTILINE) == [
        "VFR",
        "MVFR",
        "IFR",
        "LIFR",
    ]
    assert 'airport.marker.kind === "weather" ? CATEGORY' in script
    assert "drawUnknown(x, y, UNKNOWN_RADIUS * scale)" in script


def test_movement_and_triage_views_cover_their_failures() -> None:
    failed = board.triage_view({"ok": False, "error": {"message": "no"}})
    assert (failed["label"], failed["unknowns"]) == ("Unknown", ["no"])
    assert failed["movements"]["arrivals"] == "not available"

    partial = board.triage_view(
        {
            "ok": True,
            "assessment": {"severity": "elevated", "reasons": ["gusts"]},
            "evidence": {"movements_summary": {"arrivals": 3, "departures": 0}},
        }
    )
    assert partial["label"] == "Elevated"
    assert partial["movements"] == {"arrivals": "3", "departures": "not available yet today"}
    unmeasured = board.triage_view({"ok": True, "assessment": {}, "evidence": {}})
    assert unmeasured["movements"]["departures"] == "not available"


def test_aircraft_fade_with_age_and_an_unknown_age_is_faintest() -> None:
    assert board.fade(0) == 1.0
    assert board.fade(60) == 0.5
    assert board.fade(10_000) == board.MIN_FADE
    assert board.fade(None) == board.MIN_FADE
    assert board.fade(-5) == 1.0


def test_traffic_views_separate_failure_quiet_and_old() -> None:
    failed = board.traffic_view(
        {"ok": False, "error": {"code": "credentials_missing", "hint": "Set them."}}
    )
    assert failed["state"] == "unknown"
    assert failed["count"] is None
    assert failed["hint"] == "Set them."

    old = board.traffic_view(
        {
            "ok": True,
            "counts": {"returned": 1},
            "snapshot_current": False,
            "snapshot_time_epoch": NOW - 1300,
            "upstream_time_epoch": NOW,
            "aircraft": [{"icao24": "a", "last_contact": NOW - 1300}],
        }
    )
    assert old["state"] == "stale"
    assert old["aircraft"][0]["age_s"] == 1300, "measured from now, not from the snapshot"
    assert old["aircraft"][0]["fade"] == board.MIN_FADE

    undated = board.traffic_view(
        {"ok": True, "counts": {"returned": 1}, "aircraft": [{"last_contact": NOW}]}
    )
    assert undated["aircraft"][0]["age_s"] is None
    assert undated["aircraft"][0]["fade"] == board.MIN_FADE


def test_airspace_views_cover_every_state() -> None:
    columns = ["icao24", "latitude", "longitude", "last_contact"]
    base = {"ok": True, "columns": columns, "snapshot_time_epoch": NOW, "upstream_time_epoch": NOW}

    assert board.airspace_view({"ok": False, "error": {"code": "x"}})["state"] == "unknown"
    assert board.airspace_view({**base, "rows": []})["summary"] == board.NONE_HEARD
    rows = [["a", 1.0, 2.0, NOW]]
    assert board.airspace_view({**base, "rows": rows})["state"] == "stale"
    current = board.airspace_view({**base, "rows": rows, "snapshot_current": True})
    assert current["summary"] == "1 heard"
    assert current["aircraft"][0]["fade"] == 1.0


# --- the banner comes from the data ----------------------------------------------------
def test_banner_modes_are_computed_from_the_views() -> None:
    current = {"state": "reported", "age_s": 60}
    old = {"state": "unknown", "age_s": 9000}
    empty = {"state": "unknown", "age_s": None}

    assert board.banner([current, old], None)["label"] == "LIVE: 1 of 2 with a current METAR"
    assert board.banner([old, empty], None)["mode"] == "STALE"
    assert board.banner([empty], None)["mode"] == "NO DATA"
    assert board.banner([current], "2026-11-14T23:30:00+00:00") == {
        "mode": "RECORDED",
        "label": "RECORDED 2026-11-14T23:30:00+00:00",
    }


async def test_the_governance_strip_reports_mode_spend_and_upstream_count(
    database: Path, open_board: httpx.AsyncClient
) -> None:
    live_runtime(
        database,
        Upstreams(metar=[METAR], states=[AIRCRAFT]),
        dashboard_daily_credit_budget=50,
    )

    await open_board.get("/api/airport/ESGG")
    overview = (await open_board.get("/api/board")).json()

    assert overview["banner"]["mode"] == "LIVE"
    assert overview["airports"][0]["marker"]["category"] == "VFR"
    assert overview["governance"] == {
        "credentials": "client_credentials",
        "credits_hour": 1,
        "budget_hour": 400,
        "credits_24h": 1,
        "budget_24h": 50,
        "ledger_survives_restart": False,
        "cache_hit_rate": 0.333,
        "upstream_remaining": 3900,
    }


async def test_board_requests_only_reach_what_it_lists(
    database: Path, open_board: httpx.AsyncClient
) -> None:
    upstream = Upstreams()
    live_runtime(database, upstream)

    assert (await open_board.get("/api/airport/EGLL")).status_code == 404
    assert (await open_board.post("/api/airport/EGLL/triage")).status_code == 404
    assert (await open_board.get("/api/traffic/mars")).status_code == 404
    assert upstream.calls == []


async def test_wide_traffic_is_served_and_charged_to_the_daily_guard(
    database: Path, open_board: httpx.AsyncClient
) -> None:
    runtime = live_runtime(database, Upstreams(states=[AIRCRAFT]))

    layer = (await open_board.get("/api/traffic/europe")).json()

    assert layer["summary"] == "1 heard"
    assert layer["aircraft"][0]["fade"] == 0.5
    assert runtime.opensky.credits_spent_24h == 4


async def test_airport_detail_spends_one_credit_at_forty_miles(
    database: Path, open_board: httpx.AsyncClient
) -> None:
    runtime = live_runtime(database, Upstreams(states=[AIRCRAFT]))

    detail = (await open_board.get("/api/airport/ESGG")).json()

    assert detail["traffic"]["summary"] == "1 heard"
    assert runtime.opensky.credits_spent_24h == 1


# --- the last-update badge -------------------------------------------------------------
RECORDED_AT = "2026-11-14T23:30:40+00:00"


def seen(age_s: int | None) -> dict[str, Any]:
    return {"state": "reported", "age_s": age_s, "upstream_time": NOW}


def badge_texts(view: dict[str, Any]) -> list[str]:
    return [line["text"] for line in view["lines"]]


@pytest.mark.parametrize("weather", [[], [seen(None)], [seen(None), seen(None)]])
def test_the_badge_says_no_data_yet_until_a_report_has_a_time(
    weather: list[dict[str, Any]],
) -> None:
    view = board.last_update(weather, None)

    assert view["state"] == "no_data"
    assert view["lines"] == [
        {"source": "weather", "label": "Weather", "state": "no_data", "text": "no data yet"}
    ]


def test_the_badge_reports_the_freshest_weather_and_names_it() -> None:
    view = board.last_update([seen(3000), seen(None), seen(240)], None)

    assert view["state"] == "live"
    assert view["lines"] == [
        {
            "source": "weather",
            "label": "Weather",
            "state": "live",
            "text": "Last update 4 min ago",
        }
    ]


@pytest.mark.parametrize(
    ("age_s", "text"),
    [
        (0, "Last update under 1 min ago"),
        (59, "Last update under 1 min ago"),
        (60, "Last update 1 min ago"),
        (-120, "Last update under 1 min ago"),
    ],
)
def test_the_badge_words_short_ages_without_rounding_up(age_s: int, text: str) -> None:
    assert badge_texts(board.last_update([seen(age_s)], None)) == [text]


def test_weather_turns_stale_only_past_the_tools_own_threshold() -> None:
    limit = board.METAR_STALE_AFTER_S

    at_limit = board.last_update([seen(limit)], None)
    past_limit = board.last_update([seen(limit + 1)], None)

    assert limit == 5400
    assert (at_limit["state"], badge_texts(at_limit)) == ("live", ["Last update 90 min ago"])
    assert past_limit["state"] == "stale"
    assert past_limit["lines"][0]["state"] == "stale"
    assert badge_texts(past_limit) == ["stale: last update 90 min ago"]
    long_gone = board.last_update([seen(7500)], None)
    assert badge_texts(long_gone) == ["stale: last update 2 h 5 min ago"]


def test_a_report_dated_far_ahead_is_never_the_freshest() -> None:
    ahead = -board.METAR_STALE_AFTER_S - 1

    mixed = board.last_update([seen(ahead), seen(600)], None)
    alone = board.last_update([seen(ahead)], None)
    at_tolerance = board.last_update([seen(-board.METAR_STALE_AFTER_S)], None)

    assert badge_texts(mixed) == ["Last update 10 min ago"]
    assert (alone["state"], badge_texts(alone)) == ("stale", ["stale: dated in the future"])
    assert at_tolerance["state"] == "live"


def test_weather_and_traffic_age_separately() -> None:
    view = board.last_update([seen(240)], None, NOW - 1300, NOW)

    assert view["state"] == "stale", "one stale source warns the whole badge"
    assert [(line["label"], line["state"], line["text"]) for line in view["lines"]] == [
        ("Weather", "live", "Last update 4 min ago"),
        ("Traffic", "stale", "stale: last update 21 min ago"),
    ]


def test_traffic_turns_stale_only_past_the_snapshot_threshold() -> None:
    limit = board.STATES_STALE_AFTER_S

    at_limit = board.last_update([seen(60)], None, NOW - limit, NOW)
    past_limit = board.last_update([seen(60)], None, NOW - limit - 1, NOW)
    ahead = board.last_update([seen(60)], None, NOW + limit + 1, NOW)

    assert limit == 300
    assert at_limit["state"] == "live"
    assert at_limit["lines"][1]["text"] == "Last update 5 min ago"
    assert past_limit["lines"][1] == {
        "source": "traffic",
        "label": "Traffic",
        "state": "stale",
        "text": "stale: last update 5 min ago",
    }
    assert ahead["lines"][1]["text"] == "stale: dated in the future"


def test_traffic_age_is_measured_against_opensky_and_nothing_else() -> None:
    # A weather answer from a later NOAA clock must not age the traffic snapshot.
    later = {**seen(60), "upstream_time": NOW + 600}
    own_clock = board.last_update([seen(60), later], None, NOW - 120, NOW)
    no_clock = board.last_update([later], None, NOW - 120, None)

    assert own_clock["lines"][1]["text"] == "Last update 2 min ago"
    assert no_clock["lines"][1] == {
        "source": "traffic",
        "label": "Traffic",
        "state": "no_upstream_time",
        "text": "freshness unknown — no upstream time",
    }


def test_a_layer_without_a_snapshot_time_claims_no_age() -> None:
    view = board.last_update([seen(240)], None, 0)

    assert view["state"] == "live"
    assert view["lines"][1]["state"] == "unknown"
    assert view["lines"][1]["text"] == "snapshot time unknown"
    assert len(board.last_update([seen(240)], None)["lines"]) == 1


def test_a_recording_shows_its_own_time_and_no_age() -> None:
    view = board.last_update([seen(9000)], RECORDED_AT, NOW - 60)

    assert view["state"] == "recorded", "age during playback would describe the playback"
    assert [(line["label"], line["state"], line["text"]) for line in view["lines"]] == [
        ("Recorded", "recorded", "2026-11-14 23:30 UTC"),
        ("Traffic", "recorded", "snapshot 2026-11-14 23:29 UTC"),
    ]
    assert not any("ago" in text for text in badge_texts(view))
    undated = board.last_update([], RECORDED_AT, 0)
    assert undated["lines"][1]["text"] == board.SNAPSHOT_TIME_UNKNOWN


@pytest.mark.parametrize(
    ("recorded_at", "text"),
    [
        ("yesterday", "yesterday"),
        ("", "time unknown"),
        ("2026-11-14T23:30:00", "2026-11-14T23:30:00"),
        ("2026-11-15T00:30:00+01:00", "2026-11-14 23:30 UTC"),
    ],
)
def test_a_recording_time_is_never_guessed(recorded_at: str, text: str) -> None:
    assert badge_texts(board.last_update([], recorded_at)) == [text]


@pytest.mark.parametrize(
    ("epoch", "expected"),
    [(NOW, NOW), (None, 0), (0, 0), (-5, 0), ("soon", 0), (board.MAX_SNAPSHOT_EPOCH + 1, 0)],
)
def test_the_layer_only_hands_the_page_a_usable_snapshot_time(epoch: Any, expected: int) -> None:
    view = board.airspace_view(
        {"ok": True, "columns": [], "rows": [], "snapshot_time_epoch": epoch}
    )

    assert view["snapshot_epoch"] == expected


async def test_the_board_reports_both_ages_through_its_api(
    database: Path, open_board: httpx.AsyncClient
) -> None:
    live_runtime(database, Upstreams(metar=[METAR], states=[AIRCRAFT]))

    layer = (await open_board.get("/api/traffic/europe")).json()
    weather_only = (await open_board.get("/api/board")).json()["last_update"]
    both = (
        await open_board.get(
            "/api/board",
            params={
                "traffic_snapshot": layer["snapshot_epoch"],
                "traffic_upstream": layer["upstream_epoch"],
            },
        )
    ).json()["last_update"]
    failed_layer = (await open_board.get("/api/board?traffic_snapshot=0")).json()["last_update"]

    assert (layer["snapshot_epoch"], layer["upstream_epoch"]) == (NOW, NOW)
    assert badge_texts(weather_only) == ["Last update 10 min ago"]
    assert badge_texts(both) == ["Last update 10 min ago", "Last update under 1 min ago"]
    assert badge_texts(failed_layer)[1] == "snapshot time unknown"


# --- ages come from the upstream's clock, never this machine's ---------------------------
# Each test runs the real tools and the real board with this machine's clock 21 min 27 s
# fast, as measured when the change was made, and also patches `time.time` itself. The
# verdict must be the one a correct clock gives, because no clock of ours is consulted.
OBSERVED = NOW - 600  # METAR's reportTime, 23:20


def fast_host(monkeypatch: pytest.MonkeyPatch, upstream_now: int) -> float:
    host = float(upstream_now + HOST_SKEW_S)
    monkeypatch.setattr(time, "time", lambda: host)
    return host


async def board_badge(
    database: Path, upstream: Upstreams, host: float, *, traffic: bool = False
) -> dict[str, Any]:
    live_runtime(database, upstream, host_clock=host)
    async with http(live_app(database)) as client:
        params: dict[str, Any] = {}
        if traffic:
            layer = (await client.get("/api/traffic/europe")).json()
            params = {
                "traffic_snapshot": layer["snapshot_epoch"],
                "traffic_upstream": layer.get("upstream_epoch", 0),
            }
        badge: dict[str, Any] = (await client.get("/api/board", params=params)).json()[
            "last_update"
        ]
        return badge


@pytest.mark.parametrize("offset_s", [-30, 30])
async def test_weather_is_live_against_noaas_clock_with_the_host_21_min_fast(
    database: Path, monkeypatch: pytest.MonkeyPatch, offset_s: int
) -> None:
    upstream_now = OBSERVED + offset_s
    fast = await board_badge(
        database, Upstreams(metar=[METAR], date=upstream_now), fast_host(monkeypatch, upstream_now)
    )
    honest = await board_badge(
        database, Upstreams(metar=[METAR], date=upstream_now), float(upstream_now)
    )

    assert fast["lines"] == [line("weather", "Weather", "live", "Last update under 1 min ago")]
    assert fast == honest, "the host clock changed the verdict"


async def test_weather_is_stale_by_noaas_clock_whatever_the_host_says(
    database: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    upstream_now = OBSERVED + board.METAR_STALE_AFTER_S + 60
    badge = await board_badge(
        database, Upstreams(metar=[METAR], date=upstream_now), fast_host(monkeypatch, upstream_now)
    )

    assert badge["lines"] == [line("weather", "Weather", "stale", "stale: last update 91 min ago")]


async def test_weather_without_an_upstream_time_claims_no_age(
    database: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    badge = await board_badge(
        database, Upstreams(metar=[METAR], date=None), fast_host(monkeypatch, NOW)
    )

    assert badge == {
        "state": "no_upstream_time",
        "lines": [line("weather", "Weather", "no_upstream_time", board.NO_UPSTREAM_TIME)],
    }
    assert not re.search(r"\d", badge["lines"][0]["text"]), "a number would be the host's"


@pytest.mark.parametrize("offset_s", [-30, 30])
async def test_traffic_is_live_against_openskys_clock_with_the_host_21_min_fast(
    database: Path, monkeypatch: pytest.MonkeyPatch, offset_s: int
) -> None:
    upstream_now = NOW + offset_s
    world = Upstreams(metar=[METAR], states=[AIRCRAFT], date=upstream_now, snapshot=NOW)
    fast = await board_badge(database, world, fast_host(monkeypatch, upstream_now), traffic=True)
    honest = await board_badge(database, world, float(upstream_now), traffic=True)

    assert fast["lines"][1] == line("traffic", "Traffic", "live", "Last update under 1 min ago")
    assert fast == honest, "the host clock changed the verdict"


async def test_traffic_is_stale_by_openskys_clock_whatever_the_host_says(
    database: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    upstream_now = NOW + board.STATES_STALE_AFTER_S + 60
    world = Upstreams(metar=[METAR], states=[AIRCRAFT], date=upstream_now, snapshot=NOW)
    badge = await board_badge(database, world, fast_host(monkeypatch, upstream_now), traffic=True)

    assert badge["lines"][1] == line("traffic", "Traffic", "stale", "stale: last update 6 min ago")


async def test_traffic_without_an_upstream_time_claims_no_age(
    database: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = Upstreams(metar=[METAR], states=[AIRCRAFT], date=None, snapshot=NOW)
    badge = await board_badge(database, world, fast_host(monkeypatch, NOW), traffic=True)

    assert badge["lines"][1] == line(
        "traffic", "Traffic", "no_upstream_time", board.NO_UPSTREAM_TIME
    )
    assert not re.search(r"\d", badge["lines"][1]["text"]), "a number would be the host's"


def test_the_board_reads_no_clock_of_its_own() -> None:
    source = Path(board.__file__).read_text(encoding="utf-8")

    assert not re.search(r"\btime\.time\b|datetime\.now|\bimport time\b", source)


def test_no_upstream_time_is_neither_live_nor_stale() -> None:
    css = (STATIC / "board.css").read_text(encoding="utf-8")

    assert board.NO_UPSTREAM_TIME == "freshness unknown — no upstream time"
    assert '.last-update[data-state="no_upstream_time"]' in css
    assert len({"live", "stale", "no_data", "no_upstream_time"}) == 4


def line(source: str, label: str, state: str, text: str) -> dict[str, str]:
    return {"source": source, "label": label, "state": state, "text": text}


async def test_a_failed_layer_gives_the_page_no_snapshot_time(
    database: Path, open_board: httpx.AsyncClient
) -> None:
    live_runtime(database, Upstreams(), opensky_client_id=None, opensky_client_secret=None)

    layer = (await open_board.get("/api/traffic/europe")).json()

    assert layer["state"] == "unknown"
    assert layer["snapshot_epoch"] == 0


@pytest.mark.parametrize("value", ["-1", "soon", str(board.MAX_SNAPSHOT_EPOCH + 1)])
async def test_the_board_refuses_an_impossible_snapshot_time(
    database: Path, open_board: httpx.AsyncClient, value: str
) -> None:
    upstream = Upstreams()
    live_runtime(database, upstream)

    response = await open_board.get("/api/board", params={"traffic_snapshot": value})

    assert response.status_code == 422
    assert upstream.calls == []


async def test_replay_badge_keeps_the_recordings_time(database: Path, tmp_path: Path) -> None:
    recording = await record(
        database, tmp_path / "out", Upstreams(metar=[METAR], states=[AIRCRAFT])
    )
    source = RecordedSource(recording)
    app = create_app(BoardService(source, airports(database)), Gate(None))

    async with http(app) as client:
        first = (await client.get("/api/board")).json()["last_update"]
        second = (await client.get("/api/board")).json()["last_update"]

    made = datetime.fromisoformat(source.recorded_at).strftime("%Y-%m-%d %H:%M UTC")
    assert first == second
    assert first["state"] == "recorded"
    assert badge_texts(first) == [made]


def test_the_page_starts_at_no_data_yet_and_computes_no_age() -> None:
    page = (STATIC / "index.html").read_text(encoding="utf-8")
    script = (STATIC / "board.js").read_text(encoding="utf-8")

    assert f"<span>{board.NO_DATA_YET}</span>" in page
    assert not re.search(r"\bDate\b|performance\.now|setInterval", script)
    assert "line.text" in script
    assert "board.last_update" in script


# --- the radar identity ----------------------------------------------------------------
def css_tokens(name: str = "board.css") -> dict[str, str]:
    text = (STATIC / name).read_text(encoding="utf-8")
    root = re.search(r":root \{(.*?)\n\}", text, re.DOTALL)
    assert root is not None
    tokens = dict(re.findall(r"--([\w-]+):\s*([^;]+);", root.group(1)))
    for key, value in tokens.items():
        reference = re.fullmatch(r"var\(--([\w-]+)\)", value)
        if reference:
            tokens[key] = tokens[reference.group(1)]
    return tokens


def luminance(colour: str) -> float:
    channels = [int(colour[i : i + 2], 16) / 255 for i in (1, 3, 5)]
    r, g, b = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(one: str, other: str) -> float:
    high, low = sorted((luminance(one), luminance(other)), reverse=True)
    return (high + 0.05) / (low + 0.05)


def test_the_tokens_are_the_chosen_palette() -> None:
    tokens = css_tokens()

    assert {key: tokens[key].upper() for key in ("void", "grid", "phosphor", "amber", "type")} == {
        "void": "#070C11",
        "grid": "#16242E",
        "phosphor": "#46E0A8",
        "amber": "#E8A133",
        "type": "#DCE7EC",
    }
    assert tokens["ifr"].upper() == "#FF5C6C"
    assert tokens["vfr"] == tokens["phosphor"]
    assert (tokens["mvfr"].upper(), tokens["lifr"].upper()) == ("#4FA8FF", "#E9E2FF")


@pytest.mark.parametrize("surface", ["void", "land"])
@pytest.mark.parametrize("state", ["vfr", "mvfr", "ifr", "lifr", "unknown"])
def test_state_markers_reach_six_to_one_on_every_map_surface(state: str, surface: str) -> None:
    tokens = css_tokens()

    assert contrast(tokens[state], tokens[surface]) >= 6.0


@pytest.mark.parametrize("surface", ["void", "panel", "raised", "land", "amber-tint"])
@pytest.mark.parametrize("ink", ["type", "muted", "amber", "phosphor"])
def test_text_colours_reach_aa_on_every_surface(ink: str, surface: str) -> None:
    tokens = css_tokens()

    assert contrast(tokens[ink], tokens[surface]) >= 4.5


def test_only_the_stylesheet_defines_colours_and_faces() -> None:
    script = (STATIC / "board.js").read_text(encoding="utf-8")
    sheet = (STATIC / "board.css").read_text(encoding="utf-8")
    rules = sheet.split("\n}", 1)[1]

    assert not re.search(r"#[0-9a-fA-F]{3,8}\b", script)
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b", rules)
    assert "font-face" not in sheet
    assert "url(" not in sheet


def test_unknown_stays_a_hollow_dashed_ring() -> None:
    script = (STATIC / "board.js").read_text(encoding="utf-8")
    sheet = (STATIC / "board.css").read_text(encoding="utf-8")
    drawn = re.search(r"function drawUnknown\(.*?\n  \}", script, re.DOTALL)
    listed = re.search(r"\.mark\.unknown \{(.*?)\}", sheet, re.DOTALL)

    assert drawn is not None
    assert re.search(r"setLineDash\(\[\d", drawn.group())
    assert "fill" not in drawn.group()
    assert listed is not None
    assert "dashed var(--unknown)" in listed.group(1)
    assert "background" not in listed.group(1)


def test_the_sign_in_page_repeats_the_tokens_exactly() -> None:
    tokens, repeated = css_tokens(), css_tokens("login.css")

    assert repeated
    assert all(tokens[key] == value for key, value in repeated.items() if key != "color-scheme")


# --- the gate --------------------------------------------------------------------------
def test_without_a_passcode_the_board_binds_to_localhost() -> None:
    assert bind_host("0.0.0.0", None) == "127.0.0.1"  # noqa: S104
    assert bind_host("0.0.0.0", "") == "127.0.0.1"  # noqa: S104
    assert bind_host("0.0.0.0", PASSCODE) == "0.0.0.0"  # noqa: S104


def test_host_names_are_read_without_port_or_brackets() -> None:
    assert host_name("LOCALHOST:8765") == "localhost"
    assert host_name("[::1]:8765") == "::1"


async def test_without_a_passcode_a_tunnel_hostname_is_refused(database: Path) -> None:
    async with http(live_app(database), "https://example.trycloudflare.com") as client:
        response = await client.get("/api/board")

    assert response.status_code == 403
    assert "passcode" in response.text


async def test_a_passcode_gates_every_route_but_the_login(database: Path) -> None:
    async with http(live_app(database, PASSCODE)) as client:
        assert (await client.get("/api/board")).status_code == 401
        assert (await client.post("/api/airport/ESGG/triage")).status_code == 401
        page = await client.get("/")
        assert (page.status_code, page.headers["location"]) == (303, "/login")
        assert (await client.get("/static/board.js")).status_code == 303
        login = await client.get("/login")
        assert login.status_code == 200
        assert PASSCODE not in login.text
        assert (await client.get("/login.css")).status_code == 200
        assert (await client.get("/healthz")).json() == {"status": "ok"}
        assert (await client.get("/favicon.ico")).status_code == 204


async def test_signing_in_sets_a_protected_cookie_that_opens_the_board(database: Path) -> None:
    upstream = Upstreams()
    live_runtime(database, upstream)
    async with http(live_app(database, PASSCODE), "https://board.example") as client:
        wrong = await client.post("/login", data={"passcode": "nope"})
        assert wrong.status_code == 401
        assert COOKIE_NAME not in wrong.cookies
        assert upstream.calls == []

        right = await client.post("/login", data={"passcode": PASSCODE})
        cookie = right.headers["set-cookie"].lower()
        assert right.status_code == 303
        assert all(flag in cookie for flag in ("httponly", "samesite=strict", "secure"))
        assert PASSCODE not in cookie
        assert (await client.get("/api/board")).status_code == 200

        out = await client.post("/logout")
        assert out.status_code == 303
        client.cookies.clear()
        assert (await client.get("/api/board")).status_code == 401


async def test_login_rejects_an_oversized_body_and_is_idle_without_a_gate(database: Path) -> None:
    async with http(live_app(database, PASSCODE)) as client:
        oversized = await client.post("/login", content=b"passcode=" + b"x" * 5000)
        assert oversized.status_code == 401
    async with http(live_app(database)) as client:
        assert (await client.get("/login")).status_code == 303
        assert (await client.post("/login", data={"passcode": "x"})).status_code == 303


def test_forged_and_expired_cookies_are_refused() -> None:
    clock = FakeClock(float(NOW))
    gate = Gate(PASSCODE, clock=clock)
    cookie = gate.issue()
    expires, _, signature = cookie.partition(".")

    assert gate.valid(cookie)
    assert not gate.valid(None)
    assert not gate.valid(f"{int(expires) + 1}.{signature}")
    assert not gate.valid(f"abc.{signature}")
    assert not Gate(PASSCODE, clock=clock).valid(cookie), "a restart signs everyone out"
    clock.advance(13 * 3600)
    assert not gate.valid(cookie)


def test_a_gate_without_a_passcode_accepts_no_attempt() -> None:
    assert Gate(None).attempt("anything") is False


async def test_repeated_failures_lock_the_login_for_a_minute(database: Path) -> None:
    clock = FakeClock(float(NOW))
    gate = Gate(PASSCODE, clock=clock)
    app = create_app(BoardService(LiveSource(), airports(database)), gate)
    async with http(app) as client:
        for _ in range(MAX_FAILURES):
            await client.post("/login", data={"passcode": "guess"})
        locked = await client.post("/login", data={"passcode": PASSCODE})
        assert locked.status_code == 429
        clock.advance(FAILURE_WINDOW_S + 1)
        assert (await client.post("/login", data={"passcode": PASSCODE})).status_code == 303


async def test_a_cross_origin_post_is_refused(
    database: Path, open_board: httpx.AsyncClient
) -> None:
    upstream = Upstreams()
    live_runtime(database, upstream)

    response = await open_board.post(
        "/api/airport/ESGG/triage", headers={"Origin": "https://evil.example"}
    )

    assert response.status_code == 403
    assert upstream.calls == []


async def test_the_login_form_works_as_a_browser_submits_it(database: Path) -> None:
    """Found in a real browser: `no-referrer` made the form's Origin `null`, a 403."""
    async with http(live_app(database, PASSCODE)) as client:
        page = await client.get("/login")
        signed_in = await client.post(
            "/login", data={"passcode": PASSCODE}, headers={"Origin": LOCAL}
        )
        opaque = await client.post(
            "/login", data={"passcode": PASSCODE}, headers={"Origin": "null"}
        )

    assert page.headers["referrer-policy"] == "same-origin"
    assert signed_in.status_code == 303
    assert opaque.status_code == 403


# --- works from a clone ----------------------------------------------------------------
async def test_every_response_forbids_outside_resources(open_board: httpx.AsyncClient) -> None:
    page = await open_board.get("/")

    assert page.status_code == 200
    policy = page.headers["content-security-policy"]
    assert "default-src 'none'" in policy
    assert "script-src 'self'" in policy
    assert "http" not in policy
    for asset in re.findall(r'(?:src|href)="(/[^"]+)"', page.text):
        assert (await open_board.get(asset)).status_code == 200, asset


def test_the_board_files_name_no_outside_host() -> None:
    for name in ("index.html", "board.js", "board.css", "login.css"):
        text = (STATIC / name).read_text(encoding="utf-8")
        assert not re.search(r"https?://|//cdn|@import", text), name


@pytest.mark.parametrize(
    ("name", "digest"),
    re.findall(
        r"\| `([\w.-]+)` \| `?[^|]+\| `([0-9a-f]{64})` \|",
        (STATIC / "vendor" / "README.md").read_text(encoding="utf-8"),
    ),
)
def test_vendored_files_are_the_pinned_bytes(name: str, digest: str) -> None:
    assert hashlib.sha256((STATIC / "vendor" / name).read_bytes()).hexdigest() == digest


def test_every_vendored_file_is_pinned() -> None:
    readme = (STATIC / "vendor" / "README.md").read_text(encoding="utf-8")
    for path in (STATIC / "vendor").glob("*.*"):
        if path.suffix in {".js", ".json"}:
            assert f"`{path.name}`" in readme


# --- recording and replay --------------------------------------------------------------
async def record(database: Path, directory: Path, upstream: Upstreams) -> Path:
    live_runtime(database, upstream)
    recorder = RecordingSource(LiveSource(), directory)
    app = create_app(BoardService(recorder, airports(database)), Gate(None))
    async with http(app) as client:
        await client.get("/api/board")
        await client.get("/api/airport/ESGG")
        await client.post("/api/airport/ESGG/triage")
        await client.get("/api/traffic/europe")
    recorder.close()
    set_runtime(None)
    return recorder.path


async def test_replay_makes_no_upstream_call_and_says_it_is_recorded(
    database: Path, tmp_path: Path
) -> None:
    recording = await record(
        database, tmp_path / "out", Upstreams(metar=[METAR], states=[AIRCRAFT])
    )
    watcher = Upstreams()
    live_runtime(database, watcher)
    source = RecordedSource(recording)
    app = create_app(BoardService(source, airports(database)), Gate(None))

    async with http(app) as client:
        first = (await client.get("/api/board")).json()
        second = (await client.get("/api/board")).json()
        detail = (await client.get("/api/airport/ESGG")).json()
        triage = (await client.post("/api/airport/ESGG/triage")).json()
        layer = (await client.get("/api/traffic/europe")).json()
        missing = (await client.get("/api/traffic/world")).json()

    assert watcher.calls == [], "replay must not reach any upstream"
    assert first == second
    assert first["banner"] == {"mode": "RECORDED", "label": f"RECORDED {source.recorded_at}"}
    assert first["airports"][0]["marker"]["category"] == "VFR"
    assert detail["traffic"]["summary"] == "1 heard"
    assert triage["severity"] != "unknown"
    assert layer["summary"] == "1 heard"
    assert missing["state"] == "unknown"
    assert "not_recorded" in missing["summary"]


async def test_a_recording_is_versioned_hashed_and_holds_no_local_path(
    database: Path, tmp_path: Path
) -> None:
    recording = await record(database, tmp_path / "out", Upstreams(metar=[METAR]))
    header, *entries = [json.loads(line) for line in recording.read_text("utf-8").splitlines()]

    assert (header["format"], header["version"]) == (FORMAT, 1)
    assert {"t", "tool", "arguments", "payload_sha256", "payload"} == set(entries[0])
    status = next(e for e in entries if e["tool"] == "upstream_status")
    assert "reference_data" not in status["payload"]
    assert str(database) not in recording.read_text("utf-8")


async def test_a_tampered_or_foreign_recording_is_refused(database: Path, tmp_path: Path) -> None:
    recording = await record(database, tmp_path / "out", Upstreams(metar=[METAR]))
    lines = recording.read_text("utf-8").splitlines()
    bad = tmp_path / "bad.jsonl"

    entry = json.loads(lines[1])
    entry["payload"]["ok"] = "changed"
    bad.write_text("\n".join([lines[0], json.dumps(entry)]), encoding="utf-8")
    with pytest.raises(RecordingError, match="hash"):
        RecordedSource(bad)

    bad.write_text("\n".join([lines[0], "{}"]), encoding="utf-8")
    with pytest.raises(RecordingError, match="malformed"):
        RecordedSource(bad)

    bad.write_text(json.dumps({"format": "other"}), encoding="utf-8")
    with pytest.raises(RecordingError, match="not a dashboard recording"):
        RecordedSource(bad)

    bad.write_text(json.dumps({"format": FORMAT, "version": 99}), encoding="utf-8")
    with pytest.raises(RecordingError, match="version"):
        RecordedSource(bad)

    bad.write_text("", encoding="utf-8")
    with pytest.raises(RecordingError, match="not a recording"):
        RecordedSource(bad)


def test_recordings_may_not_be_written_inside_the_repository() -> None:
    with pytest.raises(RecordingError, match="outside the repository"):
        RecordingSource(LiveSource(), REPO_ROOT / "recordings")
    with pytest.raises(RecordingError, match="outside the repository"):
        RecordingSource(LiveSource(), REPO_ROOT)


def test_the_default_recording_directory_is_in_the_home_directory(tmp_path: Path) -> None:
    assert default_directory(tmp_path) == tmp_path
    assert default_directory(None) == Path.home() / ".flightops" / "recordings"


# --- the entry point -------------------------------------------------------------------
def cli_settings(database: Path, **overrides: Any) -> Settings:
    return Settings(_env_file=None, airports_db=database, **overrides)  # type: ignore[call-arg]


def cli_args(**overrides: Any) -> argparse.Namespace:
    return argparse.Namespace(**{"host": "0.0.0.0", "record": None, "replay": None, **overrides})  # noqa: S104


def test_no_passcode_binds_locally_and_logs_why(database: Path, logs: LogCapture) -> None:
    _app, host = cli.build(cli_args(), cli_settings(database))

    assert host == "127.0.0.1"
    (event,) = logs.events("dashboard_localhost_only")
    assert "FLIGHTOPS_DASHBOARD_PASSCODE" in event["reason"]
    assert logs.events("opensky_mode")[0]["credentials"] == "none"


async def test_a_passcode_allows_the_requested_host_and_never_reaches_the_log(
    database: Path, logs: LogCapture
) -> None:
    settings = cli_settings(database, dashboard_passcode=SecretStr(PASSCODE))

    app, host = cli.build(cli_args(), settings)

    assert host == "0.0.0.0"  # noqa: S104
    assert PASSCODE not in logs.text
    assert PASSCODE not in repr(settings)
    async with http(app) as client:
        assert (await client.get("/api/board")).status_code == 401


async def test_recording_and_replay_start_from_the_command_line(
    database: Path, tmp_path: Path, logs: LogCapture
) -> None:
    runtime = live_runtime(database, Upstreams())
    settings = cli_settings(database, dashboard_recordings_dir=tmp_path / "default")
    app, _host = cli.build(cli_args(record=""), settings)
    async with app.router.lifespan_context(app):
        pass
    set_runtime(None)
    assert runtime is not None
    (recording,) = (tmp_path / "default").glob("board-*.jsonl")

    replay, _host = cli.build(cli_args(replay=recording), settings)
    async with replay.router.lifespan_context(replay):
        pass

    assert logs.events("dashboard_replaying")


def test_a_fresh_clone_builds_its_reference_data_from_the_seed(tmp_path: Path) -> None:
    target = tmp_path / "new" / "airports.sqlite3"

    listed = cli.load_airports(cli_settings(target))

    assert target.is_file()
    assert listed
    assert all(a["latitude"] is not None for a in listed)
    assert listed[0]["type"] == "large_airport"


def test_main_serves_with_uvicorn_at_the_resolved_address(
    database: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    served: dict[str, Any] = {}
    monkeypatch.setattr(cli, "Settings", lambda: cli_settings(database))
    monkeypatch.setattr(cli.uvicorn, "run", lambda _app, **options: served.update(options))

    cli.main(["--host", "0.0.0.0", "--port", "9999"])  # noqa: S104

    assert served["host"] == "127.0.0.1"
    assert served["port"] == 9999


def test_main_reports_an_unreadable_recording_as_a_usage_error(
    database: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "Settings", lambda: cli_settings(database))

    with pytest.raises(SystemExit) as stopped:
        cli.main(["--replay", str(tmp_path / "missing.jsonl")])

    assert stopped.value.code == 2
