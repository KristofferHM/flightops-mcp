"""Which aircraft the board draws, in what order, and what it says about each one.

Clutter is a ranking problem, so the sort is the primary control and the filters are
secondary. Every decision is here, where a test can hold it:

* The cap is applied after filtering and sorting, and ties break by `icao24`, so the
  same snapshot and the same choices always draw the same aircraft. What the cap drops
  is counted and said: `showing 200 of 412 — raise the cap or narrow the filter`.
* A missing altitude or speed is unknown. It sorts after every known value and never
  as 0, and it falls in no altitude band.
* An aircraft with no position is not drawn, and nothing is interpolated.
* The airline is derived from the callsign's ICAO designator, through a bundled table,
  and labelled as derived. A prefix the table does not hold is shown bare.
* `transit` (observed), `estimated arrival` (inferred) and `destination` (no source)
  are three different kinds of claim, and are worded as three lines.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final, Literal

from flightops_mcp.airports import haversine_nm
from flightops_mcp.opensky import AIRCRAFT_FLIGHTS_COST

from . import board
from .operator import airline_table

Payload = dict[str, Any]
Sort = Literal["distance", "altitude", "callsign", "last_heard", "speed"]
Band = Literal["low", "mid", "high"]
Phase = Literal["airborne", "ground"]

# An ICAO flight identification: a three-letter operator designator, then a flight
# number that starts with a digit. `RYR` alone is the designator with no number.
CALLSIGN: Final = re.compile(r"([A-Z]{3})(\d[A-Z0-9]{0,3})?")
DEFAULT_CAP: Final = 200
CAPS: Final = (50, 100, 200, 500, 1000)
RADII_NM: Final = (25, 50, 100, 250, 500)
SORTS: Final[Mapping[str, str]] = {
    "distance": "distance from the airport, nearest first",
    "altitude": "altitude, lowest first",
    "callsign": "callsign, A to Z",
    "last_heard": "last heard, most recent first",
    "speed": "speed, fastest first",
}
SORT_SHORT: Final[Mapping[str, str]] = {
    "distance": "distance",
    "altitude": "altitude",
    "callsign": "callsign",
    "last_heard": "last heard",
    "speed": "speed",
}
# Barometric altitude bands, in metres: below 3 km, 3 to 9 km, above 9 km.
BANDS: Final[Mapping[str, tuple[float | None, float | None]]] = {
    "low": (None, 3000.0),
    "mid": (3000.0, 9000.0),
    "high": (9000.0, None),
}
BAND_TEXT: Final = {"low": "below 3 km", "mid": "3–9 km", "high": "above 9 km"}
# Follow stops when the focused aircraft has not been heard for this long: by then it
# is drawn at the faintest, and the board no longer shows where it is.
FOCUS_STALE_S: Final = board.FADE_AFTER_S
NO_AIRPORT_SORT: Final = "No airport is selected, so the aircraft are sorted by last heard."
# What stays on screen: the selection line already says "sorted by last heard".
NO_AIRPORT_SORT_SHORT: Final = "No airport is selected."
# Of the airline choices offered, the most common in the snapshot.
AIRLINE_CHOICES: Final = 60
# A transit is labelled with its first and last times and about this many between.
TRANSIT_LABELS: Final = 6
M_TO_FT: Final = 3.28084
MS_TO_KT: Final = 1.943844
NM_PER_KM: Final = 1 / 1.852


# --- the airline, derived from the callsign ------------------------------------------
def designator(callsign: object) -> str | None:
    """The ICAO airline designator a callsign starts with, or None if it is not one.

    `NOZ1260` is NOZ. `N12345` is a US registration, not an airline, and is None.
    """
    if not isinstance(callsign, str):
        return None
    match = CALLSIGN.fullmatch(callsign.strip().upper())
    return match.group(1) if match else None


def airline(callsign: object) -> Payload | None:
    """The airline a callsign names, derived from the bundled table, or None.

    A designator the table does not hold keeps its prefix and gets no name: a guess at
    an operator is worse than its code.
    """
    code = designator(callsign)
    if code is None:
        return None
    found = airline_table().get(code)
    return {
        "designator": code,
        "name": found[0] if found else None,
        "country": found[1] if found else None,
        "derived_from": "callsign",
    }


# --- the selection --------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Selection:
    """What the reader chose. None or "" means no filter on that axis."""

    airport: Payload | None = None
    radius_nm: float | None = None
    airline: str | None = None
    band: Band | None = None
    phase: Phase | None = None
    query: str = ""
    sort: Sort = "distance"
    cap: int = DEFAULT_CAP
    focus: str | None = None


def bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dlon = math.radians(lon2 - lon1)
    x = math.sin(dlon) * math.cos(p2)
    y = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dlon)
    return (math.degrees(math.atan2(x, y)) + 360.0) % 360.0


def _placed(entry: Payload) -> bool:
    return isinstance(entry.get("latitude"), int | float) and isinstance(
        entry.get("longitude"), int | float
    )


def _in_band(altitude: float | None, band: str) -> bool:
    if altitude is None:
        return False
    low, high = BANDS[band]
    return (low is None or altitude >= low) and (high is None or altitude < high)


def _sort_key(sort: str) -> Any:
    """Known values first, in the sort's direction; unknowns after; ties by icao24."""

    def key(entry: Payload) -> tuple[bool, Any, str]:
        icao24 = str(entry.get("icao24") or "")
        if sort == "distance":
            value = entry.get("distance_nm")
        elif sort == "altitude":
            value = entry.get("altitude_m")
        elif sort == "callsign":
            value = (entry.get("callsign") or "").strip().upper() or None
        elif sort == "last_heard":
            value = entry.get("age_s")
        else:
            speed = entry.get("velocity_ms")
            value = -speed if speed is not None else None
        return (value is None, value if value is not None else 0, icao24)

    return key


def _row(entry: Payload, origin: Payload | None) -> Payload:
    code = designator(entry.get("callsign"))
    row = {**entry, "airline": code, "distance_nm": None, "bearing_deg": None}
    if origin is not None:
        lat, lon = origin["latitude"], origin["longitude"]
        row["distance_nm"] = round(haversine_nm(lat, lon, entry["latitude"], entry["longitude"]), 1)
        row["bearing_deg"] = round(bearing_deg(lat, lon, entry["latitude"], entry["longitude"]))
    return row


def airline_choices(rows: Sequence[Payload]) -> list[Payload]:
    """The airlines in the snapshot, most aircraft first, for the filter's list."""
    counted = Counter(row["airline"] for row in rows if row["airline"])
    table = airline_table()
    ranked = sorted(counted.items(), key=lambda item: (-item[1], item[0]))[:AIRLINE_CHOICES]
    return [
        {"designator": code, "name": table.get(code, (None, None))[0], "count": count}
        for code, count in ranked
    ]


def select(aircraft: Sequence[Payload], choice: Selection, now: int | None) -> Payload:
    """Filter, sort and cap a snapshot's aircraft, and say which control did what.

    `aircraft` are `board._aircraft` entries. `now` is OpenSky's clock for the snapshot,
    used only for the focused aircraft's verdict.
    """
    origin = choice.airport
    placed = [_row(entry, origin) for entry in aircraft if _placed(entry)]
    unplaced = len(aircraft) - len(placed)
    pool = placed
    if origin is not None and choice.radius_nm is not None:
        pool = [row for row in pool if row["distance_nm"] <= choice.radius_nm]
    in_radius = len(pool)
    choices = airline_choices(pool)
    filtered = pool
    if choice.airline:
        filtered = [row for row in filtered if row["airline"] == choice.airline]
    if choice.band:
        filtered = [row for row in filtered if _in_band(row["altitude_m"], choice.band)]
    if choice.phase:
        on_ground = choice.phase == "ground"
        filtered = [row for row in filtered if bool(row.get("on_ground")) is on_ground]
    needle = choice.query.strip().casefold()
    if needle:
        filtered = [
            row
            for row in filtered
            if needle in str(row.get("callsign") or "").casefold()
            or needle in str(row.get("icao24") or "").casefold()
        ]
    sort: str = choice.sort
    fallback = sort == "distance" and origin is None
    if fallback:
        # Distance needs an airport. Without one the order is still deterministic.
        sort = "last_heard"
    ranked = sorted(filtered, key=_sort_key(sort))
    cap = max(1, choice.cap)
    shown = ranked[:cap]
    focus = focus_view(placed, choice.focus, origin, now) if choice.focus else None
    return {
        "heard": len(placed),
        "no_position": unplaced,
        "in_radius": in_radius if choice.radius_nm is not None and origin is not None else None,
        "filtered": len(filtered),
        "shown": len(shown),
        "sort": sort,
        "sort_text": SORTS[sort],
        "cap": cap,
        "text": selection_text(
            choice, len(placed), in_radius, len(filtered), len(shown), sort=sort
        ),
        "dropped": dropped_text(len(shown), len(filtered)),
        "dropped_short": dropped_short(len(shown), len(filtered)),
        "fallback": NO_AIRPORT_SORT if fallback else None,
        "fallback_short": NO_AIRPORT_SORT_SHORT if fallback else None,
        "airlines": choices,
        "aircraft": shown,
        "focus": focus,
    }


def _filtered_by(choice: Selection) -> bool:
    return bool(choice.airline or choice.band or choice.phase or choice.query.strip())


def selection_text(
    choice: Selection, heard: int, in_radius: int, filtered: int, shown: int, *, sort: str
) -> str:
    """`1,183 in radius · filtered to 412 · sorted by distance · showing 200`."""
    origin = choice.airport
    if origin is not None and choice.radius_nm is not None:
        parts = [f"{in_radius:,} within {choice.radius_nm:g} nm of {origin['icao']}"]
    else:
        parts = [f"{heard:,} heard"]
    if _filtered_by(choice):
        parts.append(f"filtered to {filtered:,}")
    parts.append(f"sorted by {SORT_SHORT[sort]}")
    parts.append(f"showing {shown:,}")
    return " · ".join(parts)


def dropped_text(shown: int, filtered: int) -> str | None:
    """What the cap left out, or None when it left out nothing."""
    if shown >= filtered:
        return None
    return f"showing {shown:,} of {filtered:,} — raise the cap or narrow the filter"


def dropped_short(shown: int, filtered: int) -> str | None:
    """The cap's count without the advice."""
    if shown >= filtered:
        return None
    return f"showing {shown:,} of {filtered:,}"


# --- focus, follow and the inspector ---------------------------------------------------
def focus_view(
    rows: Sequence[Payload], icao24: str | None, origin: Payload | None, now: int | None
) -> Payload:
    """The focused aircraft, drawn whatever the filters and the cap say, and its verdict.

    `reported` keeps a follower centred on it. `stale` and `not_reported` stop the
    follower, and `reason` says which happened. A view setting never changes what is
    fetched: this reads the snapshot the layer already holds.
    """
    found = next((row for row in rows if row.get("icao24") == icao24), None)
    if found is None:
        return _focus(icao24, "not_reported", f"{icao24} is not in the newest snapshot.", None, [])
    age = found.get("age_s")
    name = (found.get("callsign") or "").strip() or str(icao24)
    rows_text = aircraft_rows(found, origin)
    if age is None:
        return _focus(icao24, "stale", f"When {name} was last heard is unknown.", found, rows_text)
    if age > FOCUS_STALE_S:
        reason = f"{name} was last heard {_ago(age)} ago."
        return _focus(icao24, "stale", reason, found, rows_text)
    if now is None:
        reason = f"OpenSky sent no clock, so how recent {name} is is unknown."
        return _focus(icao24, "stale", reason, found, rows_text)
    return _focus(icao24, "reported", None, found, rows_text)


def _focus(
    icao24: str | None,
    state: str,
    reason: str | None,
    found: Payload | None,
    rows: list[Payload],
) -> Payload:
    return {
        "icao24": icao24,
        "state": state,
        "reason": reason,
        # The line a follower shows when it lets go: which of the two things happened.
        "follow_stopped": f"Follow stopped: {reason}" if reason else None,
        "aircraft": found,
        "rows": rows,
    }


def _ago(seconds: float) -> str:
    return f"{round(seconds)} s" if seconds < 120 else board.age_text(seconds)


def _airline_text(callsign: object) -> str:
    derived = airline(callsign)
    if derived is None:
        return "none: the callsign is not an airline designator"
    if derived["name"] is None:
        return f"{derived['designator']}: not in the bundled table"
    return f"{derived['name']} ({derived['designator']}, {derived['country']}), from the callsign"


def _class_text(entry: Payload) -> str:
    """`general aviation (callsign SE-MOY)`. The evidence is the point: it can be argued with."""
    found = entry.get("operator_class")
    if found is None:
        return "not classified"
    return f"{found} ({entry.get('class_evidence') or 'nothing matched'})"


def _row_text(label: str, text: str, mono: bool = False) -> Payload:
    return {"label": label, "text": text, "mono": mono}


def aircraft_rows(entry: Payload, origin: Payload | None) -> list[Payload]:
    """The inspector's facts, each worded here. An unreported figure says so."""
    altitude = entry.get("altitude_m")
    speed = entry.get("velocity_ms")
    track = entry.get("track")
    age = entry.get("age_s")
    if entry.get("on_ground"):
        altitude_text = "on the ground"
    elif altitude is None:
        altitude_text = "not reported"
    else:
        altitude_text = f"{altitude:,.0f} m ({altitude * M_TO_FT:,.0f} ft), barometric"
    rows = [
        _row_text("Callsign", (entry.get("callsign") or "").strip() or "not sent", mono=True),
        _row_text("Airline", _airline_text(entry.get("callsign"))),
        _row_text("Class", _class_text(entry)),
        _row_text("ICAO24", str(entry.get("icao24") or ""), mono=True),
        _row_text("Altitude", altitude_text),
        _row_text(
            "Speed",
            "not reported" if speed is None else f"{speed:,.0f} m/s ({speed * MS_TO_KT:,.0f} kt)",
        ),
        _row_text("Heading", "not reported" if track is None else f"{round(track) % 360:03d}°"),
        _row_text("On the ground", "yes" if entry.get("on_ground") else "no"),
        _row_text(
            "Last heard",
            "unknown" if age is None else f"{_ago(max(0, age))} ago, by OpenSky's clock",
        ),
    ]
    if origin is None or entry.get("distance_nm") is None:
        rows.append(_row_text("From airport", "no airport selected"))
    else:
        rows.append(
            _row_text(
                f"From {origin['icao']}",
                f"{entry['distance_nm']:,.1f} nm, bearing {entry['bearing_deg']:03d}°",
            )
        )
    return rows


# --- transit, estimated arrival, destination ------------------------------------------
def _utc(epoch: object, seconds: bool = False) -> str | None:
    if isinstance(epoch, bool) or not isinstance(epoch, int) or epoch <= 0:
        return None
    return datetime.fromtimestamp(epoch, UTC).strftime("%H:%M:%SZ" if seconds else "%H:%MZ")


def _day(epoch: object) -> str:
    if isinstance(epoch, bool) or not isinstance(epoch, int) or epoch <= 0:
        return "time unknown"
    return datetime.fromtimestamp(epoch, UTC).strftime("%Y-%m-%d %H:%M UTC")


def transit_labels(count: int) -> list[int]:
    """Which waypoints carry a time label: the first, the last, and evenly between."""
    if count <= 0:
        return []
    step = max(1, math.ceil((count - 1) / max(1, TRANSIT_LABELS - 1)))
    picked = list(range(0, count, step))
    if picked[-1] != count - 1:
        picked.append(count - 1)
    return picked


def _flight_text(flight: Payload | None) -> str:
    if not flight:
        return ""
    route = f"{flight.get('departure_airport') or '?'} → {flight.get('arrival_airport') or '?'}"
    return f"{route}, ended {_day(flight.get('last_seen_epoch'))}"


def arrival_text(arrival: Payload) -> str:
    """The estimated-arrival line. It always says what kind of claim it is."""
    state = arrival.get("state")
    basis = "estimated from ADS-B, not a schedule"
    if state == "estimated":
        return f"{arrival.get('airport')} — {basis}"
    if state == "not_requested":
        return f"not requested — asking OpenSky costs {AIRCRAFT_FLIGHTS_COST} credits"
    if state == "unavailable":
        return f"unavailable ({arrival.get('error', 'unknown')})"
    if state == "not_estimated":
        return "OpenSky lists this flight but estimated no arrival airport for it"
    previous = arrival.get("previous_flight")
    text = "no record for this flight yet: OpenSky lists a flight once it has ended"
    if previous:
        text += f". Last listed: {_flight_text(previous)} ({basis})"
    return text


def arrival_short(arrival: Payload) -> str:
    """The estimated-arrival value without its basis or its price. The label says "Estimated"."""
    state = arrival.get("state")
    if state == "estimated":
        return str(arrival.get("airport"))
    if state == "not_requested":
        return "not requested"
    # A failure's code and OpenSky's "no estimate" are the answer, not a reason: they stay whole.
    if state in ("unavailable", "not_estimated"):
        return arrival_text(arrival)
    previous = arrival.get("previous_flight")
    # The last listed flight is a value, so it stays; why there is no record yet moves.
    return "no record for this flight yet" + (
        f". Last listed: {_flight_text(previous)}" if previous else ""
    )


def destination_short(text: str) -> str:
    """`No source`: the verdict is the first sentence; the transponder's reason moves."""
    first, _, _ = text.partition(".")
    return first or text


def _claim(label: str, text: str, short: str) -> Payload:
    """An inspector claim: `short` on screen, `text` behind the section's `?`."""
    return {**_row_text(label, text), "short": short}


def track_view(payload: Payload) -> Payload:
    """`aircraft_track` as the inspector and the map show it."""
    if not payload.get("ok"):
        error = board._error(payload)  # noqa: SLF001 - one rule for reading a failure
        code = error.get("code", "unknown")
        return {
            "state": "unknown",
            "waypoints": [],
            "labels": [],
            "lines": [
                _claim("Transit", f"unavailable ({code})", f"unavailable ({code})"),
                _claim("Estimated arrival", "not known: the track could not be read", "not known"),
                _claim("Destination", "No source.", "No source"),
            ],
            "unknowns": [str(error.get("message", "The track could not be read."))],
            "credits_spent": 0,
        }
    transit = payload.get("transit") or {}
    columns = transit.get("columns") or []
    points = [dict(zip(columns, row, strict=True)) for row in transit.get("waypoints") or []]
    waypoints = [
        {
            "time": point["time"],
            "time_utc": _utc(point["time"]),
            "latitude": point["latitude"],
            "longitude": point["longitude"],
            "altitude_m": point.get("baro_altitude_m"),
        }
        for point in points
    ]
    if waypoints:
        first, last = transit.get("first_waypoint_epoch"), transit.get("last_waypoint_epoch")
        span = (last - first) if isinstance(first, int) and isinstance(last, int) else None
        length = f" ({board.age_text(span)})" if span is not None else ""
        transit_short = f"observed: {len(waypoints)} waypoints, {_utc(first)}–{_utc(last)}{length}"
        transit_line = f"{transit_short}, from OpenSky /tracks; nothing drawn past the last"
    else:
        transit_short = "none"
        transit_line = "none: OpenSky holds no track for this aircraft now"
    destination = (payload.get("destination") or {}).get("text", "No source.")
    arrival = payload.get("estimated_arrival") or {}
    return {
        "state": "observed" if waypoints else "none",
        "waypoints": waypoints,
        "labels": transit_labels(len(waypoints)),
        "lines": [
            _claim("Transit", transit_line, transit_short),
            _claim("Estimated arrival", arrival_text(arrival), arrival_short(arrival)),
            _claim("Destination", destination, destination_short(destination)),
        ],
        "estimate_requested": (payload.get("estimated_arrival") or {}).get("state")
        != "not_requested",
        "unknowns": list(payload.get("unknowns") or []),
        "credits_spent": (payload.get("bookkeeping") or {}).get("credits_spent", 0),
    }
