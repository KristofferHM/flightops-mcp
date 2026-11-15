#!/usr/bin/env python
"""Build the local airport reference database from the OurAirports open data.

    uv run python scripts/build_airports_db.py                    # download + build
    uv run python scripts/build_airports_db.py --csv data/airports_seed.csv
    uv run python scripts/build_airports_db.py --db data/test.sqlite3 --limit 2000

Source: https://davidmegginson.github.io/ourairports-data/airports.csv
Licence: public domain. The snapshot is refreshed nightly upstream; we pin a
copy only in CI-friendly fixtures, never a multi-megabyte blob in git.
"""

from __future__ import annotations

import argparse
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from flightops_mcp.airports import build_from_csv  # noqa: E402

CSV_URL = "https://davidmegginson.github.io/ourairports-data/airports.csv"


def download(destination: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    print(f"downloading {CSV_URL}")
    with urllib.request.urlopen(CSV_URL, timeout=120) as response:  # noqa: S310
        payload = response.read()
    destination.write_bytes(payload)
    print(f"  {len(payload):,} bytes -> {destination}")
    return destination


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
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
    args = parser.parse_args()

    csv_path = args.csv
    if csv_path is None:
        if args.cache.is_file() and args.cache.stat().st_size > 1_000_000:
            csv_path = args.cache
            print(f"using cached CSV {csv_path}")
        else:
            csv_path = download(args.cache)

    rows = build_from_csv(csv_path, args.db, limit=args.limit)
    size = args.db.stat().st_size
    print(f"built {args.db} with {rows:,} airports ({size / 1e6:.1f} MB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
