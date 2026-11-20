"""What is approaching the selected airport, and how the board may say so.

The traffic layer holds no destination: a transponder broadcasts position, not intent, and
OpenSky's `estArrivalAirport` lives on the flights endpoints, not on `states/all`. So this is
geometry, and it says so:

* Approaching means airborne, within `RADIUS_NM`, and course convergent: the bearing from the
  aircraft to the airport within `CONVERGENCE_DEG` of its track. The thresholds are printed
  with the result, so a reader can disagree with them.
* The line drawn for one is projected, not observed: a great circle from where it was heard
  to the airport. An interpolation dressed as a track is the same lie as a zero altitude.
* An ETA is distance over ground speed, rounded, and labelled estimated. None without a
  known, positive speed, and none on the ground.
* A callsign on the airport's arrival board is evidence, and is counted apart from geometry.
  A callsign missing from the board removes nothing: OpenSky lists a flight once it ends.
* Observed tracks cost `TRACK_COST` each and are fetched only on request, at a price stated
  before anything is spent.
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
from typing import Any, Final

from flightops_mcp.airports import haversine_nm
from flightops_mcp.opensky import TRACK_COST

from .aircraft import MS_TO_KT, airline, bearing_deg

Payload = dict[str, Any]

# 60 nm is about 15 minutes out for a jet on descent: wide enough to catch an arrival
# before it turns final, narrow enough that a straight line to the field is plausible.
RADIUS_NM: Final = 60.0
# ±30° allows for a dogleg onto a downwind or a vector, and still excludes overflights
# crossing at right angles.
CONVERGENCE_DEG: Final = 30.0
DRAWN_MAX: Final = 12
PROJECTED: Final = "projected, not observed"
NO_LAYER: Final = (
    "Turn traffic on to see what is approaching. It reads the traffic layer the board "
    "already holds, so it costs nothing."
)
BOARD_NOT_ASKED: Final = "Arrival board not checked."


def off_course_deg(track: float, bearing: float) -> float:
    """The smallest angle between a track and a bearing, 0 to 180. 350° and 10° are 20° apart."""
    return abs((track - bearing + 180.0) % 360.0 - 180.0)


def eta_minutes(distance_nm: float, velocity_ms: object, on_ground: object) -> int | None:
    """Minutes to the airport at the current ground speed, rounded. None when it cannot be said.

    Never from an unknown speed: a missing speed is not zero, and zero would divide.
    """
    if on_ground is not False:
        return None
    if isinstance(velocity_ms, bool) or not isinstance(velocity_ms, int | float):
        return None
    if velocity_ms <= 0:
        return None
    return max(1, round(distance_nm / (velocity_ms * MS_TO_KT) * 60.0))


def eta_text(minutes: int | None) -> str:
    if minutes is None:
        return "no ETA: ground speed unknown or zero"
    return f"ETA about {minutes} min, estimated from ground speed and distance"


def _callsign(value: object) -> str | None:
    return value.strip().upper() or None if isinstance(value, str) else None


def _placed(entry: Payload) -> bool:
    return isinstance(entry.get("latitude"), int | float) and isinstance(
        entry.get("longitude"), int | float
    )


def _measured(entry: Payload, airport: Payload) -> Payload:
    """The aircraft with its distance to the airport and the bearing it would fly to reach it."""
    lat, lon = entry["latitude"], entry["longitude"]
    to_lat, to_lon = airport["latitude"], airport["longitude"]
    return {
        **entry,
        "distance_nm": round(haversine_nm(lat, lon, to_lat, to_lon), 1),
        "bearing_to_airport_deg": round(bearing_deg(lat, lon, to_lat, to_lon)),
    }


def convergent(entry: Payload) -> bool:
    """Airborne, inside the radius, and pointed at the airport. `entry` is `_measured`."""
    track = entry.get("track")
    if entry.get("on_ground") is not False:
        return False
    if isinstance(track, bool) or not isinstance(track, int | float):
        return False
    if entry["distance_nm"] > RADIUS_NM:
        return False
    return off_course_deg(track, entry["bearing_to_airport_deg"]) <= CONVERGENCE_DEG


def _row(entry: Payload, on_board: bool) -> Payload:
    minutes = eta_minutes(entry["distance_nm"], entry.get("velocity_ms"), entry.get("on_ground"))
    derived = airline(entry.get("callsign"))
    altitude = entry.get("altitude_m")
    name = _callsign(entry.get("callsign")) or str(entry.get("icao24"))
    parts = [name]
    if derived is not None and derived["name"]:
        parts.append(f"{derived['name']} (from the callsign)")
    parts.append(f"{entry['distance_nm']:,.1f} nm")
    parts.append("altitude not reported" if altitude is None else f"{altitude:,.0f} m")
    parts.append(eta_text(minutes))
    parts.append("on the arrival board" if on_board else f"approaching by geometry, {PROJECTED}")
    return {
        **entry,
        "airline_name": derived["name"] if derived else None,
        "eta_min": minutes,
        "evidence": "arrival_board" if on_board else "geometry",
        "text": " · ".join(parts),
    }


def rule_text(icao: str) -> str:
    return (
        f"Approaching means airborne, within {RADIUS_NM:g} nm of {icao}, with its track within "
        f"±{CONVERGENCE_DEG:g}° of the bearing to {icao}. The nearest {DRAWN_MAX} are drawn. "
        f"Each line is {PROJECTED}."
    )


def summary_text(on_board: int, approaching: int, within: int, board_asked: bool) -> str:
    """`3 on the arrival board · 5 more approaching · 27 within 60 nm`."""
    if board_asked:
        parts = [f"{on_board} on the arrival board", f"{approaching} more approaching"]
    else:
        parts = [f"{approaching} approaching"]
    parts.append(f"{within} within {RADIUS_NM:g} nm")
    return " · ".join(parts)


def price(count: int) -> Payload:
    """The observed-track button, with its arithmetic in the label."""
    total = count * TRACK_COST
    return {
        "count": count,
        "each": TRACK_COST,
        "total": total,
        "text": f"Show observed tracks ({count} aircraft · {TRACK_COST} credits each = {total})",
    }


def inbound_view(
    traffic: Sequence[Payload], airport: Payload, board_callsigns: Collection[str] | None
) -> Payload:
    """The approaching aircraft for one airport, from a held layer. Spends nothing.

    `traffic` are `board._aircraft` entries. `board_callsigns` is the airport's arrival
    board, or None when it was not asked for.
    """
    icao = airport["icao"]
    board_set = {c for c in (_callsign(x) for x in board_callsigns or ()) if c}
    measured = [_measured(entry, airport) for entry in traffic if _placed(entry)]
    within = [entry for entry in measured if entry["distance_nm"] <= RADIUS_NM]
    picked = []
    for entry in within:
        # Evidence only while airborne: a landed aircraft on the list has arrived already.
        matched = _callsign(entry.get("callsign")) in board_set and entry.get("on_ground") is False
        if matched or convergent(entry):
            picked.append(_row(entry, matched))
    picked.sort(key=lambda row: (row["distance_nm"], str(row.get("icao24") or "")))
    drawn = picked[:DRAWN_MAX]
    on_board = sum(1 for row in picked if row["evidence"] == "arrival_board")
    approaching = len(picked) - on_board
    asked = board_callsigns is not None
    return {
        "airport": icao,
        "thresholds": {
            "radius_nm": RADIUS_NM,
            "convergence_deg": CONVERGENCE_DEG,
            "drawn_max": DRAWN_MAX,
        },
        "rule": rule_text(icao),
        "within_radius": len(within),
        "on_board": on_board,
        "approaching": approaching,
        "candidates": len(picked),
        "drawn": len(drawn),
        "text": summary_text(on_board, approaching, len(within), asked),
        "dropped": (
            f"drawing the nearest {len(drawn)} of {len(picked)}"
            if len(drawn) < len(picked)
            else None
        ),
        "board": "asked" if asked else "not_asked",
        "board_text": None if asked else BOARD_NOT_ASKED,
        "aircraft": drawn,
        "price": price(len(drawn)),
    }


def arrival_callsigns(payload: Payload) -> list[str] | None:
    """The callsigns on an `airport_movements` arrival list, or None if it was not read."""
    if not payload.get("ok"):
        return None
    rows = (payload.get("movements") or {}).get("arrival")
    if rows is None:
        return None
    return [c for c in (_callsign(row.get("callsign")) for row in rows) if c]


def board_view(payload: Payload) -> Payload:
    """What asking for the arrival board found, in words."""
    found = arrival_callsigns(payload)
    if found is None:
        error = payload.get("error") or {}
        unknowns = list(payload.get("unknowns") or [])
        reason = error.get("message") or (unknowns[0] if unknowns else "no arrival list returned")
        text = f"Arrival board unavailable: {reason}"
        return {"state": "unavailable", "callsigns": None, "text": text}
    window = (payload.get("window") or {}).get("minutes")
    span = f" in the last {window} min" if window else ""
    return {
        "state": "read",
        "callsigns": found,
        "text": (
            f"{len(found)} on OpenSky's arrival list{span}. OpenSky lists a flight once it has "
            "landed, so an aircraft still flying is usually not on it."
        ),
    }


def tracks_refusal(count: int, remaining: int | None) -> str | None:
    """Why `count` tracks cannot be bought, or None when they may be. Unknown is not refused."""
    if count == 0:
        return "Nothing is approaching, so there are no tracks to show."
    needed = count * TRACK_COST
    if remaining is not None and needed > remaining:
        noun = "track" if count == 1 else "tracks"
        left = max(0, remaining)
        return f"Not enough credits for {count} {noun} ({needed} needed, {left} remaining)."
    return None


def remaining_credits(governance: Payload) -> int | None:
    """The fewest credits any known guard has left: the hourly ledger and the daily one."""
    left = []
    for spent, budget in (("credits_hour", "budget_hour"), ("credits_24h", "budget_24h")):
        used, cap = governance.get(spent), governance.get(budget)
        if isinstance(used, int) and isinstance(cap, int):
            left.append(cap - used)
    return min(left) if left else None
