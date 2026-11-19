"""Local airport reference data, built from the public-domain OurAirports CSV.

Why a local SQLite file rather than an API: airport identities are stable, so
resolving "ESGG" should not cost a network round trip, an API credit, or a
dependency on an upstream being up. It also makes the test suite hermetic.

Build it with:

    uv run python scripts/build_airports_db.py --csv data/airports_world.csv   # committed
    uv run python scripts/build_airports_db.py            # downloads the whole CSV
    uv run python scripts/build_airports_db.py --csv X    # or use a local copy

`data/airports_world.csv` is the committed world set: every large and medium airport
with scheduled service. `data/airports_seed.csv` is a 25-airport fixture for tests.
"""

from __future__ import annotations

import csv
import math
import sqlite3
from collections.abc import Iterable, Iterator, Mapping
from contextlib import closing
from pathlib import Path
from typing import Any, Final

_SCHEMA = """
CREATE TABLE IF NOT EXISTS airports (
    ident TEXT PRIMARY KEY,
    iata TEXT,
    name TEXT NOT NULL,
    type TEXT,
    latitude REAL,
    longitude REAL,
    elevation_ft INTEGER,
    iso_country TEXT,
    iso_region TEXT,
    municipality TEXT,
    scheduled_service TEXT,
    continent TEXT
);
CREATE INDEX IF NOT EXISTS idx_airports_iata ON airports(iata);
CREATE INDEX IF NOT EXISTS idx_airports_continent ON airports(continent);
CREATE INDEX IF NOT EXISTS idx_airports_name ON airports(name);
"""

_COLUMNS = (
    "ident",
    "iata",
    "name",
    "type",
    "latitude",
    "longitude",
    "elevation_ft",
    "iso_country",
    "iso_region",
    "municipality",
    "scheduled_service",
    "continent",
)

# Only airports a crew-scheduling system would actually know about.
USEFUL_TYPES: Final = frozenset(
    {"large_airport", "medium_airport", "small_airport", "seaplane_base"}
)
# The world set the board draws: both kinds kept, so a reader can tell them apart.
WORLD_TYPES: Final = frozenset({"large_airport", "medium_airport"})

# OurAirports' continent codes. Antarctica has no scheduled large or medium airport.
CONTINENTS: Final[Mapping[str, str]] = {
    "EU": "Europe",
    "NA": "North America",
    "SA": "South America",
    "AF": "Africa",
    "AS": "Asia",
    "OC": "Oceania",
}


def _without_comment_header(lines: Iterable[str]) -> Iterator[str]:
    """Drop the `#` lines a committed export starts with, and nothing after them.

    Only the leading block: a quoted field further down may begin a line with `#`.
    """
    header = True
    for line in lines:
        if header and line.startswith("#"):
            continue
        header = False
        yield line


def build_from_csv(
    csv_path: Path,
    db_path: Path,
    *,
    limit: int | None = None,
    types: frozenset[str] = USEFUL_TYPES,
    scheduled_only: bool = False,
) -> int:
    """Load OurAirports' airports.csv into SQLite. Returns the number of airports stored.

    A row without an ident cannot be looked up and is skipped. When an ident
    appears twice the later row wins, and the return value counts airports in
    the database, not rows read: the two differ exactly when the CSV repeats
    itself. `limit` stops after that many accepted rows. `types` and
    `scheduled_only` filter rows before they count towards it.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        db_path.unlink()

    accepted = 0
    # `with connect()` commits but does not close; on Windows the open handle then
    # blocks the next rebuild from deleting the file.
    with closing(sqlite3.connect(db_path)) as connection:
        connection.executescript(_SCHEMA)
        with csv_path.open(newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(_without_comment_header(handle))
            batch: list[tuple[Any, ...]] = []
            for record in reader:
                if limit is not None and accepted >= limit:
                    break
                if record.get("type") not in types:
                    continue
                if scheduled_only and record.get("scheduled_service") != "yes":
                    continue
                ident = (record.get("ident") or "").strip().upper()
                if not ident:
                    continue
                batch.append(
                    (
                        ident,
                        (record.get("iata_code") or "").strip().upper() or None,
                        record.get("name") or "",
                        record.get("type"),
                        _to_float(record.get("latitude_deg")),
                        _to_float(record.get("longitude_deg")),
                        _to_int(record.get("elevation_ft")),
                        record.get("iso_country"),
                        record.get("iso_region"),
                        record.get("municipality"),
                        record.get("scheduled_service"),
                        (record.get("continent") or "").strip().upper() or None,
                    )
                )
                accepted += 1
                if len(batch) >= 5000:
                    _insert(connection, batch)
                    batch.clear()
            if batch:
                _insert(connection, batch)
        connection.commit()
        (stored,) = connection.execute("SELECT count(*) FROM airports").fetchone()
    return int(stored)


def _insert(connection: sqlite3.Connection, batch: Iterable[tuple[Any, ...]]) -> None:
    placeholders = ", ".join("?" for _ in _COLUMNS)
    # The column list is the frozen module constant `_COLUMNS`; the only values
    # interpolated into SQL are "?" placeholders. Nothing here is caller-supplied.
    connection.executemany(
        f"INSERT OR REPLACE INTO airports ({', '.join(_COLUMNS)}) VALUES ({placeholders})",  # noqa: S608
        batch,
    )


def _to_float(value: str | None) -> float | None:
    """A finite number, or None. `float()` also reads "nan" and "inf", which are not places."""
    try:
        number = float(value) if value not in (None, "") else None
    except ValueError:
        return None
    return number if number is not None and math.isfinite(number) else None


def _to_int(value: str | None) -> int | None:
    number = _to_float(value)
    return int(number) if number is not None else None


class AirportDB:
    """Read-only lookups over the reference database."""

    def __init__(self, path: Path) -> None:
        self.path = path

    @property
    def exists(self) -> bool:
        return self.path.is_file()

    def healthy(self) -> bool:
        """Readiness probe: the file opens, the table queries and holds rows.

        An empty database is not ready: every tool that needs coordinates would
        fail, so reporting ready would send traffic to an instance that cannot
        serve it.
        """
        if not self.path.is_file():
            return False
        try:
            with closing(self._connect()) as connection:
                row = connection.execute("SELECT count(*) FROM airports").fetchone()
        except sqlite3.Error:
            return False
        return bool(row) and int(row[0]) > 0

    def has_continents(self) -> bool:
        """Whether the file was built with the continent column the board filters on."""
        with closing(self._connect()) as connection:
            columns = {row[1] for row in connection.execute("PRAGMA table_info(airports)")}
        return "continent" in columns

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(f"file:{self.path.as_posix()}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        return connection

    def get(self, ident: str) -> dict[str, Any] | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM airports WHERE ident = ?", (ident.upper(),)
            ).fetchone()
        return dict(row) if row else None

    def lookup(self, query: str, *, limit: int = 5) -> list[dict[str, Any]]:
        """Resolve an ICAO/IATA code or a name fragment.

        Exact identifier matches are returned before fuzzy name matches, because
        "ESGG" should never compete with "Siggjo" for the model's attention.
        """
        needle = query.strip()
        if not needle:
            return []
        upper = needle.upper()
        with closing(self._connect()) as connection:
            exact = connection.execute(
                "SELECT * FROM airports WHERE ident = ? OR iata = ? ORDER BY ident LIMIT ?",
                (upper, upper, limit),
            ).fetchall()
            if exact:
                return [dict(row) for row in exact]
            fuzzy = connection.execute(
                "SELECT * FROM airports WHERE name LIKE ? ORDER BY type, name LIMIT ?",
                (f"%{needle}%", limit),
            ).fetchall()
        return [dict(row) for row in fuzzy]

    def board(self, *, limit: int = 30) -> list[dict[str, Any]]:
        """Airports that can be placed on a map, the largest kind first."""
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT * FROM airports WHERE latitude IS NOT NULL AND longitude IS NOT NULL "
                "ORDER BY type != 'large_airport', ident LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def placed(self) -> list[dict[str, Any]]:
        """Every airport with coordinates, large before medium, then by ident."""
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT * FROM airports WHERE latitude IS NOT NULL AND longitude IS NOT NULL "
                "ORDER BY type != 'large_airport', ident"
            ).fetchall()
        return [dict(row) for row in rows]

    def nearest(
        self, lat: float, lon: float, *, limit: int = 5, max_nm: float = 200.0
    ) -> list[dict[str, Any]]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT * FROM airports WHERE latitude IS NOT NULL AND longitude IS NOT NULL"
            ).fetchall()
        scored: list[tuple[float, dict[str, Any]]] = []
        for row in rows:
            record = dict(row)
            distance = haversine_nm(lat, lon, record["latitude"], record["longitude"])
            if distance <= max_nm:
                record["distance_nm"] = round(distance, 1)
                scored.append((distance, record))
        scored.sort(key=lambda pair: pair[0])
        return [record for _, record in scored[:limit]]


def haversine_nm(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in nautical miles."""
    earth_radius_nm = 3440.065
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = phi2 - phi1
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * earth_radius_nm * math.asin(min(1.0, math.sqrt(a)))
