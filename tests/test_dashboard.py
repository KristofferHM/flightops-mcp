"""The airport board, through its HTTP surface, against fake upstreams."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
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

from .conftest import FIXTURES, FakeClock, LogCapture, SleepRecorder

NOW = int(datetime(2026, 11, 14, 23, 30, tzinfo=UTC).timestamp())
PASSCODE = "correct horse battery"
METAR = {
    "icaoId": "ESGG",
    "reportTime": "2026-11-14T23:20:00Z",
    "rawOb": "ESGG 142320Z 24008KT 9999 BKN030 08/05 Q1008",
    "fltCat": "VFR",
}
AS_OF = datetime.fromtimestamp(NOW, tz=UTC).isoformat()
LOCAL = "http://127.0.0.1:8765"
AIRCRAFT = ["abc123", "SAS1 ", "Sweden", NOW, NOW - 60, 12.0, 57.7, 3000.0, False, 200.0, 90.0, 0.0]


@dataclass
class Upstreams:
    metar: list[dict[str, Any]] = field(default_factory=list)
    states: list[list[Any]] = field(default_factory=list)
    calls: list[str] = field(default_factory=list)

    def opensky(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request.url.path)
        if "/flights/" in request.url.path:
            return httpx.Response(200, json=[])
        return httpx.Response(
            200,
            json={"time": NOW, "states": self.states},
            headers={"X-Rate-Limit-Remaining": "3900"},
        )

    def weather(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request.url.path)
        if request.url.path.endswith("/metar") and self.metar:
            return httpx.Response(200, json=self.metar)
        return httpx.Response(204)

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


def live_runtime(database: Path, upstream: Upstreams, **overrides: Any) -> Runtime:
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
        clock=lambda: float(NOW),
        data_clock=lambda: float(NOW),
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
    view = board.weather_view(payload)

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
    assert "drawUnknown(x, y, 6)" in script


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
            "as_of": AS_OF,
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
    base = {"ok": True, "columns": columns, "snapshot_time_epoch": NOW, "as_of": AS_OF}

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
