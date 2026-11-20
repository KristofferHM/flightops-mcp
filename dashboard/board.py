"""What the board shows, decided here and not in the browser.

The rules are about honesty, so they live where a test can hold them:

* Unknown is its own marker. It is never a colour on the weather scale.
* A marker describes weather. It never claims to be the airport's status.
* Zero aircraft is "none heard, coverage not proven".
* Zero movements is "not available yet": OpenSky publishes flights late, so an empty
  list says nothing about whether anything moved.
* The mode banner is computed from the payloads, never from a setting.
* The last-update badge is computed from payload timestamps, never from the page's clock.
* Every age is measured against the upstream's own clock, the `Date` header carried in
  the payload as `upstream_time_epoch`. Without one the age is unknown. This machine's
  clock is never the fallback: it ran 21 min 27 s fast and made all live data "stale".
"""

from __future__ import annotations

import bisect
import math
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, Final

from flightops_mcp.airports import haversine_nm
from flightops_mcp.opensky import STATES_STALE_AFTER_S
from flightops_mcp.server import METAR_STALE_AFTER_S

FLIGHT_CATEGORIES: Final = frozenset({"VFR", "MVFR", "IFR", "LIFR"})
NONE_HEARD: Final = "none heard, coverage not proven"
MOVEMENTS_NOT_AVAILABLE: Final = "not available yet today"
NO_DATA_YET: Final = "no data yet"
SNAPSHOT_TIME_UNKNOWN: Final = "snapshot time unknown"
NO_UPSTREAM_TIME: Final = "freshness unknown — no upstream time"
# An aircraft last heard this long ago is drawn at the faintest level. Positions are
# only ever faded: moving one between polls would invent where it is.
FADE_AFTER_S: Final = 120.0
MIN_FADE: Final = 0.2
# The page echoes a snapshot time back to /api/board. The year 2100 bounds what it may
# send, so a corrupt upstream time cannot overflow the date formatting.
MAX_SNAPSHOT_EPOCH: Final = 4_102_444_800
# How far away a neighbour's report may be offered for an airport with none. Measured
# on the world set: 5 % of the airports without a report have a reporting airport
# within 25 nm, 18 % within 50 and 48 % within 100. At 100 nm it is not that airport's
# weather. Widening this needs that argument answered here, not a setting.
NEIGHBOUR_MAX_NM: Final = 25.0
# Weather is asked only for the airports nearest the centre of the view, at most this
# many. A world poll asked NOAA about all 3,270 to draw 1,888 grey rings; 600 is what a
# continent at full zoom holds, and twelve NOAA requests.
WEATHER_LOOKUP_CAP: Final = 600
# Added to the view's radius, so a short pan does not uncover airports never asked about.
VIEW_MARGIN_DEG: Final = 5.0
# Below each zoom, airports are drawn as one marker per cell of this many degrees.
# At and above the last zoom every airport is drawn on its own. Measured on the live world
# view (600 airports): 10-degree cells drew 30 to 56 markers at zoom 1, 20-degree cells 11
# to 19 (too coarse to read a continent), 5-degree cells up to 151.
CLUSTER_LEVELS: Final = ((2.0, 10.0), (3.0, 5.0), (4.0, 2.5))
# Airports without a report are hidden by default below this zoom, and shown from it,
# until the reader chooses. The choice is the reader's, and outlasts the page.
UNKNOWN_SHOWN_FROM_ZOOM: Final = 4.0
# The globe lifts each aircraft by its altitude: 12 km, a cruising level, is 3.5 % of
# the radius. Enough to separate 0 from 12 km, too little to bend the sphere.
GLOBE_ALTITUDE_CEILING_M: Final = 12_000.0
GLOBE_MAX_LIFT: Final = 0.035
CATEGORY_ORDER: Final = ("VFR", "MVFR", "IFR", "LIFR")
CATEGORY_SYMBOL: Final = {"VFR": "●", "MVFR": "■", "IFR": "◆", "LIFR": "▲"}

Payload = dict[str, Any]


def age_text(seconds: float) -> str:
    minutes = int(abs(seconds) // 60)
    return f"{minutes} min" if minutes < 120 else f"{minutes // 60} h {minutes % 60} min"


def fade(age_s: float | None) -> float:
    """Opacity for an aircraft heard `age_s` ago. An unknown age gets the faintest."""
    if age_s is None:
        return MIN_FADE
    return round(max(MIN_FADE, min(1.0, 1.0 - max(0.0, age_s) / FADE_AFTER_S)), 2)


def _error(payload: Payload) -> Payload:
    error = payload.get("error")
    return error if isinstance(error, dict) else {}


def _newest_metar(payload: Payload) -> Payload | None:
    metars = payload.get("metar") or []
    return max(metars, key=lambda m: str(m.get("observation_time") or "")) if metars else None


def weather_view(payload: Payload) -> Payload:
    """One airport's weather, with the reason whenever it is not known."""
    if not payload.get("ok"):
        code = _error(payload).get("code", "unknown")
        return {
            "state": "unknown",
            "no_observation": False,
            "category": None,
            "age_s": None,
            "upstream_time": None,
            "upstream_time_missing": False,
            "summary": f"Unknown: weather unavailable ({code})",
            "raw": None,
            "unknowns": [str(_error(payload).get("message", "Weather could not be read."))],
        }
    latest = payload.get("latest_metar") or {}
    age = latest.get("age_s")
    current = bool(latest.get("current"))
    newest = _newest_metar(payload)
    category = (newest or {}).get("flight_category")
    known = category if category in FLIGHT_CATEGORIES else None
    upstream = upstream_time(payload)
    if newest is None:
        summary = "Unknown: no METAR returned"
    elif upstream is None:
        # The tool could not age the METAR, so neither can the board.
        age = None
        summary = f"Unknown: {NO_UPSTREAM_TIME}"
    elif age is None:
        summary = "Unknown: the METAR has no readable time"
    elif not current:
        when = "in the future" if age < 0 else "old"
        summary = f"Unknown: METAR {age_text(age)} {when}"
    elif known is None:
        summary = "Unknown: the METAR gives no flight category"
    else:
        summary = f"{known}, METAR {age_text(age)} old"
    reported = age is not None and current and known is not None
    return {
        "state": "reported" if reported else "unknown",
        # NOAA answered and held nothing for this station. Only these may borrow a
        # neighbour's line: a failed fetch or an old report is a different unknown.
        "no_observation": newest is None,
        "category": known if reported else None,
        "age_s": age,
        "upstream_time": upstream,
        "upstream_time_missing": newest is not None and upstream is None,
        "summary": summary,
        "raw": (newest or {}).get("raw_text"),
        "unknowns": list(payload.get("unknowns") or []),
    }


def neighbours(airports: Sequence[Payload], views: Mapping[str, Payload]) -> dict[str, Payload]:
    """For each airport NOAA holds nothing for, the nearest one that has a current report.

    An annotation, never evidence: the airport's own view, marker and every count stay
    as they were. `views` holds one view per airport in `airports`, and those are the
    only candidates. "Current" is the view's own `reported`, so the tool's 90-minute
    rule decides it, and an airport is never its own neighbour because it cannot be
    both reported and without an observation. Ties go to the lower ICAO
    code, so the same data always names the same neighbour.
    """
    reporting = sorted(
        (airport["latitude"], airport["ident"], airport)
        for airport in airports
        if views[airport["ident"]]["state"] == "reported"
    )
    latitudes = [entry[0] for entry in reporting]
    # A degree of latitude is 60 nm, so nothing outside this band can be within reach.
    band = NEIGHBOUR_MAX_NM / 60.0
    found: dict[str, Payload] = {}
    for airport in airports:
        ident = airport["ident"]
        if not views[ident]["no_observation"]:
            continue
        lat, lon = airport["latitude"], airport["longitude"]
        low = bisect.bisect_left(latitudes, lat - band)
        high = bisect.bisect_right(latitudes, lat + band)
        scored = [
            (haversine_nm(lat, lon, other["latitude"], other["longitude"]), code)
            for _, code, other in reporting[low:high]
        ]
        within = [pair for pair in scored if pair[0] <= NEIGHBOUR_MAX_NM]
        if not within:
            continue
        distance, code = min(within)
        view = views[code]
        found[ident] = {
            "icao": code,
            "distance_nm": round(distance),
            "category": view["category"],
            "age_s": view["age_s"],
            "text": (
                f"No report at {ident} — {code} ({round(distance)} nm away) reports "
                f"{view['category']}, {_ago(view['age_s'])} old"
            ),
        }
    return found


def marker(weather: Payload) -> Payload:
    """How the map draws an airport. `scope` keeps the claim to weather."""
    if weather["state"] == "reported":
        category = weather["category"]
        return {
            "kind": "weather",
            "category": category,
            "label": f"Weather {category}",
            "scope": "weather",
        }
    return {"kind": "unknown", "category": None, "label": "Unknown", "scope": "weather"}


def airports_in_view(
    airports: Sequence[Payload],
    centre_lat: float,
    centre_lon: float,
    radius_deg: float,
    cap: int = WEATHER_LOOKUP_CAP,
) -> tuple[list[Payload], int]:
    """The airports whose weather is worth asking for, and how many the view holds.

    The view is a circle on the sphere, its centre and its radius plus a margin, not a
    latitude/longitude box: a box breaks at the antimeridian and over a pole, and the
    globe's view is a circle anyway. Nearest the centre first, ties by code, so the same
    view always asks about the same airports.
    """
    reach = min(180.0, radius_deg + VIEW_MARGIN_DEG)
    scored = sorted(
        (angular_distance_deg(centre_lat, centre_lon, a["latitude"], a["longitude"]), a["ident"], a)
        for a in airports
    )
    inside = [airport for distance, _, airport in scored if distance <= reach]
    return inside[: max(0, cap)], len(inside)


def cluster_level(zoom: float) -> float | None:
    """The cell size in degrees airports are grouped by at this zoom, or None for none."""
    for below, cell in CLUSTER_LEVELS:
        if zoom < below:
            return cell
    return None


def unknown_shown_by_default(zoom: float) -> bool:
    """Whether airports without a report are drawn before the reader has chosen."""
    return zoom >= UNKNOWN_SHOWN_FROM_ZOOM


def cluster_text(counts: Mapping[str, int], unknown: int, show_unknown: bool) -> str:
    """`● 42 VFR · ◆ 11 IFR`, the worst-first counts a cluster's tooltip reads out."""
    parts = [
        f"{CATEGORY_SYMBOL[category]} {counts[category]} {category}"
        for category in reversed(CATEGORY_ORDER)
        if counts.get(category)
    ]
    if show_unknown and unknown:
        parts.append(f"○ {unknown} without a report")
    return " · ".join(parts) or "no report heard"


def clusters(airports: Sequence[Payload], cell_deg: float) -> list[Payload]:
    """One aggregate per grid cell of `cell_deg` degrees, carrying a count per category.

    `airports` are board entries: a reported one has a `marker`, a trimmed one does not.
    The marker takes the shape of the most common category (`majority`, ties to the worse),
    so a continent of VFR does not read as a continent of LIFR. When something worse is in
    the cell, `worst` names it and the page adds it as a small badge: one IFR airport is not
    hidden inside a cell of VFR ones. Unknowns are counted apart: the page draws or hides
    them with the toggle, and a cell that holds only unknowns has both None.
    """
    cells: dict[tuple[int, int], list[Payload]] = {}
    for airport in airports:
        key = (
            math.floor((airport["latitude"] + 90.0) / cell_deg),
            math.floor((airport["longitude"] + 180.0) / cell_deg),
        )
        cells.setdefault(key, []).append(airport)
    out = []
    for key in sorted(cells):
        members = cells[key]
        counts = dict.fromkeys(CATEGORY_ORDER, 0)
        unknown = 0
        for airport in members:
            category = (airport.get("marker") or {}).get("category")
            if category in counts:
                counts[category] += 1
            else:
                unknown += 1
        reported = [a for a in members if (a.get("marker") or {}).get("category") in counts]
        worst = max(
            (a["marker"]["category"] for a in reported),
            key=CATEGORY_ORDER.index,
            default=None,
        )
        majority = max(
            (category for category in CATEGORY_ORDER if counts[category]),
            key=lambda category: (counts[category], CATEGORY_ORDER.index(category)),
            default=None,
        )
        # Placed at the mean of the airports it stands for, so it sits over land and not
        # at a cell corner. A cell never crosses the antimeridian, so the mean is sound.
        placed_by = reported or members
        out.append(
            {
                "latitude": round(sum(a["latitude"] for a in placed_by) / len(placed_by), 3),
                "longitude": round(sum(a["longitude"] for a in placed_by) / len(placed_by), 3),
                "reported": len(reported),
                "unknown": unknown,
                "counts": counts,
                "majority": majority,
                "worst": worst,
                "text": cluster_text(counts, unknown, show_unknown=False),
                "text_with_unknown": cluster_text(counts, unknown, show_unknown=True),
            }
        )
    return out


def board_airport(record: Payload, weather: Payload, marker_view: Payload) -> Payload:
    """One airport as the map receives it. Without a report: code, name and place only.

    A world poll was 900 KB, most of it weather objects that said "nothing heard". The
    trimmed entry still draws as Unknown, and selecting it fetches its own line. One
    thing is kept: a neighbour's line, for the few that have one, because that line is
    the only thing the map can say about the airport.
    """
    entry: Payload = {
        "icao": record["ident"],
        "name": record["name"],
        "latitude": record["latitude"],
        "longitude": record["longitude"],
    }
    if marker_view["kind"] != "weather":
        return {**entry, "weather": weather} if weather.get("neighbour") else entry
    return {
        **entry,
        "iata": record.get("iata"),
        "type": record.get("type"),
        "weather": weather,
        "marker": marker_view,
    }


def shown_note(unknown: int) -> str:
    """What the toggle shows when it is on."""
    if unknown == 0:
        return "every airport asked has a report"
    return f"{unknown:,} without a report, drawn as dashed rings"


# The tooltip of an airport the map received trimmed: it has no line of its own here.
TRIMMED_TIP: Final = "Unknown: no report heard. Select it for its own line."


def hidden_note(unknown: int) -> str:
    """What the toggle hides, said whether or not anything is hidden."""
    if unknown == 0:
        return "none hidden: every airport asked has a report"
    return f"{unknown:,} hidden — no report heard"


def viewport_note(region_total: int, in_view: int, asked: int, looked_up: int, calls: int) -> str:
    """Which airports were asked about, and how many lookups this poll cost."""
    if asked < in_view:
        scope = f"showing {asked:,} of {in_view:,} nearest the centre"
    else:
        scope = f"showing all {asked:,} in view"
    if in_view < region_total:
        scope += f" ({region_total:,} in the region)"
    calls_text = "call" if calls == 1 else "calls"
    return f"Weather {scope} · {looked_up:,} lookups in {calls} {calls_text}"


def _valid_epoch(epoch: object) -> int | None:
    if isinstance(epoch, bool) or not isinstance(epoch, int):
        return None
    return epoch if 0 < epoch <= MAX_SNAPSHOT_EPOCH else None


def upstream_time(payload: Payload) -> int | None:
    """The upstream's clock when it answered, or None when the payload has none.

    Aircraft ages are measured from here, not from the snapshot's own time: a snapshot
    20 minutes behind must not make its aircraft look freshly heard.
    """
    return _valid_epoch(payload.get("upstream_time_epoch"))


def _measure(value: object) -> float | None:
    """A reported figure, or None. A missing altitude is unknown, never 0."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value) if math.isfinite(value) else None


def globe_lift(altitude_m: float | None) -> float | None:
    """How far above the globe's surface an aircraft is drawn, as a fraction of its radius.

    None for an unknown altitude: the aircraft is drawn on the surface and marked as
    unknown, so a missing altitude cannot pass for one of 0.
    """
    if altitude_m is None:
        return None
    clamped = max(0.0, min(GLOBE_ALTITUDE_CEILING_M, altitude_m))
    return round(clamped / GLOBE_ALTITUDE_CEILING_M * GLOBE_MAX_LIFT, 5)


def angular_distance_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """The great-circle angle between two points, in degrees."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dlat, dlon = p2 - p1, math.radians(lon2 - lon1)
    h = math.sin(dlat / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlon / 2) ** 2
    return math.degrees(2 * math.asin(min(1.0, math.sqrt(h))))


def on_near_side(lat: float, lon: float, centre_lat: float, centre_lon: float) -> bool:
    """Whether a point is on the globe's visible hemisphere.

    The page applies this rule each frame (`place` in board.js) to airports and aircraft
    alike: an aircraft is culled by where it is over the ground, not by how high it is.
    """
    # A hair of tolerance: a point exactly on the limb is visible, and floating point
    # puts it a billionth of a degree either side.
    return angular_distance_deg(lat, lon, centre_lat, centre_lon) <= 90.0 + 1e-9


def orthographic(
    lat: float, lon: float, altitude_m: float | None, centre_lat: float, centre_lon: float
) -> tuple[float, float] | None:
    """Where the globe draws an aircraft, on a unit sphere, north up. None on the far side.

    The surface point is scaled outward from the centre by `1 + lift`: lifting a point
    radially in 3-D is exactly that scaling once it is projected orthographically.
    """
    if not on_near_side(lat, lon, centre_lat, centre_lon):
        return None
    phi, lam = math.radians(lat), math.radians(lon - centre_lon)
    phi0 = math.radians(centre_lat)
    x = math.cos(phi) * math.sin(lam)
    y = math.cos(phi0) * math.sin(phi) - math.sin(phi0) * math.cos(phi) * math.cos(lam)
    scale = 1.0 + (globe_lift(altitude_m) or 0.0)
    return round(x * scale, 6), round(y * scale, 6)


def _heard_utc(epoch: object) -> str | None:
    valid = _valid_epoch(epoch)
    return datetime.fromtimestamp(valid, UTC).strftime("%H:%M:%SZ") if valid else None


def _aircraft(entry: Payload, now: float | None) -> Payload:
    heard = entry.get("last_contact")
    age = round(now - heard) if now and heard else None
    altitude = _measure(entry.get("baro_altitude_m"))
    return {
        "icao24": entry.get("icao24"),
        "callsign": entry.get("callsign"),
        "latitude": entry.get("latitude"),
        "longitude": entry.get("longitude"),
        "on_ground": entry.get("on_ground"),
        "track": entry.get("true_track_deg"),
        "altitude_m": altitude,
        "velocity_ms": _measure(entry.get("velocity_ms")),
        "lift": globe_lift(altitude),
        "last_contact": _valid_epoch(heard),
        # OpenSky's time for the position, so a trail point is labelled with its own time.
        "heard_utc": _heard_utc(heard),
        "age_s": age,
        "fade": fade(age),
    }


def traffic_view(payload: Payload) -> Payload:
    """The aircraft heard near an airport, or why that is not known."""
    if not payload.get("ok"):
        error = _error(payload)
        return {
            "state": "unknown",
            "count": None,
            "summary": f"Unknown: traffic unavailable ({error.get('code', 'unknown')})",
            "hint": error.get("hint"),
            "credentials": payload.get("credentials"),
            "freshness": None,
            "aircraft": [],
            "unknowns": [str(error.get("message", "Traffic could not be read."))],
        }
    count = int((payload.get("counts") or {}).get("returned", 0))
    current = bool(payload.get("snapshot_current"))
    if count == 0:
        state, summary = "none_heard", NONE_HEARD
    elif current:
        state, summary = "heard", f"{count} heard"
    else:
        state, summary = "stale", f"{count} heard in a snapshot that is not current"
    now = upstream_time(payload)
    return {
        "state": state,
        "count": count,
        "summary": summary,
        "hint": None,
        "credentials": payload.get("credentials"),
        "freshness": payload.get("freshness"),
        "aircraft": [_aircraft(a, now) for a in payload.get("aircraft") or []],
        "unknowns": list(payload.get("unknowns") or []),
    }


def _snapshot_epoch(payload: Payload) -> int:
    """The snapshot's own time, or 0 when it has none the board can use."""
    return _valid_epoch(payload.get("snapshot_time_epoch")) or 0


def airspace_view(payload: Payload) -> Payload:
    """The wide traffic layer, flattened for drawing."""
    if not payload.get("ok"):
        error = _error(payload)
        return {
            "state": "unknown",
            "summary": f"Unknown: traffic unavailable ({error.get('code', 'unknown')})",
            "hint": error.get("hint"),
            "snapshot_epoch": 0,
            "upstream_epoch": 0,
            "aircraft": [],
            "unknowns": [str(error.get("message", "Traffic could not be read."))],
        }
    columns = payload["columns"]
    now = upstream_time(payload)
    aircraft = [
        _aircraft(dict(zip(columns, row, strict=True)), now) for row in payload.get("rows") or []
    ]
    current = bool(payload.get("snapshot_current"))
    if not aircraft:
        state, summary = "none_heard", NONE_HEARD
    elif current:
        state, summary = "heard", f"{len(aircraft)} heard"
    else:
        state, summary = "stale", f"{len(aircraft)} heard in a snapshot that is not current"
    return {
        "state": state,
        "summary": summary,
        "hint": None,
        "region": payload.get("region"),
        "credentials": payload.get("credentials"),
        "freshness": payload.get("freshness"),
        "truncated": bool(payload.get("truncated")),
        "snapshot_epoch": _snapshot_epoch(payload),
        # Echoed back by the page with the snapshot time, so the badge can age the
        # snapshot against OpenSky's clock. 0 when the answer carried no `Date`.
        "upstream_epoch": now or 0,
        "aircraft": aircraft,
        "unknowns": list(payload.get("unknowns") or []),
    }


def refused_view(reason: str) -> Payload:
    """A traffic refresh that was not made. Nothing is drawn and no time is claimed."""
    return {
        "state": "refused",
        "summary": reason,
        "hint": None,
        "snapshot_epoch": 0,
        "upstream_epoch": 0,
        "aircraft": [],
        "unknowns": [reason],
        "refused": reason,
    }


def _movement_text(count: int | None) -> str:
    return str(count) if count else MOVEMENTS_NOT_AVAILABLE


def _row(label: str, text: str) -> Payload:
    return {"label": label, "text": text}


def _number(value: object) -> str | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return f"{value:g}"


def _unreadable(entry: Payload) -> str:
    unreadable = int(entry.get("unreadable") or 0)
    return f"; {unreadable} of {entry['received']} rows unreadable" if unreadable else ""


def count_rows(counts: Payload | None) -> list[Payload]:
    """The triage's counts as the panel lists them. A count never made says so."""
    counts = counts or {}
    window = counts.get("movements_window_minutes")
    rows = []
    for key, label in (("arrivals", "Arrivals"), ("departures", "Departures")):
        entry = counts.get(key)
        if entry is None:
            rows.append(_row(label, "not counted: movements unavailable"))
        else:
            rows.append(
                _row(label, f"{entry['count']} in the last {window} min{_unreadable(entry)}")
            )
    aircraft = counts.get("aircraft_in_search_box")
    if aircraft is None:
        rows.append(_row("Aircraft nearby", "not counted: traffic unavailable"))
    else:
        radius = _number(counts.get("search_radius_nm"))
        text = (
            f"{aircraft['count']} heard within {radius} nm ({aircraft.get('airborne')} airborne, "
            f"{aircraft.get('on_ground')} on the ground){_unreadable(aircraft)}"
        )
        if aircraft.get("truncated"):
            text += f"; only the {aircraft['listed']} most recent listed"
        rows.append(_row("Aircraft nearby", text))
    return rows


def _wind_text(wind: Payload) -> str:
    speed = _number(wind.get("speed_kt"))
    if speed is None:
        return "not in the report"
    direction = _number(wind.get("direction_deg"))
    if wind.get("variable"):
        heading = "variable"
    elif direction is None:
        heading = "direction not given"
    else:
        heading = f"{direction.zfill(3)}°"
    gust = _number(wind.get("gust_kt"))
    return f"{heading} {speed} kt, " + (f"gusts {gust} kt" if gust else "no gusts reported")


def weather_rows(weather: Payload | None) -> list[Payload]:
    """The newest METAR as values. Each figure the report did not give says so."""
    if weather is None:
        return [_row("METAR", "none returned, so no values")]
    category = weather.get("category") or "none given"
    if not weather.get("current"):
        category += " (not current)"
    age = weather.get("age_s")
    ceiling = weather.get("ceiling") or {}
    feet = ceiling.get("ft")
    visibility = weather.get("visibility") or {}
    miles = _number(visibility.get("statute_mi"))
    return [
        _row("Category", category),
        _row("METAR age", "unknown" if age is None else f"{_ago(age)} by NOAA's clock"),
        _row(
            "Ceiling",
            f"{feet:,} ft ({ceiling.get('basis')})"
            if isinstance(feet, int)
            else str(ceiling.get("basis", "not in the report")),
        ),
        _row(
            "Visibility",
            "not in the report"
            if miles is None
            else f"{miles}{' or more' if visibility.get('at_least') else ''} sm",
        ),
        _row("Wind", _wind_text(weather.get("wind") or {})),
    ]


LEG_STATES: Final = {
    "observed": "observed",
    "empty": "answered, nothing in it",
    "not_current": "answered, not current",
    "unavailable": "unavailable",
    "not_fetched": "not fetched",
}


def leg_rows(legs: Payload | None) -> list[Payload]:
    """Each leg's state, age by its own upstream's clock, and source."""
    rows = []
    for key, entry in (legs or {}).items():
        state = LEG_STATES.get(entry.get("status"), "unknown")
        if entry.get("error"):
            state += f" ({entry['error']})"
        age = entry.get("age_s")
        when = "age unknown" if age is None else f"data {_ago(age)} old"
        rows.append(
            {
                "label": key.capitalize(),
                "state": entry.get("status"),
                "text": f"{state}; {when}; {entry.get('source')}",
            }
        )
    return rows


def triage_view(payload: Payload) -> Payload:
    """A triage as the panel shows it, with its reasons and what it could not see."""
    if not payload.get("ok"):
        error = _error(payload)
        return {
            "severity": "unknown",
            "label": "Unknown",
            "reasons": [],
            "unknowns": [str(error.get("message", "The triage could not run."))],
            "legs_without_evidence": [],
            "movements": {"arrivals": "not available", "departures": "not available"},
            "counts": [],
            "weather": [],
            "legs": [],
            "why_not_higher": [],
            "why_not_lower": [],
        }
    assessment = payload.get("assessment") or {}
    severity = str(assessment.get("severity", "unknown"))
    summary = (payload.get("evidence") or {}).get("movements_summary")
    movements = (
        {
            "arrivals": _movement_text(summary.get("arrivals")),
            "departures": _movement_text(summary.get("departures")),
        }
        if summary
        else {"arrivals": "not available", "departures": "not available"}
    )
    return {
        "severity": severity,
        "label": "Unknown" if severity == "unknown" else severity.capitalize(),
        "reasons": list(assessment.get("reasons") or []),
        "unknowns": list(assessment.get("unknowns") or []),
        "legs_without_evidence": list(assessment.get("legs_without_evidence") or []),
        "movements": movements,
        # A triage from before these fields existed (an old recording) shows none of
        # them, rather than rows that claim nothing was counted or reported.
        "counts": count_rows(payload["counts"]) if "counts" in payload else [],
        "weather": weather_rows(payload["weather"]) if "weather" in payload else [],
        "legs": leg_rows(payload.get("legs")),
        "why_not_higher": list(assessment.get("why_not_higher") or []),
        "why_not_lower": list(assessment.get("why_not_lower") or []),
    }


def governance(status: Payload) -> Payload:
    """The strip along the bottom. A figure that cannot be read stays None, never 0."""
    budget = status.get("budget") or {}
    cache = status.get("cache") or {}
    credentials = status.get("credentials") or {}
    return {
        "credentials": credentials.get("opensky"),
        "credits_hour": budget.get("credits_spent_this_hour"),
        "budget_hour": budget.get("local_budget_per_hour"),
        "credits_24h": budget.get("dashboard_credits_spent_24h"),
        "budget_24h": budget.get("dashboard_budget_per_24h"),
        "ledger_survives_restart": budget.get("daily_ledger_survives_restart"),
        "cache_hit_rate": cache.get("hit_rate"),
        "upstream_remaining": budget.get("upstream_remaining_last_seen"),
    }


def banner(weather: list[Payload], recorded_at: str | None) -> Payload:
    """LIVE, STALE, RECORDED or NO DATA, from what the payloads actually hold."""
    total = len(weather)
    current = sum(1 for view in weather if view["state"] == "reported")
    dated = sum(1 for view in weather if view["age_s"] is not None)
    if recorded_at is not None:
        return {"mode": "RECORDED", "label": f"RECORDED {recorded_at}"}
    if current:
        return {"mode": "LIVE", "label": f"LIVE: {current} of {total} with a current METAR"}
    if dated:
        return {"mode": "STALE", "label": f"STALE: no current METAR among {total} airports"}
    return {"mode": "NO DATA", "label": "NO DATA"}


def _ago(age_s: float) -> str:
    return "under 1 min" if age_s < 60 else age_text(age_s)


def _utc_text(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")


def _recorded_text(recorded_at: str) -> str:
    """The recording's own time. One that cannot be read is shown as written."""
    try:
        moment = datetime.fromisoformat(recorded_at)
    except ValueError:
        return recorded_at or "time unknown"
    return _utc_text(moment) if moment.tzinfo else recorded_at


def _line(source: str, label: str, state: str, text: str) -> Payload:
    return {"source": source, "label": label, "state": state, "text": text}


def _age_line(source: str, label: str, age_s: float, limit_s: int) -> Payload:
    # The limits are the tools' own, so the badge and `current` cannot disagree.
    if age_s < -limit_s:
        return _line(source, label, "stale", "stale: dated in the future")
    if age_s > limit_s:
        return _line(source, label, "stale", f"stale: last update {_ago(age_s)} ago")
    return _line(source, label, "live", f"Last update {_ago(age_s)} ago")


def _weather_line(weather: list[Payload]) -> Payload:
    ages = [view["age_s"] for view in weather if view["age_s"] is not None]
    if not ages:
        if any(view.get("upstream_time_missing") for view in weather):
            return _line("weather", "Weather", "no_upstream_time", NO_UPSTREAM_TIME)
        return _line("weather", "Weather", "no_data", NO_DATA_YET)
    # The freshest report wins, but one dated far ahead of the clock is not fresh.
    newest = min(ages, key=lambda age: (age < -METAR_STALE_AFTER_S, max(age, 0)))
    return _age_line("weather", "Weather", newest, METAR_STALE_AFTER_S)


def _traffic_line(snapshot: int, upstream: int | None, recorded: bool) -> Payload:
    if snapshot and recorded:
        when = _utc_text(datetime.fromtimestamp(snapshot, UTC))
        return _line("traffic", "Traffic", "recorded", f"snapshot {when}")
    if not snapshot:
        return _line("traffic", "Traffic", "unknown", SNAPSHOT_TIME_UNKNOWN)
    if upstream is None:
        return _line("traffic", "Traffic", "no_upstream_time", NO_UPSTREAM_TIME)
    return _age_line("traffic", "Traffic", upstream - snapshot, STATES_STALE_AFTER_S)


def last_update(
    weather: list[Payload],
    recorded_at: str | None,
    traffic_snapshot: int | None = None,
    traffic_upstream: int | None = None,
    traffic_refused: str | None = None,
) -> Payload:
    """The map's badge: how old the data on screen is, one line per source.

    Weather and traffic age separately, so neither number can stand in for the other.
    `traffic_snapshot` is None while no traffic layer is drawn; `traffic_upstream` is
    OpenSky's clock when it sent that snapshot. A recording shows when it was made,
    because an age measured during playback would describe the playback.
    `traffic_refused` is why the newest refresh was not made; it gets its own line, so
    an old snapshot is never read as a layer that is still refreshing.
    """
    recorded = recorded_at is not None
    if recorded_at is not None:
        lines = [_line("recording", "Recorded", "recorded", _recorded_text(recorded_at))]
    else:
        lines = [_weather_line(weather)]
    if traffic_snapshot is not None:
        lines.append(_traffic_line(traffic_snapshot, _valid_epoch(traffic_upstream), recorded))
        if traffic_refused:
            lines.append(_line("traffic", "Refresh", "refused", traffic_refused))
    stale = any(line["state"] in ("stale", "refused") for line in lines)
    return {"state": "stale" if stale else lines[0]["state"], "lines": lines}
