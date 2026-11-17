"""Properties of the airport loader: whatever the CSV looks like, the database agrees.

Records are generated, written with the `csv` module the way OurAirports writes
them (quoted when a field needs it), and loaded. The database must then hold
one airport per distinct ident among the rows the loader accepts, the later
row winning, with every field round-tripped, whatever commas, quotes, line
breaks, blank lines or repeated idents the file contained.
"""

from __future__ import annotations

import csv
import io
import sqlite3
import tempfile
from pathlib import Path

from hypothesis import given, settings
from hypothesis import strategies as st

from flightops_mcp.airports import build_from_csv

HEADER = [
    "id",
    "ident",
    "type",
    "name",
    "latitude_deg",
    "longitude_deg",
    "elevation_ft",
    "iso_country",
    "iso_region",
    "municipality",
    "scheduled_service",
    "iata_code",
]
USEFUL = ["large_airport", "medium_airport", "small_airport", "seaplane_base"]
OTHER = ["heliport", "closed", "balloonport", ""]

# A few idents in both cases, so repeats (and repeats that differ only in case) are common.
idents = st.sampled_from(["ESGG", "esgg", "ESSA", "EKCH", "ENGM", " ENGM ", "", "  "])
# Names a careless CSV writer would break: commas, quotes, line breaks.
names = st.text(
    st.characters(blacklist_categories=("Cs",), blacklist_characters="\x00"), max_size=20
) | st.sampled_from(['Göteborg, "Landvetter"', "a,b,c", 'say "hi"', "line\nbreak", "cr\r\nlf"])
coordinates = st.one_of(
    st.floats(-90, 90, allow_nan=False).map(repr),
    st.sampled_from(["", "nan", "inf", "-inf", "north", "1e400"]),
)


@st.composite
def records(draw: st.DrawFn) -> dict[str, str]:
    return {
        "id": str(draw(st.integers(1, 10**6))),
        "ident": draw(idents),
        "type": draw(st.sampled_from(USEFUL + OTHER)),
        "name": draw(names),
        "latitude_deg": draw(coordinates),
        "longitude_deg": draw(coordinates),
        "elevation_ft": draw(coordinates),
        "iso_country": draw(st.sampled_from(["SE", "DK", "NO", ""])),
        "iso_region": "",
        "municipality": draw(names),
        "scheduled_service": draw(st.sampled_from(["yes", "no", ""])),
        "iata_code": draw(st.sampled_from(["GOT", "got", "ARN", "", " CPH "])),
    }


def write_csv(path: Path, rows: list[dict[str, str]], blank_after: set[int]) -> None:
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=HEADER, lineterminator="\n")
    writer.writeheader()
    if -1 in blank_after:
        buffer.write("\n")
    for index, row in enumerate(rows):
        writer.writerow(row)
        if index in blank_after:
            buffer.write("\n")
    path.write_text(buffer.getvalue(), encoding="utf-8", newline="")


def accepted(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    return [row for row in rows if row["type"] in USEFUL and row["ident"].strip()]


def load(
    rows: list[dict[str, str]], blank_after: set[int], limit: int | None = None
) -> tuple[int, dict[str, tuple[object, ...]]]:
    with tempfile.TemporaryDirectory() as directory:
        csv_path = Path(directory) / "airports.csv"
        db_path = Path(directory) / "airports.sqlite3"
        write_csv(csv_path, rows, blank_after)
        count = build_from_csv(csv_path, db_path, limit=limit)
        with sqlite3.connect(db_path) as connection:
            stored = {
                row[0]: row[1:]
                for row in connection.execute(
                    "SELECT ident, iata, name, latitude, longitude, elevation_ft FROM airports"
                )
            }
        connection.close()
    return count, stored


@given(rows=st.lists(records(), max_size=30), blank_after=st.sets(st.integers(0, 30)))
def test_one_airport_per_ident_and_the_count_says_so(
    rows: list[dict[str, str]], blank_after: set[int]
) -> None:
    count, stored = load(rows, blank_after)

    expected = {row["ident"].strip().upper(): row for row in accepted(rows)}
    assert count == len(stored) == len(expected)
    assert set(stored) == set(expected)
    for ident, row in expected.items():
        iata, name, latitude, longitude, elevation = stored[ident]
        # The later row wins, and quoting round-trips the name exactly.
        assert name == row["name"]
        assert iata == (row["iata_code"].strip().upper() or None)
        for value in (latitude, longitude, elevation):
            assert value is None or isinstance(value, int | float)
            assert value is None or abs(float(value)) < float("inf")


@given(
    rows=st.lists(records(), max_size=30),
    blank_after=st.sets(st.integers(0, 30)),
    limit=st.integers(0, 30),
)
def test_a_limit_stops_after_that_many_accepted_rows(
    rows: list[dict[str, str]], blank_after: set[int], limit: int
) -> None:
    """The limit used to be checked only every 5000 rows, so `--limit 20` loaded everything."""
    count, stored = load(rows, blank_after, limit=limit)

    first = accepted(rows)[:limit]
    assert count == len({row["ident"].strip().upper() for row in first})
    assert count <= limit
    assert set(stored) == {row["ident"].strip().upper() for row in first}


# Seven positions for a blank line: 128 inputs in all, so more examples only repeat them.
@settings(max_examples=50)
@given(blank_after=st.sets(st.integers(-1, 5)))
def test_blank_lines_and_a_header_alone_load_nothing_and_say_so(blank_after: set[int]) -> None:
    count, stored = load([], blank_after)
    assert (count, stored) == (0, {})
