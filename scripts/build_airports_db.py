#!/usr/bin/env python
"""Build the local airport reference database from the OurAirports open data.

    uv run python scripts/build_airports_db.py --csv data/airports_world.csv   # committed set
    uv run python scripts/build_airports_db.py                    # download + build everything
    uv run python scripts/build_airports_db.py --csv data/airports_seed.csv   # test fixture
    uv run python scripts/build_airports_db.py --db data/test.sqlite3 --limit 2000

Refresh the committed world set from upstream (build time only; nothing at runtime
downloads anything):

    uv run python scripts/build_airports_db.py --world --export data/airports_world.csv

`--world` keeps large and medium airports with scheduled service. `--export` writes
the filtered rows to a CSV whose `#` header names the source, the date of the
upstream file and its licence, then builds the database from that file.

Source: https://davidmegginson.github.io/ourairports-data/airports.csv
Licence: public domain (OurAirports).
"""

from __future__ import annotations

import argparse
import csv
import sys
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from flightops_mcp.airports import USEFUL_TYPES, WORLD_TYPES, build_from_csv  # noqa: E402

CSV_URL = "https://davidmegginson.github.io/ourairports-data/airports.csv"
# The columns the reference database reads. The rest of the upstream file stays upstream.
EXPORT_COLUMNS = (
    "ident",
    "type",
    "name",
    "latitude_deg",
    "longitude_deg",
    "elevation_ft",
    "continent",
    "iso_country",
    "iso_region",
    "municipality",
    "scheduled_service",
    "iata_code",
)


def download(destination: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    print(f"downloading {CSV_URL}")
    with urllib.request.urlopen(CSV_URL, timeout=120) as response:  # noqa: S310
        payload = response.read()
    destination.write_bytes(payload)
    print(f"  {len(payload):,} bytes -> {destination}")
    return destination


def export(
    source: Path, destination: Path, *, types: frozenset[str], scheduled_only: bool, dated: str
) -> int:
    """Write the filtered rows with a provenance header. Returns the rows written."""
    with source.open(newline="", encoding="utf-8") as handle:
        rows = [
            row
            for row in csv.DictReader(handle)
            if row.get("type") in types
            and (not scheduled_only or row.get("scheduled_service") == "yes")
            and (row.get("ident") or "").strip()
        ]
    rows.sort(key=lambda row: row["ident"])
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("w", newline="", encoding="utf-8") as handle:
        handle.write(f"# Source: OurAirports, {CSV_URL}\n")
        handle.write(f"# Upstream file dated {dated}\n")
        handle.write("# Licence: public domain (https://ourairports.com/data/)\n")
        filters = f"type in {', '.join(sorted(types))}"
        if scheduled_only:
            filters += "; scheduled_service == yes"
        handle.write(f"# Filter: {filters}; {len(rows)} rows\n")
        handle.write(
            "# Regenerate: uv run python scripts/build_airports_db.py --world "
            f"--export {destination.as_posix()}\n"
        )
        writer = csv.DictWriter(handle, fieldnames=EXPORT_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    return len(rows)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--csv", type=Path, default=None, help="use a local CSV instead of downloading"
    )
    parser.add_argument("--db", type=Path, default=Path("data/airports.sqlite3"))
    parser.add_argument(
        "--limit", type=int, default=None, help="stop after N airports (smoke tests)"
    )
    parser.add_argument(
        "--cache",
        type=Path,
        default=Path("data/airports.csv"),
        help="where a downloaded CSV is kept so a re-run does not re-download",
    )
    parser.add_argument(
        "--types",
        default=None,
        help="comma-separated OurAirports types to keep (default: large, medium, small, seaplane)",
    )
    parser.add_argument(
        "--scheduled-only", action="store_true", help="keep only scheduled_service == yes"
    )
    parser.add_argument(
        "--world",
        action="store_true",
        help="the committed world set: large and medium airports with scheduled service",
    )
    parser.add_argument(
        "--export", type=Path, default=None, help="write the filtered rows to this CSV first"
    )
    parser.add_argument(
        "--dated",
        default=None,
        help="the upstream file's date for the export header (default: its modification date)",
    )
    args = parser.parse_args()

    types = frozenset(t.strip() for t in args.types.split(",")) if args.types else USEFUL_TYPES
    scheduled_only = args.scheduled_only
    if args.world:
        types, scheduled_only = WORLD_TYPES, True

    csv_path = args.csv
    if csv_path is None:
        if args.cache.is_file() and args.cache.stat().st_size > 1_000_000:
            csv_path = args.cache
            print(f"using cached CSV {csv_path}")
        else:
            csv_path = download(args.cache)

    if args.export is not None:
        dated = args.dated or datetime.fromtimestamp(csv_path.stat().st_mtime, UTC).date()
        written = export(
            csv_path, args.export, types=types, scheduled_only=scheduled_only, dated=str(dated)
        )
        print(f"exported {written:,} airports -> {args.export} ({args.export.stat().st_size:,} B)")
        csv_path = args.export

    rows = build_from_csv(
        csv_path, args.db, limit=args.limit, types=types, scheduled_only=scheduled_only
    )
    size = args.db.stat().st_size
    print(f"built {args.db} with {rows:,} airports ({size / 1e6:.1f} MB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
