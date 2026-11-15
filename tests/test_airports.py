"""The airport reference database: build, resolve, and a cross-checked distance.

`haversine_nm` is validated two independent ways: against an equirectangular
approximation (a different formula, valid at these latitudes) and against a
published airport-to-airport distance. A sign or unit error cannot survive both.
"""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from flightops_mcp.airports import AirportDB, build_from_csv, haversine_nm

from .conftest import FIXTURES


@pytest.fixture
def db(tmp_path: Path) -> AirportDB:
    path = tmp_path / "airports.sqlite3"
    rows = build_from_csv(FIXTURES / "airports_sample.csv", path)
    assert rows == 8, f"fixture should build 8 airports, built {rows}"
    return AirportDB(path)


def test_exact_icao_lookup(db: AirportDB) -> None:
    airport = db.get("ESGG")
    assert airport is not None
    assert "Landvetter" in airport["name"]
    assert airport["iata"] == "GOT"
    assert airport["municipality"] == "Göteborg", "UTF-8 must survive the CSV round trip"


def test_iata_lookup_resolves_to_the_same_airport(db: AirportDB) -> None:
    matches = db.lookup("GOT")
    assert matches[0]["ident"] == "ESGG"


def test_lookup_is_case_insensitive(db: AirportDB) -> None:
    assert db.lookup("esgg")[0]["ident"] == "ESGG"


def test_name_fragment_lookup(db: AirportDB) -> None:
    matches = db.lookup("Landvetter")
    assert any(row["ident"] == "ESGG" for row in matches)


def test_unknown_query_returns_empty_rather_than_a_near_miss(db: AirportDB) -> None:
    assert db.lookup("ZZZZZZ") == []


def test_nearest_returns_the_airport_you_are_standing_on(db: AirportDB) -> None:
    esgg = db.get("ESGG")
    assert esgg is not None
    nearby = db.nearest(esgg["latitude"], esgg["longitude"], limit=3)
    assert nearby[0]["ident"] == "ESGG"
    assert nearby[0]["distance_nm"] < 0.1


def test_published_coordinates_are_not_scrambled(db: AirportDB) -> None:
    """Landvetter is at roughly 57.66 N, 12.28 E. A lat/lon swap would fail here."""
    esgg = db.get("ESGG")
    assert esgg is not None
    assert abs(esgg["latitude"] - 57.66) < 0.05
    assert abs(esgg["longitude"] - 12.28) < 0.05


def test_haversine_matches_an_independent_approximation() -> None:
    # Gothenburg Landvetter -> Stockholm Arlanda is published as ~212 nm.
    got_lat, got_lon = 57.6628, 12.2798
    arn_lat, arn_lon = 59.6519, 17.9186
    distance = haversine_nm(got_lat, got_lon, arn_lat, arn_lon)
    assert 205 <= distance <= 220, distance

    # Equirectangular approximation: a different formula, same answer at this scale.
    mean_lat = math.radians((got_lat + arn_lat) / 2)
    dx = (arn_lon - got_lon) * math.cos(mean_lat)
    dy = arn_lat - got_lat
    approx = math.hypot(dx, dy) * 60.0
    assert abs(approx - distance) / distance < 0.01, (approx, distance)


def test_haversine_is_zero_and_symmetric() -> None:
    assert haversine_nm(57.66, 12.28, 57.66, 12.28) == pytest.approx(0.0)
    forward = haversine_nm(57.66, 12.28, 59.65, 17.92)
    backward = haversine_nm(59.65, 17.92, 57.66, 12.28)
    assert forward == pytest.approx(backward)
