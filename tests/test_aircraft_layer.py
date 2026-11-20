"""The aircraft layer and the legible wide view: decisions held in Python.

Selection (filters, sorts, the cap), the airline derived from a callsign, the globe's
altitude lift and far-side cull, which airports are asked about, how they cluster, and
the toggle for airports without a report.
"""

from __future__ import annotations

import copy
import csv
import inspect
import random
import re
from pathlib import Path
from typing import Any

import httpx
import pytest

from dashboard import aircraft, board, operator
from dashboard.aircraft import Selection, select
from dashboard.app import create_app
from dashboard.gate import Gate
from dashboard.service import BoardService, UnknownBoardItem, View

NOW = 1_795_126_400
ESGG = {"icao": "ESGG", "latitude": 57.6628, "longitude": 12.2798}
STATIC = Path(__file__).parent.parent / "dashboard" / "static"


def plane(icao24: str, **fields: Any) -> dict[str, Any]:
    """A drawn aircraft, as board._aircraft makes it."""
    entry = {
        "icao24": icao24,
        "callsign": f"SAS{int(icao24, 16) % 9000 + 100}",
        "latitude": 57.7,
        "longitude": 12.3,
        "on_ground": False,
        "track": 90.0,
        "altitude_m": 9000.0,
        "velocity_ms": 220.0,
        "lift": board.globe_lift(9000.0),
        "last_contact": NOW - 5,
        "heard_utc": "12:00:00Z",
        "age_s": 5,
        "fade": 0.96,
    }
    entry.update(fields)
    return entry


def fleet(count: int, seed: int = 7) -> list[dict[str, Any]]:
    """`count` aircraft spread around ESGG, with every kind of gap in them."""
    rng = random.Random(seed)  # noqa: S311 - a seeded fixture, not a secret
    out = []
    for n in range(count):
        out.append(
            plane(
                f"{n:06x}",
                latitude=57.66 + rng.uniform(-4, 4),
                longitude=12.28 + rng.uniform(-6, 6),
                altitude_m=None if n % 11 == 0 else rng.uniform(0, 12_000),
                velocity_ms=None if n % 13 == 0 else rng.uniform(0, 260),
                callsign=["BAW1", "NOZ12", "RYR3AB", "N123AB", None][n % 5],
                age_s=rng.randint(0, 200),
                on_ground=n % 17 == 0,
            )
        )
    return out


# --- the airline, derived from the callsign --------------------------------------------
@pytest.mark.parametrize(
    ("callsign", "code", "name"),
    [
        ("NOZ1260", "NOZ", "Norwegian Air Norway"),
        ("BAW791G", "BAW", "British Airways"),
        ("FIN7LP", "FIN", "Finnair"),
        # The designator alone: an operator with no flight number.
        ("RYR", "RYR", "Ryanair"),
        ("noz1260", "NOZ", "Norwegian Air Norway"),
        ("SAS4047 ", "SAS", "Scandinavian Airlines"),
        # A designator the table does not hold: the prefix, and no name.
        ("QQQ123", "QQQ", None),
    ],
)
def test_an_airline_is_derived_from_a_designator(callsign: str, code: str, name: str) -> None:
    derived = aircraft.airline(callsign)

    assert derived is not None
    assert (derived["designator"], derived["name"], derived["derived_from"]) == (
        code,
        name,
        "callsign",
    )


@pytest.mark.parametrize("callsign", ["N12345", "", "   ", None, "GABCD", "DLH12345", "12ABC", 7])
def test_a_registration_or_nothing_is_not_an_airline(callsign: object) -> None:
    assert aircraft.designator(callsign) is None
    assert aircraft.airline(callsign) is None


def test_the_inspector_names_a_known_airline_and_leaves_an_unknown_bare() -> None:
    rows = {r["label"]: r["text"] for r in aircraft.aircraft_rows(plane("abc123"), None)}
    unknown = {
        r["label"]: r["text"]
        for r in aircraft.aircraft_rows(plane("abc124", callsign="QQQ1"), None)
    }
    tail = {
        r["label"]: r["text"]
        for r in aircraft.aircraft_rows(plane("abc125", callsign="N123AB"), None)
    }

    assert rows["Airline"].startswith("Scandinavian Airlines (SAS, SE), from the callsign")
    assert unknown["Airline"] == "QQQ: not in the bundled table"
    assert tail["Airline"] == "none: the callsign is not an airline designator"


def test_the_bundled_table_is_well_formed_and_documented() -> None:
    with operator.AIRLINE_TABLE.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    codes = [row["designator"] for row in rows]

    assert list(rows[0]) == ["designator", "name", "country"]
    assert codes == sorted(codes), "kept sorted, so a duplicate shows up next to its twin"
    assert len(codes) == len(set(codes))
    assert all(re.fullmatch(r"[A-Z]{3}", code) for code in codes)
    assert all(row["name"].strip() and re.fullmatch(r"[A-Z]{2}", row["country"]) for row in rows)
    readme = (operator.AIRLINE_TABLE.parent / "README.md").read_text(encoding="utf-8")
    assert "Licence" in readme and "To add a designator" in readme


# --- filters, sorts and the cap ---------------------------------------------------------
def test_the_cap_draws_the_same_aircraft_whatever_order_they_arrive_in() -> None:
    planes = fleet(1183)
    shuffled = planes[:]
    random.Random(1).shuffle(shuffled)  # noqa: S311 - a seeded fixture, not a secret
    choice = Selection(airport=ESGG, sort="distance", cap=200)

    first = select(planes, choice, NOW)
    second = select(shuffled, choice, NOW)

    assert [p["icao24"] for p in first["aircraft"]] == [p["icao24"] for p in second["aircraft"]]
    assert first["shown"] == 200
    distances = [p["distance_nm"] for p in first["aircraft"]]
    assert distances == sorted(distances)


def test_showing_x_of_y_counts_each_control() -> None:
    planes = fleet(1183)
    choice = Selection(airport=ESGG, radius_nm=250, phase="airborne", cap=200)

    chosen = select(planes, choice, NOW)

    in_radius = sum(
        1
        for p in planes
        if board.haversine_nm(ESGG["latitude"], ESGG["longitude"], p["latitude"], p["longitude"])
        <= 250
    )
    airborne = sum(
        1
        for p in planes
        if not p["on_ground"]
        and board.haversine_nm(ESGG["latitude"], ESGG["longitude"], p["latitude"], p["longitude"])
        <= 250
    )
    assert (chosen["in_radius"], chosen["filtered"], chosen["shown"]) == (in_radius, airborne, 200)
    assert chosen["text"] == (
        f"{in_radius:,} within 250 nm of ESGG · filtered to {airborne:,} · "
        "sorted by distance · showing 200"
    )
    assert chosen["dropped"] == (
        f"showing 200 of {airborne:,} — raise the cap or narrow the filter"
    )


def test_nothing_dropped_says_nothing_about_the_cap() -> None:
    chosen = select(fleet(30), Selection(cap=200), NOW)

    assert chosen["dropped"] is None
    assert chosen["text"] == "30 heard · sorted by last heard · showing 30"


def test_a_null_altitude_sorts_after_every_known_one_and_is_in_no_band() -> None:
    planes = [
        plane("000001", altitude_m=None),
        plane("000002", altitude_m=0.0, on_ground=True),
        plane("000003", altitude_m=4000.0),
        plane("000004", altitude_m=None),
        plane("000005", altitude_m=12000.0),
    ]

    by_altitude = select(planes, Selection(sort="altitude"), NOW)["aircraft"]
    low = select(planes, Selection(band="low"), NOW)

    assert [p["icao24"] for p in by_altitude] == ["000002", "000003", "000005", "000001", "000004"]
    assert [p["icao24"] for p in low["aircraft"]] == ["000002"]


@pytest.mark.parametrize(
    ("band", "kept"),
    [("low", ["a2999"]), ("mid", ["a3000", "a8999"]), ("high", ["a9000"])],
)
def test_the_altitude_bands_meet_without_a_gap(band: str, kept: list[str]) -> None:
    planes = [
        plane("a2999", altitude_m=2999.9),
        plane("a3000", altitude_m=3000.0),
        plane("a8999", altitude_m=8999.9),
        plane("a9000", altitude_m=9000.0),
    ]

    chosen = select(planes, Selection(band=band, sort="callsign"), NOW)  # type: ignore[arg-type]

    assert sorted(p["icao24"] for p in chosen["aircraft"]) == kept


def test_each_sort_is_in_its_stated_direction_with_unknowns_last() -> None:
    planes = [
        plane("000001", velocity_ms=100.0, callsign="BAW1", age_s=30),
        plane("000002", velocity_ms=None, callsign=None, age_s=None),
        plane("000003", velocity_ms=250.0, callsign="AAL9", age_s=3),
    ]

    def order(sort: str) -> list[str]:
        chosen = select(planes, Selection(sort=sort), NOW)  # type: ignore[arg-type]
        return [p["icao24"] for p in chosen["aircraft"]]

    assert order("speed") == ["000003", "000001", "000002"]
    assert order("callsign") == ["000003", "000001", "000002"]
    assert order("last_heard") == ["000003", "000001", "000002"]


def test_distance_without_an_airport_falls_back_and_says_so() -> None:
    chosen = select(fleet(10), Selection(sort="distance"), NOW)

    assert chosen["sort"] == "last_heard"
    assert chosen["fallback"] == aircraft.NO_AIRPORT_SORT
    assert all(p["distance_nm"] is None for p in chosen["aircraft"])


def test_airline_and_search_filters_and_their_choices() -> None:
    planes = fleet(50)

    by_airline = select(planes, Selection(airline="NOZ"), NOW)
    searched = select(planes, Selection(query="ryr3"), NOW)

    assert {p["callsign"] for p in by_airline["aircraft"]} == {"NOZ12"}
    assert {p["callsign"] for p in searched["aircraft"]} == {"RYR3AB"}
    assert [c["designator"] for c in by_airline["airlines"]] == ["BAW", "NOZ", "RYR"]
    assert by_airline["airlines"][0] == {
        "designator": "BAW",
        "name": "British Airways",
        "count": 10,
    }


def test_an_aircraft_without_a_position_is_not_drawn_but_counted() -> None:
    planes = [plane("000001"), plane("000002", latitude=None), plane("000003", longitude=None)]

    chosen = select(planes, Selection(), NOW)

    assert [p["icao24"] for p in chosen["aircraft"]] == ["000001"]
    assert chosen["no_position"] == 2


# --- focus and follow -------------------------------------------------------------------
def test_the_focused_aircraft_is_kept_outside_the_cap_and_the_filter() -> None:
    planes = fleet(400)
    far = max(planes, key=lambda p: abs(p["latitude"] - ESGG["latitude"]))
    far["age_s"] = 10

    chosen = select(
        planes, Selection(airport=ESGG, cap=50, phase="ground", focus=far["icao24"]), NOW
    )

    assert far["icao24"] not in {p["icao24"] for p in chosen["aircraft"]}
    assert chosen["focus"]["state"] == "reported"
    assert chosen["focus"]["aircraft"]["icao24"] == far["icao24"]
    labels = [row["label"] for row in chosen["focus"]["rows"]]
    assert labels == [
        "Callsign",
        "Airline",
        "Class",
        "ICAO24",
        "Altitude",
        "Speed",
        "Heading",
        "On the ground",
        "Last heard",
        "From ESGG",
    ]


def test_follow_stops_for_a_stale_aircraft_and_says_which_happened() -> None:
    stale = select(
        [plane("abc123", age_s=121, callsign="SAS2711")], Selection(focus="abc123"), NOW
    )["focus"]
    unheard = select([plane("abc123", age_s=None)], Selection(focus="abc123"), NOW)["focus"]
    gone = select([plane("abc123")], Selection(focus="def456"), NOW)["focus"]
    no_clock = select([plane("abc123")], Selection(focus="abc123"), None)["focus"]
    edge = select([plane("abc123", age_s=120)], Selection(focus="abc123"), NOW)["focus"]

    assert (stale["state"], stale["follow_stopped"]) == (
        "stale",
        "Follow stopped: SAS2711 was last heard 2 min ago.",
    )
    assert stale["reason"] == "SAS2711 was last heard 2 min ago."
    assert unheard["state"] == "stale"
    assert (gone["state"], gone["follow_stopped"]) == (
        "not_reported",
        "Follow stopped: def456 is not in the newest snapshot.",
    )
    assert no_clock["state"] == "stale"
    assert (edge["state"], edge["reason"], edge["follow_stopped"]) == ("reported", None, None)


def test_the_inspector_gives_both_units_and_never_a_zero_for_unknown() -> None:
    known = {r["label"]: r["text"] for r in aircraft.aircraft_rows(plane("abc123"), None)}
    blank = {
        r["label"]: r["text"]
        for r in aircraft.aircraft_rows(
            plane("abc123", altitude_m=None, velocity_ms=None, track=None, age_s=None), None
        )
    }

    assert known["Altitude"] == "9,000 m (29,528 ft), barometric"
    assert known["Speed"] == "220 m/s (428 kt)"
    assert known["Heading"] == "090°"
    assert known["From airport"] == "no airport selected"
    assert (blank["Altitude"], blank["Speed"], blank["Heading"], blank["Last heard"]) == (
        "not reported",
        "not reported",
        "not reported",
        "unknown",
    )


# --- transit, estimated arrival, destination: three lines --------------------------------
TRACK_PAYLOAD = {
    "ok": True,
    "transit": {
        "columns": [
            "time",
            "latitude",
            "longitude",
            "baro_altitude_m",
            "true_track_deg",
            "on_ground",
        ],
        "waypoints": [[NOW - 3000 + 300 * n, 57 + n * 0.3, 12, 9000, 10, False] for n in range(11)],
        "first_waypoint_epoch": NOW - 3000,
        "last_waypoint_epoch": NOW,
    },
    "estimated_arrival": {
        "state": "no_record_for_this_flight",
        "previous_flight": {
            "departure_airport": "OMAD",
            "arrival_airport": "EGLL",
            "last_seen_epoch": NOW - 40_000,
        },
    },
    "destination": {"state": "no_source", "text": "No source. A transponder broadcasts position."},
    "bookkeeping": {"credits_spent": 34},
    "unknowns": [],
}


def test_the_inspector_keeps_three_kinds_of_claim_on_three_lines() -> None:
    view = aircraft.track_view(TRACK_PAYLOAD)

    lines = {row["label"]: row["text"] for row in view["lines"]}
    assert list(lines) == ["Transit", "Estimated arrival", "Destination"]
    assert lines["Transit"].startswith("observed: 11 waypoints, ")
    assert "nothing drawn past the last" in lines["Transit"]
    assert lines["Estimated arrival"].startswith("no record for this flight yet")
    assert "OMAD → EGLL" in lines["Estimated arrival"]
    assert "not a schedule" in lines["Estimated arrival"]
    assert lines["Destination"].startswith("No source.")
    # Every waypoint keeps its own time, and the labels include both ends.
    assert all(w["time_utc"] for w in view["waypoints"])
    assert view["labels"][0] == 0 and view["labels"][-1] == 10


@pytest.mark.parametrize(
    ("arrival", "text"),
    [
        ({"state": "estimated", "airport": "ENGM"}, "ENGM — estimated from ADS-B, not a schedule"),
        ({"state": "not_requested"}, "not requested — asking OpenSky costs 30 credits"),
        (
            {"state": "unavailable", "error": "credentials_missing"},
            "unavailable (credentials_missing)",
        ),
        (
            {"state": "not_estimated"},
            "OpenSky lists this flight but estimated no arrival airport for it",
        ),
    ],
)
def test_each_estimate_state_reads_as_what_it_is(arrival: dict[str, Any], text: str) -> None:
    assert aircraft.arrival_text(arrival) == text


def test_a_failed_track_claims_no_path() -> None:
    view = aircraft.track_view({"ok": False, "error": {"code": "upstream_unavailable"}})

    assert view["waypoints"] == []
    assert view["lines"][0]["text"] == "unavailable (upstream_unavailable)"


@pytest.mark.parametrize(
    ("count", "labels"),
    [(0, []), (1, [0]), (2, [0, 1]), (11, [0, 2, 4, 6, 8, 10]), (12, [0, 3, 6, 9, 11])],
)
def test_transit_labels_include_both_ends(count: int, labels: list[int]) -> None:
    assert aircraft.transit_labels(count) == labels


# --- the globe: lifted by altitude, culled by position ------------------------------------
@pytest.mark.parametrize(
    ("altitude", "lift"),
    [(None, None), (0.0, 0.0), (6000.0, 0.0175), (12000.0, 0.035), (20000.0, 0.035), (-60.0, 0.0)],
)
def test_the_lift_is_proportional_to_altitude_and_clamped(
    altitude: float | None, lift: float | None
) -> None:
    assert board.globe_lift(altitude) == lift


def test_the_page_lifts_an_aircraft_radially_and_an_unknown_altitude_not_at_all() -> None:
    """The globe's projection runs in the page, so the page's code is what is held here.

    Lifting a point radially in 3-D is, once projected orthographically, scaling its ground
    point outward from the projection's centre by `1 + lift`. A missing lift draws it on the
    surface. Python once kept its own copy of this projection; nothing called it, and its
    limb test had already drifted from the page's.
    """
    script = (STATIC / "board.js").read_text(encoding="utf-8")
    point = re.search(r"function aircraftPoint\(aircraft\) \{(.*?)\n  \}", script, re.S)

    assert point is not None
    body = point.group(1)
    assert "if (!surface) return null;" in body
    assert "!aircraft.lift) return { surface, at: surface };" in body
    assert "const [cx, cy] = projection.translate();" in body
    assert "const k = 1 + aircraft.lift;" in body
    assert "at: [cx + (surface[0] - cx) * k, cy + (surface[1] - cy) * k]" in body


def test_the_page_culls_aircraft_with_the_airports_rule() -> None:
    """The per-frame cull runs in the page. It is one function, and both layers use it."""
    script = (STATIC / "board.js").read_text(encoding="utf-8")
    place = re.search(r"function place\(lon, lat\) \{(.*?)\n  \}", script, re.S)

    assert place is not None
    assert "d3.geoDistance([lon, lat], [state.lon, state.lat]) > Math.PI / 2" in place.group(1)
    assert re.search(r"function drawAirport\(.*?place\(", script, re.S)
    assert re.search(
        r"function aircraftPoint\(.*?place\(aircraft\.longitude, aircraft\.latitude\)", script, re.S
    )


# --- the wide view: viewport, clusters, the unknown toggle --------------------------------
def airport(ident: str, lat: float, lon: float, **extra: Any) -> dict[str, Any]:
    return {
        "ident": ident,
        "iata": None,
        "name": f"{ident} Airport",
        "type": "large_airport",
        "latitude": lat,
        "longitude": lon,
        "continent": "EU",
        "municipality": None,
        "iso_country": "XX",
        **extra,
    }


def test_the_view_asks_about_the_nearest_airports_up_to_the_cap() -> None:
    airports = [airport(f"A{n:03d}", 0.0, float(n) / 10) for n in range(700)]

    chosen, in_view = board.airports_in_view(airports, 0.0, 0.0, 180.0, cap=600)

    assert in_view == 700
    assert [a["ident"] for a in chosen] == [f"A{n:03d}" for n in range(600)]


def test_the_view_is_a_circle_that_crosses_the_antimeridian() -> None:
    airports = [airport("EAST", 0.0, 179.0), airport("WEST", 0.0, -179.0), airport("FAR", 0.0, 0.0)]

    chosen, in_view = board.airports_in_view(airports, 0.0, 179.5, 1.0)

    assert {a["ident"] for a in chosen} == {"EAST", "WEST"}
    assert in_view == 2


def test_clusters_shrink_as_the_reader_zooms_in() -> None:
    """The page picks the cell per frame from the levels the payload carries (`display`)."""
    script = (STATIC / "board.js").read_text(encoding="utf-8")
    cell = re.search(r"function clusterCell\(\) \{(.*?)\n  \}", script, re.S)

    assert cell is not None
    assert (
        "for (const [below, cell] of state.display.cluster_levels) "
        "if (state.zoom < below) return cell;"
    ) in cell.group(1)
    # Strictly finer as the zoom rises, and individual airports from the last level on.
    zooms = [below for below, _ in board.CLUSTER_LEVELS]
    cells = [size for _, size in board.CLUSTER_LEVELS]
    assert zooms == sorted(zooms) and cells == sorted(cells, reverse=True)
    assert len(set(cells)) == len(cells)


def test_unknowns_are_hidden_by_default_only_at_wide_zoom() -> None:
    """Shown by default from the payload's zoom on, until the reader chooses."""
    script = (STATIC / "board.js").read_text(encoding="utf-8")
    shown = re.search(r"function unknownShown\(\) \{(.*?)\n  \}", script, re.S)

    assert shown is not None
    assert "if (state.showUnknown !== null) return state.showUnknown;" in shown.group(1)
    assert "state.zoom >= state.display.unknown_shown_from_zoom" in shown.group(1)


def _entry(ident: str, lat: float, lon: float, category: str | None) -> dict[str, Any]:
    marker = board.marker({"state": "reported", "category": category}) if category else None
    record = airport(ident, lat, lon)
    view = {"summary": "x", "neighbour": None}
    return board.board_airport(record, view, marker or board.marker({"state": "unknown"}))


def test_a_cluster_counts_each_category_and_shows_its_worst() -> None:
    entries = [
        _entry("A", 50.0, 10.0, "VFR"),
        _entry("B", 51.0, 11.0, "VFR"),
        _entry("C", 52.0, 12.0, "IFR"),
        _entry("D", 53.0, 13.0, None),
        _entry("E", -30.0, 100.0, None),
    ]

    # Cells are ordered south to north, so the southern one comes first.
    nowhere, europe = board.clusters(entries, 20.0)

    assert (europe["reported"], europe["unknown"]) == (3, 1)
    # Drawn as what most of it is, with the worst one named for its badge.
    assert (europe["majority"], europe["worst"]) == ("VFR", "IFR")
    assert europe["counts"] == {"VFR": 2, "MVFR": 0, "IFR": 1, "LIFR": 0}
    assert europe["text"] == "◆ 1 IFR · ● 2 VFR"
    assert europe["text_with_unknown"] == "◆ 1 IFR · ● 2 VFR · ○ 1 without a report"
    # Placed among the airports that report, not at the cell's corner.
    assert (europe["latitude"], europe["longitude"]) == (51.0, 11.0)
    assert (nowhere["reported"], nowhere["majority"], nowhere["worst"], nowhere["text"]) == (
        0,
        None,
        None,
        "no report heard",
    )


def test_a_tie_for_the_majority_goes_to_the_worse_category() -> None:
    (cell,) = board.clusters([_entry("A", 50.0, 10.0, "VFR"), _entry("B", 51.0, 11.0, "IFR")], 10.0)

    assert (cell["majority"], cell["worst"]) == ("IFR", "IFR")


def test_the_hidden_count_and_the_lookups_are_always_worded() -> None:
    assert board.hidden_note(1888) == "1,888 hidden — no report heard"
    assert board.hidden_note(0) == "none hidden: every airport asked has a report"
    assert board.viewport_note(3270, 3270, 600, 600, 6) == (
        "Weather showing 600 of 3,270 nearest the centre · 600 lookups in 6 calls"
    )
    assert board.viewport_note(3270, 41, 41, 41, 1) == (
        "Weather showing all 41 in view (3,270 in the region) · 41 lookups in 1 call"
    )


# --- the service and its routes ----------------------------------------------------------
class FakeSource:
    recorded_at: None = None

    def __init__(self, **answers: Any) -> None:
        self.answers = answers
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((tool, arguments))
        answer = self.answers.get(tool, {"ok": False, "error": {"code": "not_scripted"}})
        return answer(arguments) if callable(answer) else copy.deepcopy(answer)


def overview(arguments: dict[str, Any]) -> dict[str, Any]:
    """Every third airport reports VFR; the others have nothing."""
    reported = {
        "ok": True,
        "upstream_time_epoch": NOW,
        "metar": [{"flight_category": "VFR", "observation_time": "2026-11-19T22:50:00Z"}],
        "latest_metar": {"age_s": 600, "current": True},
        "unknowns": [],
    }
    silent = {
        "ok": True,
        "upstream_time_epoch": NOW,
        "metar": [],
        "latest_metar": {},
        "unknowns": [],
    }
    return {
        "ok": True,
        "airports": {
            icao: reported if int(icao[1:]) % 3 == 0 else silent for icao in arguments["airports"]
        },
    }


WORLD = [airport(f"W{n:04d}", -60 + (n * 37) % 130, -180 + (n * 71) % 360) for n in range(3270)]
LAYER = {
    "ok": True,
    "columns": list(
        aircraft_columns := [
            "icao24",
            "callsign",
            "latitude",
            "longitude",
            "baro_altitude_m",
            "on_ground",
            "velocity_ms",
            "true_track_deg",
            "last_contact",
        ]
    ),
    "rows": [
        [f"{n:06x}", "SAS1", 57.0 + n / 100, 12.0, 9000.0, False, 200.0, 90.0, NOW - 5]
        for n in range(300)
    ],
    "snapshot_time_epoch": NOW,
    "upstream_time_epoch": NOW,
    "snapshot_current": True,
    "bookkeeping": {"credits_spent": 4},
}
STATUS = {"credentials": {"opensky": "client_credentials"}, "budget": {}, "cache": {}}


def world_service(**answers: Any) -> tuple[BoardService, FakeSource]:
    source = FakeSource(
        weather_overview=overview,
        upstream_status=STATUS,
        airspace_snapshot=LAYER,
        aircraft_track=TRACK_PAYLOAD,
        **answers,
    )
    for entry in WORLD:
        entry["continent"] = "EU"
    return BoardService(source, WORLD), source


async def test_a_world_poll_asks_about_600_airports_and_draws_under_120_markers() -> None:
    service, source = world_service()

    snapshot = await service.snapshot("world", view=View(20.0, 10.0, 180.0))

    asked = [len(a["airports"]) for tool, a in source.calls if tool == "weather_overview"]
    assert asked == [100] * 6
    assert snapshot["viewport"]["asked"] == 600
    assert snapshot["viewport"]["in_view"] == 3270
    assert snapshot["viewport"]["text"].startswith("Weather showing 600 of 3,270 nearest")
    assert snapshot["hidden"] == {
        "unknown": 400,
        "text": "400 hidden — no report heard",
        "shown_text": "400 without a report, drawn as dashed rings",
    }
    # Default zoom is 1: the 10-degree cells, unknowns off.
    drawn = [c for c in snapshot["clusters"]["10"] if c["reported"]]
    assert 0 < len(drawn) < 120
    assert sum(c["reported"] + c["unknown"] for c in snapshot["clusters"]["10"]) == 600
    assert set(snapshot["clusters"]) == {"10", "5", "2.5"}
    assert snapshot["display"]["cluster_levels"] == [[2.0, 10.0], [3.0, 5.0], [4.0, 2.5]]
    assert snapshot["display"]["unknown_shown_from_zoom"] == board.UNKNOWN_SHOWN_FROM_ZOOM


async def test_an_airport_without_a_report_is_trimmed_to_where_it_is() -> None:
    service, _ = world_service()

    snapshot = await service.snapshot("world", view=View(20.0, 10.0, 180.0))

    trimmed = [a for a in snapshot["airports"] if "marker" not in a]
    full = [a for a in snapshot["airports"] if "marker" in a]
    assert len(trimmed) == 400 and len(full) == 200
    assert all(set(a) == {"icao", "name", "latitude", "longitude"} for a in trimmed)
    assert all(a["marker"]["category"] == "VFR" for a in full)


async def test_the_unknown_toggle_changes_no_payload_field() -> None:
    """The toggle is a render filter in the page: the server is never told of it."""
    service, _ = world_service()

    first = await service.snapshot("world", view=View(20.0, 10.0, 180.0))
    second = await service.snapshot("world", view=View(20.0, 10.0, 180.0))

    assert first == second
    # Everything the page needs for either setting is already in the one payload.
    assert {"hidden", "clusters", "display"} <= set(first)
    assert all({"text", "text_with_unknown", "unknown"} <= set(c) for c in first["clusters"]["10"])
    # No route takes it, and the page sends it nowhere.
    routes = {route.path: route for route in create_app(service, Gate(None)).routes}  # type: ignore[attr-defined]
    params = inspect.signature(routes["/api/board"].endpoint).parameters  # type: ignore[attr-defined]
    assert not [name for name in params if "unknown" in name]
    script = (STATIC / "board.js").read_text(encoding="utf-8")
    requests = re.findall(r"api\(`([^`]*)`", script)
    assert requests and not [path for path in requests if "nknown" in path]


async def test_filters_and_sorts_re_read_the_held_layer_for_free() -> None:
    service, source = world_service()
    choice = service.selection("W0000", radius_nm=None, sort="altitude", cap=50)

    before = service.reselect("world", choice)
    refreshed = await service.airspace("world", choice)
    calls = len(source.calls)
    again = service.reselect("world", service.selection(None, sort="callsign", cap=100))

    assert before["selection"] is None and before["aircraft"] == []
    assert refreshed["selection"]["shown"] == 50 and len(refreshed["aircraft"]) == 50
    assert again["selection"]["shown"] == 100
    assert len(source.calls) == calls, "a re-selection asks no upstream"
    assert service.traffic.session_total == 4


async def test_only_an_aircraft_in_the_held_layer_can_be_tracked() -> None:
    service, source = world_service()
    await service.airspace("world", service.selection(None))

    with pytest.raises(UnknownBoardItem):
        await service.track("world", "ffffff", estimate=False)
    view = await service.track("world", "00000A", estimate=True)

    assert source.calls[-1] == ("aircraft_track", {"icao24": "00000a", "estimate_arrival": True})
    assert [line["label"] for line in view["lines"]] == [
        "Transit",
        "Estimated arrival",
        "Destination",
    ]


async def test_the_routes_take_only_the_offered_choices() -> None:
    service, source = world_service()
    app = create_app(service, Gate(None))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as http:
        layer = (
            await http.get("/api/traffic/world?sort=speed&cap=500&radius_nm=100&airport=W0001")
        ).json()
        bad = [
            (await http.get(f"/api/traffic/world/selection?{query}")).status_code
            for query in ("sort=random", "cap=7", "radius_nm=33", "airline=sas", "focus=xyz")
        ]
        unlisted = await http.get("/api/traffic/world/selection?airport=EGLL")
        track = await http.get("/api/traffic/world/aircraft/00000a/track")
        view = await http.get("/api/board?region=world&lat=60&lon=10&radius=20")

    assert layer["selection"]["sort"] == "speed"
    assert bad == [422] * 5
    assert unlisted.status_code == 404
    assert track.status_code == 200
    assert view.json()["viewport"]["latitude"] == 60.0
    assert [t for t, _ in source.calls].count("airspace_snapshot") == 1


def test_every_tool_the_board_calls_is_one_the_live_source_serves() -> None:
    """Fakes answer any name; the live source answers only those it lists. Found live."""
    from dashboard import service as service_module
    from dashboard.sources import LIVE_TOOLS

    called = set(re.findall(r'call\(\s*"(\w+)"', inspect.getsource(service_module)))

    assert "aircraft_track" in called
    assert called <= set(LIVE_TOOLS)
