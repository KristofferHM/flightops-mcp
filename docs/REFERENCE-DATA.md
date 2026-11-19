# Airport reference data

Airport identities, names and positions come from a local SQLite file, never from an API at runtime. Resolving
`ESGG` costs no credit and no round trip, and works with no upstream up.

## The files

| File | Rows | What it is for |
|---|---:|---|
| `data/airports_world.csv` | 3,270 | The committed world set. The board and the Docker image build from it. |
| `data/airports_seed.csv` | 25 | The test fixture. The evaluation harness and the agent's local stack build from it. |
| `data/airports.csv` | ~86,000 | The full upstream file, downloaded on demand and never committed. |

`data/airports_world.csv` is 343 kB.

## Source, date and licence

- **Source:** OurAirports, `https://davidmegginson.github.io/ourairports-data/airports.csv`.
- **Upstream file:** the copy downloaded on 2026-11-15, with 86,363 rows. OurAirports refreshes the file nightly;
  the brief for this change quoted 86,375 rows on 2026-11-18. The filter gives the same 3,270 airports on both.
- **Licence:** public domain (`https://ourairports.com/data/`).

The same three facts are in the `#` header at the top of the CSV, which the loader skips.

## The filter

`type` is `large_airport` or `medium_airport`, and `scheduled_service` is `yes`. Both types are kept, and the board
labels each row Large or Medium, so a hub can be told apart from a regional field.

| Continent | Airports | Large | Medium |
|---|---:|---:|---:|
| Asia | 1,054 | 416 | 638 |
| North America | 851 | 189 | 662 |
| Europe | 508 | 253 | 255 |
| Africa | 342 | 164 | 178 |
| South America | 274 | 85 | 189 |
| Oceania | 241 | 47 | 194 |
| **World** | **3,270** | **1,154** | **2,116** |

## Refreshing it

Build time only. Nothing at runtime downloads anything.

```bash
uv run python scripts/build_airports_db.py --world --export data/airports_world.csv
uv run python scripts/build_airports_db.py --csv data/airports_world.csv           # rebuild the database
```

The first command downloads the upstream file to `data/airports.csv` (or reuses one over 1 MB), writes the filtered
rows with a fresh header, and builds the database from the result. `--dated YYYY-MM-DD` sets the header's date when
the file's modification date is not the upstream date. `--types` and `--scheduled-only` filter a build without
exporting.

## What it does not cover

- **Live traffic.** The reference data is worldwide; OpenSky's receiver coverage is not. It is dense over Europe and
  North America and sparse over Africa, South America and much of Asia, so an empty patch of sky means that no
  receiver heard anything, not that no aircraft are flying.
- **56 airports have an OurAirports ident that is not an ICAO code** (`5A8`, `AG-0001`, ...). NOAA has no METAR under
  those names, so the board shows them as Unknown. 45 of them have an ICAO code in OurAirports' `icao_code` column,
  which this database does not yet read.
- **Oceania's traffic box stops at 180° E**, because an OpenSky box cannot cross the antimeridian. Hawaii, Samoa and
  French Polynesia are listed, and outside the box.
- A database built before this change has no `continent` column. The board refuses it and prints the rebuild
  command, rather than overwriting a file that may be a deliberate full build.
