"""The board over the world set: regions, a paged list, the traffic ledger, the triage panel."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

import httpx
import pytest

from dashboard import __main__ as cli
from dashboard import board
from dashboard.app import create_app
from dashboard.gate import Gate
from dashboard.service import (
    COVERAGE_NOTE,
    REGIONS,
    TRAFFIC_COST,
    BoardService,
    TrafficSpend,
    UnknownBoardItem,
    traffic_default,
)
from flightops_mcp.airports import build_from_csv
from flightops_mcp.config import Settings

from .conftest import FakeClock

NOW = 1_794_819_600


def airport(ident: str, continent: str, name: str | None = None, **extra: Any) -> dict[str, Any]:
    return {
        "ident": ident,
        "iata": None,
        "name": name or f"{ident} Airport",
        "type": "large_airport",
        "latitude": 50.0,
        "longitude": 10.0,
        "continent": continent,
        "municipality": None,
        "iso_country": "XX",
        **extra,
    }


class FakeSource:
    """Tool answers by name, and every call recorded."""

    recorded_at: None = None

    def __init__(self, **answers: Any) -> None:
        self.answers = answers
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((tool, arguments))
        answer = self.answers.get(tool, {"ok": False, "error": {"code": "not_scripted"}})
        return answer(arguments) if callable(answer) else answer


def overview(arguments: dict[str, Any]) -> dict[str, Any]:
    entry = {
        "ok": True,
        "upstream_time_epoch": NOW,
        "metar": [{"flight_category": "VFR", "observation_time": "2026-11-16T08:50:00Z"}],
        "latest_metar": {"age_s": 600, "current": True},
        "unknowns": [],
    }
    return {"ok": True, "airports": dict.fromkeys(arguments["airports"], entry)}


STATUS = {"credentials": {"opensky": "client_credentials"}, "budget": {}, "cache": {}}
AIRSPACE = {
    "ok": True,
    "columns": ["icao24", "latitude", "longitude", "last_contact"],
    "rows": [["abc", 50.0, 10.0, NOW]],
    "snapshot_time_epoch": NOW,
    "upstream_time_epoch": NOW,
    "snapshot_current": True,
    "bookkeeping": {"credits_spent": 4},
}


def service(airports: list[dict[str, Any]], **kwargs: Any) -> tuple[BoardService, FakeSource]:
    source = FakeSource(
        weather_overview=overview, upstream_status=STATUS, airspace_snapshot=AIRSPACE
    )
    return BoardService(source, airports, **kwargs), source


EUROPE = [airport(f"E{n:03d}", "EU") for n in range(130)]
WORLD = [*EUROPE, airport("KJFK", "NA", "John F Kennedy", municipality="New York")]


# --- regions and the list -----------------------------------------------------------
def test_each_region_says_how_many_airports_it_draws() -> None:
    board_service, _ = service(WORLD)

    counts = {region["id"]: region["airports"] for region in board_service.regions()}

    assert list(counts) == list(REGIONS)
    assert counts["europe"] == 130
    assert counts["north_america"] == 1
    assert counts["africa"] == 0
    assert counts["world"] == 131


async def test_a_region_is_weathered_a_hundred_airports_per_call() -> None:
    board_service, source = service(WORLD)

    snapshot = await board_service.snapshot("europe")

    asked = [
        arguments["airports"] for tool, arguments in source.calls if tool == "weather_overview"
    ]
    assert [len(chunk) for chunk in asked] == [100, 30]
    assert len(snapshot["airports"]) == 130
    assert snapshot["airports"][0]["marker"]["category"] == "VFR"
    assert snapshot["banner"]["label"] == "LIVE: 130 of 130 with a current METAR"
    assert snapshot["coverage"] == COVERAGE_NOTE
    assert snapshot["region"] == "europe"


async def test_a_failed_overview_marks_its_airports_unknown_with_the_code() -> None:
    board_service, source = service(EUROPE[:3])
    source.answers["weather_overview"] = {"ok": False, "error": {"code": "upstream_unavailable"}}

    snapshot = await board_service.snapshot("europe")

    assert {a["marker"]["kind"] for a in snapshot["airports"]} == {"unknown"}
    assert snapshot["airports"][0]["weather"]["summary"] == (
        "Unknown: weather unavailable (upstream_unavailable)"
    )


async def test_an_overview_without_an_entry_is_not_called_weather() -> None:
    board_service, source = service(EUROPE[:2])
    source.answers["weather_overview"] = {"ok": True, "airports": {}}

    snapshot = await board_service.snapshot("europe")

    assert (
        snapshot["airports"][0]["weather"]["summary"]
        == "Unknown: weather unavailable (not_answered)"
    )


async def test_the_list_is_filtered_and_paged_by_the_server() -> None:
    board_service, _ = service(WORLD)
    await board_service.snapshot("world")

    first = board_service.airport_list("world", "", 0, 25)
    last = board_service.airport_list("world", "", 125, 25)
    found = board_service.airport_list("world", "new york", 0, 25)

    assert (first["total"], len(first["items"]), first["region_total"]) == (131, 25, 131)
    assert first["items"][0]["weather"]["summary"].startswith("VFR")
    assert [item["icao"] for item in last["items"]][-1] == "KJFK"
    assert len(last["items"]) == 6
    assert [item["icao"] for item in found["items"]] == ["KJFK"]
    assert found["total"] == 1


def test_a_page_past_the_end_and_an_oversized_page_are_clamped() -> None:
    board_service, _ = service(WORLD)

    page = board_service.airport_list("europe", "", 10_000, 500)

    assert page["offset"] == 129
    assert page["limit"] == 50
    assert page["items"][0]["weather"]["summary"] == board.NO_DATA_YET
    assert board_service.airport_list("africa", "", 5, 25)["offset"] == 0


@pytest.mark.parametrize("region", ["mars", "EUROPE", ""])
def test_an_unlisted_region_is_refused(region: str) -> None:
    board_service, _ = service(WORLD)

    with pytest.raises(UnknownBoardItem):
        board_service.airport_list(region)


async def test_the_list_and_a_region_are_served_compressed_over_http() -> None:
    board_service, _ = service(WORLD)
    app = create_app(board_service, Gate(None))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8765"
    ) as client:
        overview_response = await client.get("/api/board?region=europe")
        listed = (await client.get("/api/airports?region=world&q=jfk")).json()
        unknown = await client.get("/api/airports?region=mars")
        too_long = await client.get("/api/airports?q=" + "x" * 65)

    assert overview_response.headers["content-encoding"] == "gzip"
    assert [item["icao"] for item in listed["items"]] == ["KJFK"]
    assert unknown.status_code == 404
    assert too_long.status_code == 422


# --- the traffic ledger ---------------------------------------------------------------
def test_the_layer_refreshes_no_faster_than_its_ceiling_allows() -> None:
    assert TrafficSpend(60).interval_s() == 240
    assert TrafficSpend(240).interval_s() == 120
    assert TrafficSpend(0).interval_s() == 120


def test_the_ceiling_refuses_with_the_numbers_and_when_it_frees() -> None:
    clock = FakeClock(0.0)
    spend = TrafficSpend(8, clock=clock)

    spend.record(4)
    clock.advance(600)
    spend.record(4)

    assert spend.refusal() == (
        "8 of 8 traffic credits used in the last hour; the next refresh fits in 50 min"
    )
    clock.advance(3000)
    assert spend.refusal() is None
    assert spend.spent_last_hour() == 4
    assert spend.session_total == 8


def test_a_ceiling_below_one_refresh_says_so() -> None:
    assert TrafficSpend(3).refusal() == (
        "the hourly traffic ceiling is 3 credits, under one refresh"
    )


def test_a_cached_answer_costs_the_session_nothing() -> None:
    spend = TrafficSpend(60)

    spend.record(0)

    assert spend.view() == {
        "per_refresh": TRAFFIC_COST,
        "session_total": 0,
        "hour_spent": 0,
        "hour_ceiling": 60,
        "interval_s": 240,
    }


async def test_a_refused_refresh_asks_no_upstream_and_the_badge_says_so() -> None:
    board_service, source = service(EUROPE[:1], traffic_ceiling_per_hour=4)

    first = await board_service.airspace("europe")
    second = await board_service.airspace("europe")
    snapshot = await board_service.snapshot("europe", traffic_snapshot=NOW, traffic_upstream=NOW)

    assert first["summary"] == "1 heard"
    assert first["cost"]["session_total"] == 4
    assert [tool for tool, _ in source.calls].count("airspace_snapshot") == 1
    assert second["state"] == "refused"
    assert second["aircraft"] == []
    assert second["refused"].startswith("Auto-refresh stopped: 4 of 4 traffic credits")
    lines = snapshot["last_update"]["lines"]
    assert lines[-1]["state"] == "refused"
    assert lines[-1]["label"] == "Refresh"
    assert lines[-1]["text"] == second["refused"]
    assert snapshot["last_update"]["state"] == "stale"
    assert snapshot["traffic"]["refused"] == second["refused"]


async def test_a_guard_refusal_stops_the_layer_and_a_success_clears_it() -> None:
    board_service, source = service(EUROPE[:1])
    source.answers["airspace_snapshot"] = {
        "ok": False,
        "error": {"code": "local_budget_exhausted", "message": "Daily budget spent."},
    }

    refused = await board_service.airspace("europe")
    source.answers["airspace_snapshot"] = AIRSPACE
    cleared = await board_service.airspace("europe")
    snapshot = await board_service.snapshot("europe", traffic_snapshot=NOW, traffic_upstream=NOW)

    assert refused["refused"] == "Refresh refused: Daily budget spent."
    assert "refused" not in cleared
    assert snapshot["traffic"]["refused"] is None
    assert [line["state"] for line in snapshot["last_update"]["lines"]] == ["live", "live"]


async def test_an_upstream_failure_is_not_called_a_refusal() -> None:
    board_service, source = service(EUROPE[:1])
    source.answers["airspace_snapshot"] = {"ok": False, "error": {"code": "upstream_unavailable"}}

    layer = await board_service.airspace("europe")

    assert "refused" not in layer
    assert layer["summary"] == "Unknown: traffic unavailable (upstream_unavailable)"


@pytest.mark.parametrize(
    ("mode", "on", "available"),
    [
        ("client_credentials", True, True),
        ("anonymous", False, True),
        ("none", False, False),
        (None, False, False),
    ],
)
def test_traffic_starts_on_only_with_credentials_and_says_why(
    mode: str | None, on: bool, available: bool
) -> None:
    default = traffic_default(mode)

    assert (default["on"], default["available"]) == (on, available)
    assert default["reason"]


async def test_the_board_hands_the_page_the_traffic_default_and_cost() -> None:
    board_service, _ = service(EUROPE[:1])

    traffic = (await board_service.snapshot("europe"))["traffic"]

    assert traffic["on"] is True
    assert traffic["per_refresh"] == 4
    assert traffic["session_total"] == 0
    assert traffic["hour_ceiling"] == 60


# --- the reference file the board opens -------------------------------------------------
def test_a_database_from_before_continents_is_refused_with_the_command(tmp_path: Path) -> None:
    database = tmp_path / "airports.sqlite3"
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("CREATE TABLE airports (ident TEXT PRIMARY KEY, name TEXT)")
        connection.commit()
    settings = Settings(_env_file=None, airports_db=database)  # type: ignore[call-arg]

    with pytest.raises(cli.StaleReferenceData, match="--csv data/airports_world.csv"):
        cli.load_airports(settings)


def test_a_full_build_still_draws_only_the_world_set(tmp_path: Path) -> None:
    source = tmp_path / "full.csv"
    source.write_text(
        "ident,type,name,latitude_deg,longitude_deg,continent,scheduled_service\n"
        "AAAA,large_airport,Alpha,1,2,EU,yes\n"
        "BBBB,small_airport,Bravo,3,4,EU,yes\n"
        "CCCC,medium_airport,Charlie,5,6,EU,no\n",
        encoding="utf-8",
    )
    database = tmp_path / "airports.sqlite3"
    build_from_csv(source, database)
    settings = Settings(_env_file=None, airports_db=database)  # type: ignore[call-arg]

    assert [a["ident"] for a in cli.load_airports(settings)] == ["AAAA"]


# --- the triage panel -------------------------------------------------------------------
COUNTS = {
    "arrivals": {"count": 7, "received": 7, "unreadable": 0},
    "departures": {"count": 9, "received": 10, "unreadable": 1},
    "movements_window_minutes": 60,
    "aircraft_in_search_box": {
        "count": 40,
        "received": 52,
        "unreadable": 0,
        "listed": 40,
        "airborne": 30,
        "on_ground": 10,
        "truncated": True,
    },
    "search_radius_nm": 40.0,
}


def test_counts_read_as_numbers_with_what_was_unreadable() -> None:
    assert board.count_rows(COUNTS) == [
        {"label": "Arrivals", "text": "7 in the last 60 min"},
        {"label": "Departures", "text": "9 in the last 60 min; 1 of 10 rows unreadable"},
        {
            "label": "Aircraft nearby",
            "text": "40 heard within 40 nm (30 airborne, 10 on the ground); "
            "only the 40 most recent listed",
        },
    ]


def test_a_count_never_made_is_not_zero() -> None:
    assert board.count_rows(
        {"arrivals": None, "departures": None, "aircraft_in_search_box": None}
    ) == [
        {"label": "Arrivals", "text": "not counted: movements unavailable"},
        {"label": "Departures", "text": "not counted: movements unavailable"},
        {"label": "Aircraft nearby", "text": "not counted: traffic unavailable"},
    ]


def test_weather_reads_as_values() -> None:
    rows = board.weather_rows(
        {
            "category": "IFR",
            "current": True,
            "age_s": 300,
            "ceiling": {"ft": 1200, "basis": "OVC"},
            "visibility": {"statute_mi": 6.0, "at_least": True},
            "wind": {"direction_deg": 40.0, "variable": False, "speed_kt": 12.0, "gust_kt": 25.0},
        }
    )

    assert rows == [
        {"label": "Category", "text": "IFR"},
        {"label": "METAR age", "text": "5 min by NOAA's clock"},
        {"label": "Ceiling", "text": "1,200 ft (OVC)"},
        {"label": "Visibility", "text": "6 or more sm"},
        {"label": "Wind", "text": "040° 12 kt, gusts 25 kt"},
    ]


def test_weather_says_what_the_report_did_not_give() -> None:
    rows = board.weather_rows(
        {
            "category": None,
            "current": False,
            "age_s": None,
            "ceiling": {"ft": None, "basis": "no broken or overcast layer"},
            "visibility": {"statute_mi": None},
            "wind": {"direction_deg": None, "variable": True, "speed_kt": 3.0, "gust_kt": None},
        }
    )

    assert [row["text"] for row in rows] == [
        "none given (not current)",
        "unknown",
        "no broken or overcast layer",
        "not in the report",
        "variable 3 kt, no gusts reported",
    ]
    assert board.weather_rows(None) == [{"label": "METAR", "text": "none returned, so no values"}]
    assert board.weather_rows({"wind": {"speed_kt": None}})[-1]["text"] == "not in the report"
    no_heading = {"wind": {"speed_kt": 5.0, "direction_deg": None}}
    assert board.weather_rows(no_heading)[-1]["text"] == (
        "direction not given 5 kt, no gusts reported"
    )


def test_each_leg_reads_with_its_state_age_and_source() -> None:
    rows = board.leg_rows(
        {
            "weather": {"status": "observed", "source": "NOAA /metar", "age_s": 30, "error": None},
            "traffic": {
                "status": "unavailable",
                "source": "OpenSky /states/all",
                "age_s": None,
                "error": "upstream_unavailable",
            },
            "movements": {"status": "odd", "source": "OpenSky /flights", "age_s": 7200},
        }
    )

    assert rows == [
        {
            "label": "Weather",
            "state": "observed",
            "text": "observed; data under 1 min old; NOAA /metar",
        },
        {
            "label": "Traffic",
            "state": "unavailable",
            "text": "unavailable (upstream_unavailable); age unknown; OpenSky /states/all",
        },
        {
            "label": "Movements",
            "state": "odd",
            "text": "unknown; data 2 h 0 min old; OpenSky /flights",
        },
    ]


def test_the_panel_passes_the_why_not_lines_through() -> None:
    view = board.triage_view(
        {
            "ok": True,
            "assessment": {
                "severity": "low",
                "why_not_higher": ["Gusts 12 kt, below the 35 kt flag."],
                "why_not_lower": ["Low is the bottom of this ladder."],
            },
            "counts": COUNTS,
            "weather": None,
            "legs": {},
        }
    )

    assert view["why_not_higher"] == ["Gusts 12 kt, below the 35 kt flag."]
    assert view["why_not_lower"] == ["Low is the bottom of this ladder."]
    assert len(view["counts"]) == 3
    assert view["weather"] == [{"label": "METAR", "text": "none returned, so no values"}]
    assert view["legs"] == []


def test_a_refused_layer_draws_nothing_and_claims_no_time() -> None:
    assert board.refused_view("Stopped.") == {
        "state": "refused",
        "summary": "Stopped.",
        "hint": None,
        "snapshot_epoch": 0,
        "upstream_epoch": 0,
        "aircraft": [],
        "unknowns": ["Stopped."],
        "refused": "Stopped.",
    }


def test_a_refusal_line_appears_only_with_a_traffic_layer() -> None:
    weather = [{"state": "reported", "age_s": 60, "upstream_time": NOW}]

    without = board.last_update(weather, None, None, None, "Stopped.")
    with_layer = board.last_update(weather, None, NOW, NOW, "Stopped.")

    assert len(without["lines"]) == 1
    assert with_layer["lines"][-1] == {
        "source": "traffic",
        "label": "Refresh",
        "state": "refused",
        "text": "Stopped.",
    }
    assert with_layer["state"] == "stale"


def test_a_boolean_is_not_a_number_on_the_panel() -> None:
    counts = {**COUNTS, "search_radius_nm": True}

    assert "within None nm" in board.count_rows(counts)[2]["text"]
    assert board.weather_rows({"wind": {"speed_kt": True}})[-1]["text"] == "not in the report"
