"""What the board shows, decided here and not in the browser.

The rules are about honesty, so they live where a test can hold them:

* Unknown is its own marker. It is never a colour on the weather scale.
* A marker describes weather. It never claims to be the airport's status.
* Zero aircraft is "none heard, coverage not proven".
* Zero movements is "not available yet": OpenSky publishes flights late, so an empty
  list says nothing about whether anything moved.
* The mode banner is computed from the payloads, never from a setting.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Final

FLIGHT_CATEGORIES: Final = frozenset({"VFR", "MVFR", "IFR", "LIFR"})
NONE_HEARD: Final = "none heard, coverage not proven"
MOVEMENTS_NOT_AVAILABLE: Final = "not available yet today"
# An aircraft last heard this long ago is drawn at the faintest level. Positions are
# only ever faded: moving one between polls would invent where it is.
FADE_AFTER_S: Final = 120.0
MIN_FADE: Final = 0.2

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
            "category": None,
            "age_s": None,
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
    if newest is None:
        summary = "Unknown: no METAR returned"
    elif age is None:
        summary = "Unknown: the METAR has no readable time"
    elif not current:
        when = "in the future" if age < 0 else "old"
        summary = f"Unknown: METAR {age_text(age)} {when}"
    elif known is None:
        summary = "Unknown: the METAR gives no flight category"
    else:
        summary = f"{known}, METAR {age_text(age)} old"
    reported = current and known is not None
    return {
        "state": "reported" if reported else "unknown",
        "category": known if reported else None,
        "age_s": age,
        "summary": summary,
        "raw": (newest or {}).get("raw_text"),
        "unknowns": list(payload.get("unknowns") or []),
    }


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


def _answered_at(payload: Payload) -> float | None:
    """When the tool answered. Ages are measured from here, not from the snapshot's own
    time: a snapshot 20 minutes behind must not make its aircraft look freshly heard."""
    try:
        return datetime.fromisoformat(str(payload.get("as_of"))).timestamp()
    except ValueError:
        return None


def _aircraft(entry: Payload, now: float | None) -> Payload:
    heard = entry.get("last_contact")
    age = round(now - heard) if now and heard else None
    return {
        "icao24": entry.get("icao24"),
        "callsign": entry.get("callsign"),
        "latitude": entry.get("latitude"),
        "longitude": entry.get("longitude"),
        "on_ground": entry.get("on_ground"),
        "track": entry.get("true_track_deg"),
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
    now = _answered_at(payload)
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


def airspace_view(payload: Payload) -> Payload:
    """The wide traffic layer, flattened for drawing."""
    if not payload.get("ok"):
        error = _error(payload)
        return {
            "state": "unknown",
            "summary": f"Unknown: traffic unavailable ({error.get('code', 'unknown')})",
            "hint": error.get("hint"),
            "aircraft": [],
            "unknowns": [str(error.get("message", "Traffic could not be read."))],
        }
    columns = payload["columns"]
    now = _answered_at(payload)
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
        "aircraft": aircraft,
        "unknowns": list(payload.get("unknowns") or []),
    }


def _movement_text(count: int | None) -> str:
    return str(count) if count else MOVEMENTS_NOT_AVAILABLE


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
