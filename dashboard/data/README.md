# `airlines.csv`: ICAO airline designators

The board derives an aircraft's airline from its callsign. A callsign such as `NOZ1260` starts with
a three-letter ICAO operator designator (`NOZ`), and this table names it. **The airline is derived, never
reported**: OpenSky sends a callsign and nothing about the operator, so the inspector says
"from the callsign", and a designator the table does not hold is shown as its three letters with no name.
A callsign that is not a designator followed by a flight number, such as the US registration `N12345`,
gets no airline at all.

| Column | Meaning |
|---|---|
| `designator` | The three-letter ICAO designator, upper case. The file is sorted by it. |
| `name` | The operator's common name. |
| `country` | Where the operator is certificated, as an ISO 3166-1 alpha-2 code. |

## Source

Compiled by hand for this repository on 2026-11-19. Designators are those ICAO assigns (ICAO Doc 8585,
*Designators for Aircraft Operating Agencies*). An entry went in only if it was certain. Military callers
(`RCH`), designators whose holder was not certain, and private operators known by code alone were left out on
purpose, so they display bare instead of with a guessed name.

Which designators to include was chosen from what is flying. Two live `airspace_snapshot` calls on 2026-11-19
around 23:35 UTC, over Europe and North America, gave 6,379 airline-shaped callsigns under 551 designators. The
163 here are the most frequent ones that met the bar above, plus a few large carriers the late-evening sky
did not show. They named 81.9 % of those callsigns (`docs/DASHBOARD.md`, "Airlines, derived").

## Licence

The table is a list of facts (code, operator and country) assembled for this repository. It is released under
the repository's own licence (`LICENSE`). No third-party database was copied. OpenFlights' `airlines.dat`
(ODbL 1.0) was considered and not used, so no share-alike term applies.

## To add a designator

1. Confirm the designator and the operator from a primary source: the operator's own site, the CAA that
   certificates it, or ICAO Doc 8585. A spotting site is not a primary source.
2. Add one row, `DESIGNATOR,Name,CC`, in sorted position. Quote a name that contains a comma.
3. Run `uv run pytest tests/test_aircraft_layer.py`. It checks the header, the sort order, that each designator
   is three capital letters, that none repeats, and that every row has a name and a two-letter country.

Nothing else needs to change: the table is read once when the board first parses a callsign.
