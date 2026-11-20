"""The board's geometry and aircraft figures, held by invariants rather than by today's numbers.

Written from the survivors of the aircraft branch's mutation run (docs/MUTATION-SURVIVORS.md).
Each test states a property a wrong implementation breaks for a reason: a distance that is not
symmetric, a cluster that loses an airport, an altitude that turns into 0. Where a boundary
matters it is pinned once, exactly.
"""

from __future__ import annotations

import math
import random
from datetime import UTC, datetime, timedelta, tzinfo
from typing import Any

import pytest

from dashboard import board

NOW = int(datetime(2026, 11, 14, 23, 30, tzinfo=UTC).timestamp())
COLUMNS = [
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


def dot_product_angle(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """The same angle by another route: the arccosine of two unit vectors' dot product."""

    def unit(lat: float, lon: float) -> tuple[float, float, float]:
        phi, lam = math.radians(lat), math.radians(lon)
        return math.cos(phi) * math.cos(lam), math.cos(phi) * math.sin(lam), math.sin(phi)

    a, b = unit(lat1, lon1), unit(lat2, lon2)
    dot = sum(p * q for p, q in zip(a, b, strict=True))
    return math.degrees(math.acos(max(-1.0, min(1.0, dot))))


def random_points(count: int, seed: int) -> list[tuple[float, float]]:
    rng = random.Random(seed)  # noqa: S311 - a seeded fixture, not a secret
    return [(rng.uniform(-90, 90), rng.uniform(-180, 180)) for _ in range(count)]


# --- the angle between two points ---------------------------------------------------------
def test_the_angle_agrees_with_an_independent_formula() -> None:
    points = random_points(400, seed=3)
    for (lat1, lon1), (lat2, lon2) in zip(points, reversed(points), strict=True):
        angle = board.angular_distance_deg(lat1, lon1, lat2, lon2)
        # acos loses digits near 0 and 180; 1e-5 degrees is two metres on the ground.
        assert angle == pytest.approx(dot_product_angle(lat1, lon1, lat2, lon2), abs=1e-5)


def test_the_angle_is_symmetric_zero_for_one_point_and_between_0_and_180() -> None:
    for (lat1, lon1), (lat2, lon2) in zip(*[iter(random_points(400, seed=5))] * 2, strict=True):
        there = board.angular_distance_deg(lat1, lon1, lat2, lon2)
        back = board.angular_distance_deg(lat2, lon2, lat1, lon1)
        assert there == pytest.approx(back, abs=1e-12)
        assert 0.0 <= there <= 180.0
        assert board.angular_distance_deg(lat1, lon1, lat1, lon1) == 0.0


def test_a_quarter_turn_on_the_equator_is_90_and_antipodes_are_180() -> None:
    assert board.angular_distance_deg(0.0, 0.0, 0.0, 90.0) == pytest.approx(90.0, abs=1e-9)
    assert board.angular_distance_deg(0.0, -45.0, 0.0, 45.0) == pytest.approx(90.0, abs=1e-9)
    assert board.angular_distance_deg(0.0, 0.0, 90.0, 0.0) == pytest.approx(90.0, abs=1e-9)
    for lat, lon in [(0.0, 0.0), (8.0, 0.0), (45.0, 30.0), (-60.0, 170.0)]:
        antipode = (-lat, lon + 180.0 if lon <= 0 else lon - 180.0)
        # Haversine near 180 is ill-conditioned: its answer is good to a few millionths.
        assert board.angular_distance_deg(lat, lon, *antipode) == pytest.approx(180.0, abs=1e-5)


# --- the view: which airports are asked about ----------------------------------------------
def airport(ident: str, lat: float, lon: float, **extra: Any) -> dict[str, Any]:
    return {
        "ident": ident,
        "iata": None,
        "name": f"{ident} Airport",
        "type": "small_airport",
        "latitude": lat,
        "longitude": lon,
        **extra,
    }


def test_an_airport_exactly_on_the_edge_of_the_view_is_in_it() -> None:
    # Radius 2 plus the 5-degree margin is 7; this angle comes out as exactly 7.0.
    reach = 2.0 + board.VIEW_MARGIN_DEG
    edge, beyond = airport("EDGE", 0.0, reach), airport("PAST", 0.0, reach + 0.01)
    assert board.angular_distance_deg(0.0, 0.0, 0.0, reach) == reach

    chosen, in_view = board.airports_in_view([edge, beyond], 0.0, 0.0, 2.0)

    assert [a["ident"] for a in chosen] == ["EDGE"]
    assert in_view == 1


def test_the_margin_widens_the_view_and_does_not_narrow_it() -> None:
    airports = [airport("NEAR", 0.0, 12.0), airport("FAR", 0.0, 16.0)]

    chosen, in_view = board.airports_in_view(airports, 0.0, 0.0, 10.0)

    assert [a["ident"] for a in chosen] == ["NEAR"]
    assert in_view == 1


@pytest.mark.parametrize("cap", [0, -1, -5])
def test_a_cap_of_zero_or_less_asks_about_nothing_and_still_counts(cap: int) -> None:
    """A negative cap must not slice from the end of the list and ask about most of it."""
    airports = [airport(f"A{n}", 0.0, float(n)) for n in range(6)]

    chosen, in_view = board.airports_in_view(airports, 0.0, 0.0, 10.0, cap=cap)

    assert chosen == []
    assert in_view == 6


# --- the globe's lift ------------------------------------------------------------------------
def test_the_lift_is_given_to_five_decimals() -> None:
    # 1 km is 0.0029166... of the radius's 3.5 % ceiling share.
    assert board.globe_lift(1000.0) == 0.00292
    assert board.globe_lift(1.0) == 0.0


# --- an aircraft as the board draws it ------------------------------------------------------
def layer(*rows: list[Any]) -> dict[str, Any]:
    return {
        "ok": True,
        "columns": COLUMNS,
        "rows": [list(row) for row in rows],
        "snapshot_time_epoch": NOW,
        "upstream_time_epoch": NOW,
        "snapshot_current": True,
    }


def row(**fields: Any) -> list[Any]:
    entry = {
        "icao24": "abc123",
        "callsign": "SAS1",
        "latitude": 57.7,
        "longitude": 12.3,
        "baro_altitude_m": 9000.0,
        "on_ground": False,
        "velocity_ms": 200.0,
        "true_track_deg": 90.0,
        "last_contact": NOW - 30,
        **fields,
    }
    return [entry[column] for column in COLUMNS]


def test_altitude_speed_and_lift_come_from_the_answer() -> None:
    (drawn,) = board.airspace_view(layer(row()))["aircraft"]

    assert (drawn["altitude_m"], drawn["velocity_ms"]) == (9000.0, 200.0)
    # The lift is the altitude's, not a constant: 9 km is three quarters of the ceiling.
    assert drawn["lift"] == board.globe_lift(9000.0) == 0.02625
    assert (drawn["age_s"], drawn["heard_utc"]) == (30, "23:29:30Z")


@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf, True, "9000", None])
def test_a_figure_that_is_not_a_finite_number_is_unknown_never_zero(bad: object) -> None:
    (drawn,) = board.airspace_view(layer(row(baro_altitude_m=bad, velocity_ms=bad)))["aircraft"]

    assert drawn["altitude_m"] is None
    assert drawn["velocity_ms"] is None
    assert drawn["lift"] is None


@pytest.mark.parametrize("heard", [None, 0, -5, True, board.MAX_SNAPSHOT_EPOCH + 1])
def test_an_unusable_contact_time_has_no_time_of_day_and_no_age(heard: object) -> None:
    (drawn,) = board.airspace_view(layer(row(last_contact=heard)))["aircraft"]

    assert drawn["last_contact"] is None
    assert drawn["heard_utc"] is None


class _KolkataHost(datetime):
    """`datetime` on a host whose local zone is UTC+05:30. CI runs in UTC, where a missing
    zone argument changes nothing; this is the machine on which it changes the label."""

    @classmethod
    def fromtimestamp(cls, t: float, tz: tzinfo | None = None) -> datetime:  # type: ignore[override]
        if tz is None:
            local = datetime.fromtimestamp(t, UTC) + timedelta(hours=5, minutes=30)
            return local.replace(tzinfo=None)
        return datetime.fromtimestamp(t, tz)


def test_the_time_heard_is_utc_whatever_the_hosts_zone(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(board, "datetime", _KolkataHost)

    (drawn,) = board.airspace_view(layer(row()))["aircraft"]

    assert drawn["heard_utc"] == "23:29:30Z"


# --- an airport as the map receives it -------------------------------------------------------
def test_a_reported_airport_keeps_its_codes_and_type() -> None:
    record = airport("ESGG", 57.66, 12.28, iata="GOT", type="large_airport")
    weather = {"state": "reported", "category": "VFR"}
    marker = board.marker(weather)

    assert board.board_airport(record, weather, marker) == {
        "icao": "ESGG",
        "name": "ESGG Airport",
        "latitude": 57.66,
        "longitude": 12.28,
        "cat": "V",
        "iata": "GOT",
        "type": "large_airport",
        "weather": weather,
        "marker": marker,
    }


# --- clusters ----------------------------------------------------------------------------------
def entries(count: int, seed: int) -> list[dict[str, Any]]:
    """Board entries over the whole globe, a fifth of them without a report."""
    rng = random.Random(seed)  # noqa: S311 - a seeded fixture, not a secret
    out = []
    for n in range(count):
        record = airport(f"X{n:04d}", rng.uniform(-90, 90), rng.uniform(-180, 180))
        category = rng.choice([*board.CATEGORY_ORDER, None])
        weather = {"state": "reported", "category": category} if category else {"state": "unknown"}
        out.append(board.board_airport(record, weather, board.marker(weather)))
    return out


@pytest.mark.parametrize("cell", [cell for _, cell in board.CLUSTER_LEVELS])
def test_every_airport_is_in_exactly_one_cluster_and_counted_once(cell: float) -> None:
    airports = entries(2000, seed=11)
    reported = [a for a in airports if "marker" in a]

    cells = board.clusters(airports, cell)

    assert sum(c["reported"] + c["unknown"] for c in cells) == len(airports)
    assert sum(c["reported"] for c in cells) == len(reported)
    for category in board.CATEGORY_ORDER:
        expected = sum(1 for a in reported if a["marker"]["category"] == category)
        assert sum(c["counts"][category] for c in cells) == expected
    for c in cells:
        assert sum(c["counts"].values()) == c["reported"]


@pytest.mark.parametrize("cell", [cell for _, cell in board.CLUSTER_LEVELS])
def test_each_cluster_sits_inside_one_cell_of_the_grid(cell: float) -> None:
    """A cluster is placed at the mean of its airports, so it lies inside the cell they share."""
    cells = board.clusters(entries(2000, seed=13), cell)

    boxes = [
        (math.floor((c["latitude"] + 90) / cell), math.floor((c["longitude"] + 180) / cell))
        for c in cells
    ]
    assert len(set(boxes)) == len(cells)
    # The grids nest, so halving the cell never merges two clusters into one.
    assert len(cells) >= len(board.clusters(entries(2000, seed=13), cell * 2))


def test_the_badge_is_the_worst_category_and_the_shape_the_most_common() -> None:
    for c in board.clusters(entries(2000, seed=17), 10.0):
        present = [k for k in board.CATEGORY_ORDER if c["counts"][k]]
        assert c["worst"] == (present[-1] if present else None)
        if present:
            top = max(c["counts"][k] for k in present)
            assert c["majority"] == [k for k in present if c["counts"][k] == top][-1]
        else:
            assert c["majority"] is None


def test_a_cluster_is_placed_at_the_mean_to_three_decimals() -> None:
    record = [("A", 50.0, 10.0), ("B", 50.0, 10.0), ("C", 51.0, 11.0)]
    weather = {"state": "reported", "category": "VFR"}
    airports = [
        board.board_airport(airport(i, lat, lon), weather, board.marker(weather))
        for i, lat, lon in record
    ]

    (cell,) = board.clusters(airports, 10.0)

    assert (cell["latitude"], cell["longitude"]) == (50.333, 10.333)


def test_one_airport_is_one_marker() -> None:
    weather = {"state": "reported", "category": "MVFR"}
    one = board.board_airport(airport("ONE", 12.5, -40.25), weather, board.marker(weather))

    (cell,) = board.clusters([one], 2.5)

    assert (cell["latitude"], cell["longitude"], cell["reported"]) == (12.5, -40.25, 1)
    assert (cell["majority"], cell["worst"], cell["text"]) == ("MVFR", "MVFR", "■ 1 MVFR")


# --- the toggle's notes ------------------------------------------------------------------------
def test_the_shown_note_counts_what_it_draws() -> None:
    assert board.shown_note(0) == "every airport asked has a report"
    assert board.shown_note(1) == "1 without a report, drawn as dashed rings"
    assert board.shown_note(1888) == "1,888 without a report, drawn as dashed rings"
