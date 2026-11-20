"""The world reference set, the batched weather tool, and a triage that counts and explains."""

from __future__ import annotations

import csv
import importlib.util
import sqlite3
import time
from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import Any

import httpx
import pytest

from flightops_mcp import server as srv
from flightops_mcp.airports import CONTINENTS, WORLD_TYPES, AirportDB, build_from_csv
from flightops_mcp.config import Settings
from flightops_mcp.provenance import LICENCE_FIELDS
from flightops_mcp.runtime import Runtime, set_runtime
from flightops_mcp.weather import ceiling
from services.mock_upstream.dated import dated

from .conftest import FIXTURES, SleepRecorder

REPO = Path(__file__).resolve().parent.parent
WORLD_CSV = REPO / "data" / "airports_world.csv"
NOW = int(datetime(2026, 11, 14, 23, 30, tzinfo=UTC).timestamp())


def _rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(line for line in handle if not line.startswith("#")))


# --- the committed world set ----------------------------------------------------------
def test_the_world_set_is_every_scheduled_large_and_medium_airport() -> None:
    rows = _rows(WORLD_CSV)

    assert len(rows) == 3270
    assert {row["type"] for row in rows} == WORLD_TYPES
    assert {row["scheduled_service"] for row in rows} == {"yes"}
    assert Counter(row["continent"] for row in rows) == {
        "AS": 1054,
        "NA": 851,
        "EU": 508,
        "AF": 342,
        "SA": 274,
        "OC": 241,
    }
    assert set(CONTINENTS) == {row["continent"] for row in rows}
    assert len({row["ident"] for row in rows}) == len(rows)


def test_the_world_set_names_its_source_date_and_licence() -> None:
    header = [
        line for line in WORLD_CSV.read_text(encoding="utf-8").splitlines() if line[:1] == "#"
    ]

    assert any("ourairports-data/airports.csv" in line for line in header)
    assert any(line.startswith("# Upstream file dated 20") for line in header)
    assert any("public domain" in line for line in header)
    assert any("3270 rows" in line for line in header)


def test_the_world_set_loads_with_continents(tmp_path: Path) -> None:
    database = tmp_path / "world.sqlite3"

    assert build_from_csv(WORLD_CSV, database) == 3270
    db = AirportDB(database)
    assert db.has_continents()
    assert len(db.placed()) == 3270
    assert db.get("ESGG") == {**db.get("ESGG"), "continent": "EU", "iata": "GOT"}  # type: ignore[dict-item]


def test_the_seed_stays_the_test_fixture() -> None:
    assert len(_rows(REPO / "data" / "airports_seed.csv")) == 25


# --- the loader's filters -------------------------------------------------------------
SAMPLE = """# a provenance line
# another
ident,type,name,latitude_deg,longitude_deg,continent,scheduled_service,iata_code
AAAA,large_airport,Alpha,1,2,EU,yes,AAA
BBBB,medium_airport,"Bravo, # not a comment",3,4,AF,no,
CCCC,small_airport,Charlie,5,6,NA,yes,
DDDD,heliport,Delta,7,8,NA,yes,
"""


def test_a_comment_header_is_skipped_and_types_filter(tmp_path: Path) -> None:
    source = tmp_path / "sample.csv"
    source.write_text(SAMPLE, encoding="utf-8")
    database = tmp_path / "out.sqlite3"

    assert build_from_csv(source, database) == 3
    assert AirportDB(database).get("BBBB")["name"] == "Bravo, # not a comment"  # type: ignore[index]
    assert build_from_csv(source, database, types=WORLD_TYPES) == 2
    assert build_from_csv(source, database, types=WORLD_TYPES, scheduled_only=True) == 1
    assert AirportDB(database).get("AAAA")["continent"] == "EU"  # type: ignore[index]


def test_a_database_from_before_continents_says_so(tmp_path: Path) -> None:
    database = tmp_path / "old.sqlite3"
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("CREATE TABLE airports (ident TEXT PRIMARY KEY, name TEXT)")
        connection.commit()

    assert AirportDB(database).has_continents() is False


def _build_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "build_airports_db", REPO / "scripts" / "build_airports_db.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_export_writes_its_provenance_and_only_the_filtered_rows(tmp_path: Path) -> None:
    source = tmp_path / "full.csv"
    source.write_text(SAMPLE.split("\n", 2)[2], encoding="utf-8")
    out = tmp_path / "world.csv"

    written = _build_script().export(
        source, out, types=WORLD_TYPES, scheduled_only=True, dated="2026-11-18"
    )

    assert written == 1
    text = out.read_text(encoding="utf-8")
    assert "# Upstream file dated 2026-11-18" in text
    assert "scheduled_service == yes; 1 rows" in text
    assert [row["ident"] for row in _rows(out)] == ["AAAA"]


# --- the ceiling ------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("observation", "expected"),
    [
        (
            {"clouds": [{"cover": "FEW", "base_ft": 800.0}, {"cover": "BKN", "base_ft": 2500.0}]},
            {"ft": 2500, "basis": "BKN"},
        ),
        (
            {"clouds": [{"cover": "OVC", "base_ft": 900.0}, {"cover": "BKN", "base_ft": 600.0}]},
            {"ft": 600, "basis": "BKN"},
        ),
        ({"clouds": [], "vertical_visibility_ft": 200.0}, {"ft": 200, "basis": "VV"}),
        (
            {"clouds": [{"cover": "SCT", "base_ft": 1500.0}]},
            {"ft": None, "basis": "no broken or overcast layer"},
        ),
        ({"clouds": None}, {"ft": None, "basis": "not in the report"}),
        (
            {"clouds": [{"cover": "OVC", "base_ft": None}]},
            {"ft": None, "basis": "no broken or overcast layer"},
        ),
    ],
)
def test_the_ceiling_is_the_lowest_broken_or_overcast_layer(
    observation: dict[str, Any], expected: dict[str, Any]
) -> None:
    assert ceiling(observation) == expected


# --- runtime over fixture upstreams -----------------------------------------------------
def metar(station: str, category: str, *, minutes_ago: int = 10, **extra: Any) -> dict[str, Any]:
    observed = datetime.fromtimestamp(NOW - minutes_ago * 60, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "icaoId": station,
        "reportTime": observed,
        "rawOb": f"{station} {category}",
        "fltCat": category,
        **extra,
    }


def state(icao24: str, *, on_ground: bool = False) -> list[Any]:
    return [
        icao24,
        "TEST1 ",
        "Sweden",
        NOW - 5,
        NOW - 5,
        12.3,
        57.7,
        3000.0,
        on_ground,
        200.0,
        90.0,
        0.0,
    ]


def flight(icao24: str, last_seen: int) -> dict[str, Any]:
    return {
        "icao24": icao24,
        "firstSeen": last_seen - 3600,
        "lastSeen": last_seen,
        "estDepartureAirport": "ESSA",
        "estArrivalAirport": "ESGG",
        "callsign": "SAS1",
    }


Handler = Callable[[httpx.Request], httpx.Response]


@pytest.fixture
def world(tmp_path: Path) -> Iterator[Callable[..., Runtime]]:
    def install(
        *,
        metars: list[dict[str, Any]] | None = None,
        states: list[list[Any]] | None = None,
        states_status: int = 200,
        arrivals: list[dict[str, Any]] | None = None,
        departures: list[dict[str, Any]] | None = None,
        flights_status: int = 200,
        weather_requests: list[httpx.Request] | None = None,
    ) -> Runtime:
        database = tmp_path / "airports.sqlite3"
        if not database.is_file():
            build_from_csv(FIXTURES / "airports_sample.csv", database)
        settings = Settings(
            _env_file=None,  # type: ignore[call-arg]
            opensky_client_id="id",
            opensky_client_secret="secret",
            airports_db=database,
            backoff_base_s=0.0,
            max_retries=0,
        )

        def weather(request: httpx.Request) -> httpx.Response:
            if weather_requests is not None:
                weather_requests.append(request)
            if request.url.path.endswith("/taf"):
                return httpx.Response(200, json=[{"icaoId": "ESGG", "rawTAF": "TAF ESGG"}])
            asked = set(request.url.params["ids"].split(","))
            body = [m for m in metars or [] if m["icaoId"] in asked]
            return httpx.Response(200, json=body) if body else httpx.Response(204)

        def opensky(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if "/flights/" in path:
                if flights_status != 200:
                    return httpx.Response(flights_status)
                rows = arrivals if path.endswith("arrival") else departures
                return httpx.Response(200, json=rows or [])
            if states_status != 200:
                return httpx.Response(states_status)
            return httpx.Response(200, json={"time": NOW - 5, "states": states or []})

        def token(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 1800})

        def clock() -> float:
            return float(NOW)

        runtime = Runtime.build(
            settings,
            opensky_transport=httpx.MockTransport(dated(opensky, clock)),
            token_transport=httpx.MockTransport(token),
            weather_transport=httpx.MockTransport(dated(weather, clock)),
            sleep=SleepRecorder(),
            clock=time.time,
            data_clock=clock,
        )
        set_runtime(runtime)
        return runtime

    yield install
    set_runtime(None)


# --- weather_overview ---------------------------------------------------------------------
async def test_the_overview_asks_once_for_many_airports_and_ages_each(
    world: Callable[..., Runtime],
) -> None:
    requests: list[httpx.Request] = []
    world(
        metars=[
            metar("ESGG", "IFR"),
            metar("ESGG", "VFR", minutes_ago=40),
            metar("ESSA", "VFR", minutes_ago=200),
        ],
        weather_requests=requests,
    )

    answer = await srv.weather_overview(["esgg", "ESSA", "EKCH", "ZZZZ", "ESGG"])

    assert answer["ok"] is True
    assert len(requests) == 1
    assert requests[0].url.params["ids"] == "ESGG,ESSA,EKCH"
    assert answer["not_in_reference_data"] == ["ZZZZ"]
    assert answer["counts"] == {
        "asked": 4,
        "with_metar": 2,
        "current": 1,
        "failed": 0,
        "requests": 1,
    }
    entries = answer["airports"]
    assert entries["ESGG"]["latest_metar"] == {"age_s": 600, "current": True}
    assert entries["ESSA"]["latest_metar"]["current"] is False
    assert "not current conditions" in entries["ESSA"]["unknowns"][0]
    assert entries["EKCH"]["metar"] == []
    assert entries["EKCH"]["unknowns"][0].startswith("No METAR returned.")
    assert "ZZZZ is not in the reference data" in answer["unknowns"][0]


@pytest.mark.parametrize("airports", [[], [" "], [f"A{n:03d}" for n in range(101)]])
async def test_the_overview_refuses_an_empty_or_oversized_list(
    world: Callable[..., Runtime], airports: list[str]
) -> None:
    world()

    answer = await srv.weather_overview(airports)

    assert answer["ok"] is False
    assert answer["error"]["code"] == "invalid_argument"


async def test_one_airport_weather_still_reads_as_before(world: Callable[..., Runtime]) -> None:
    world(metars=[])

    answer = await srv.airport_weather("ESGG")

    assert answer["unknowns"][:1] == [
        "No METAR returned. That means NOAA holds no current observation for this "
        "station, not that conditions are good."
    ]
    # As before, plus the licence fields the edition stamp adds (provenance.py).
    kept = {k: v for k, v in answer["source"].items() if k not in LICENCE_FIELDS}
    assert kept == srv.WEATHER_SOURCE
    assert answer["source"]["licence_class"] == "A"


# --- the triage ---------------------------------------------------------------------------
async def test_a_reason_seen_in_several_metars_is_one_reason_with_its_count(
    world: Callable[..., Runtime],
) -> None:
    world(
        metars=[metar("ESGG", "IFR", minutes_ago=m) for m in (10, 40, 70, 100)],
        states=[state("a")],
        arrivals=[flight("a", NOW - 900)],
    )

    triage = await srv.disruption_triage("ESGG")

    assessment = triage["assessment"]
    assert assessment["reasons"] == [
        "ESGG: flight category IFR (in 4 of 4 METARs from the last 2 h)"
    ]
    assert assessment["reason_details"] == [
        {"reason": assessment["reasons"][0], "leg": "weather", "observations": 4}
    ]
    # One condition, seen four times, is not four reasons: elevated, not high.
    assert assessment["severity"] == "elevated"


async def test_a_triage_counts_each_leg_and_what_was_readable(
    world: Callable[..., Runtime],
) -> None:
    world(
        metars=[
            metar(
                "ESGG",
                "VFR",
                wdir=240,
                wspd=12,
                wgst=20,
                visib="6+",
                clouds=[{"cover": "BKN", "base": 3500}],
            )
        ],
        states=[state("a"), state("b", on_ground=True), "not a state vector"],
        arrivals=[flight("a", NOW - 900), flight("b", NOW - 600)],
        departures=[flight("c", NOW - 1200), "not a flight"],
    )

    triage = await srv.disruption_triage("ESGG")

    counts = triage["counts"]
    assert counts["arrivals"] == {"count": 2, "received": 2, "unreadable": 0}
    assert counts["departures"]["count"] == 1
    assert counts["departures"]["received"] == 2
    aircraft = counts["aircraft_in_search_box"]
    assert (aircraft["count"], aircraft["received"], aircraft["unreadable"]) == (2, 3, 1)
    assert (aircraft["airborne"], aircraft["on_ground"]) == (1, 1)
    assert counts["movements_window_minutes"] == 60
    assert counts["search_radius_nm"] == 40.0

    weather = triage["weather"]
    assert weather["category"] == "VFR"
    assert weather["ceiling"] == {"ft": 3500, "basis": "BKN"}
    assert weather["visibility"] == {"statute_mi": 6.0, "at_least": True}
    assert weather["wind"] == {
        "direction_deg": 240.0,
        "variable": False,
        "speed_kt": 12.0,
        "gust_kt": 20.0,
    }
    assert triage["assessment"]["severity"] == "low"


async def test_each_leg_carries_its_own_age_by_its_own_upstream_clock(
    world: Callable[..., Runtime],
) -> None:
    world(
        metars=[metar("ESGG", "VFR", minutes_ago=12)],
        states=[state("a")],
        arrivals=[flight("a", NOW - 900)],
    )

    legs = (await srv.disruption_triage("ESGG"))["legs"]

    assert legs["weather"]["status"] == "observed"
    assert legs["weather"]["age_s"] == 720
    assert legs["weather"]["upstream_time_epoch"] == NOW
    assert legs["traffic"]["age_s"] == 5
    assert legs["movements"]["age_s"] == 900
    assert legs["movements"]["source"].startswith("OpenSky Network /flights/")


async def test_missing_legs_are_named_one_by_one(world: Callable[..., Runtime]) -> None:
    world(metars=[metar("ESGG", "IFR")], states_status=503, flights_status=503)

    triage = await srv.disruption_triage("ESGG")

    legs = triage["legs"]
    assert legs["weather"]["status"] == "observed"
    for name in ("traffic", "movements"):
        assert legs[name]["status"] == "unavailable"
        assert legs[name]["error"] == "upstream_unavailable"
        assert legs[name]["age_s"] is None
    assert triage["counts"]["arrivals"] is None
    assert triage["counts"]["aircraft_in_search_box"] is None
    # The rule holds: unknown outranks the elevated the weather alone would give.
    assessment = triage["assessment"]
    assert assessment["severity"] == "unknown"
    assert any("alone would be elevated" in line for line in assessment["why_not_higher"])
    assert assessment["why_not_lower"] == [
        "Low needs every leg to answer; movements unavailable: upstream_unavailable, "
        "traffic unavailable: upstream_unavailable."
    ]


async def test_low_needs_something_observed_and_says_why_it_is_not_higher(
    world: Callable[..., Runtime],
) -> None:
    world(
        metars=[metar("ESGG", "VFR", wspd=8, visib=10)],
        states=[state("a")],
        arrivals=[flight("a", NOW - 900)],
    )

    assessment = (await srv.disruption_triage("ESGG"))["assessment"]

    assert assessment["severity"] == "low"
    assert assessment["why_not_lower"] == ["Low is the bottom of this ladder."]
    assert assessment["why_not_higher"] == [
        "No reason to raise it was observed.",
        "Flight category VFR, not IFR or LIFR.",
        "Wind 8 kt with no gusts reported.",
        "Visibility 10 sm, not below 1.5 sm.",
        "No significant weather group in the METAR.",
        "1 aircraft in the search box, not more than 25.",
        "1 arrivals and 0 departures in the last 60 min: traffic is moving.",
    ]


async def test_nothing_observed_anywhere_is_unknown_and_says_why_not_low(
    world: Callable[..., Runtime],
) -> None:
    world()

    assessment = (await srv.disruption_triage("ESGG"))["assessment"]

    assert assessment["severity"] == "unknown"
    assert assessment["why_not_lower"] == [
        "Low needs positive evidence from at least one leg; none had any."
    ]


async def test_high_names_the_top_rung_and_each_reason(world: Callable[..., Runtime]) -> None:
    world(
        metars=[metar("ESGG", "LIFR", wgst=45, visib=0.3, wxString="FG")],
        states=[state("a")],
        arrivals=[flight("a", NOW - 900)],
    )

    assessment = (await srv.disruption_triage("ESGG"))["assessment"]

    assert assessment["severity"] == "high"
    assert assessment["why_not_higher"] == ["High is the top of this ladder."]
    assert assessment["why_not_lower"][0] == "4 distinct reasons; high needs 3."
    assert assessment["why_not_lower"][1:] == assessment["reasons"]
