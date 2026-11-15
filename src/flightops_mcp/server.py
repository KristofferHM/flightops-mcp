"""The MCP server: tool definitions, honestly-labelled composition, and a CLI.

Every tool obeys three rules:

* **Return data, do not raise.** Expected failures come back as a payload with
  `ok: false` and a machine-readable `code`, so the calling model can correct
  itself. The MCP specification puts validation and upstream failures on the
  tool-execution error channel for exactly this reason.
* **Say what is unknown.** No tool infers absence from an empty response. A
  204 from NOAA, an empty movements list and a missing credential each produce a
  different, explicit `unknowns` entry.
* **Carry provenance.** Every payload names its upstream and the licence terms,
  because a model that cites "the API" without saying which one is unusable in
  an operations review.
"""

from __future__ import annotations

import argparse
import asyncio
import functools
from datetime import UTC, datetime
from typing import Any

from mcp.server import MCPServer

from .config import Settings
from .errors import ToolFailure, not_found, reference_data_missing
from .opensky import MAX_HISTORY_S, TIME_RESOLUTION_S, BBox
from .runtime import Runtime, get_runtime
from .telemetry import TOOL_CALLS, configure_logging, get_logger
from .weather import fleet_impact_flags

INSTRUCTIONS = """\
Use this server for questions about the operational state of an airport and the \
traffic around it.

Rules that the tools will enforce:

1. Resolve identifiers first. `resolve_airport` is offline and free; it returns \
ICAO (ident), IATA, name and coordinates. Never guess a coordinate.
2. `airport_live_traffic`, `airport_movements` and `disruption_triage` spend \
OpenSky API credits. Prefer one `disruption_triage` call over three separate \
ones.
3. `airport_weather` needs no credentials and no credits.
4. Absence of data is not evidence. Each payload lists `unknowns`; if it is \
non-empty, say so instead of concluding that operations are normal.
5. This server is advisory. It reports observations and flags; it never \
authorises an operational decision.
"""

mcp: MCPServer = MCPServer(
    name="flightops-mcp",
    version="0.1.0",
    instructions=INSTRUCTIONS,
)

_log = get_logger(__name__)


def guarded(fn: Any) -> Any:
    """Convert expected failures into payloads; keep the tool signature intact.

    `functools.wraps` matters more than it looks: the MCP SDK derives the JSON
    schema for a tool from the decorated function's signature, so a `*args,
    **kwargs` wrapper would publish an empty, useless schema.
    """

    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> dict[str, Any]:
        try:
            result: dict[str, Any] = await fn(*args, **kwargs)
        except ToolFailure as exc:
            TOOL_CALLS.labels(tool=fn.__name__, outcome=exc.code).inc()
            _log.info("tool_failure", tool=fn.__name__, code=exc.code)
            return exc.to_payload()
        except Exception as exc:  # noqa: BLE001 - a tool must never crash the session
            TOOL_CALLS.labels(tool=fn.__name__, outcome="internal_error").inc()
            _log.exception("tool_internal_error", tool=fn.__name__, error_class=type(exc).__name__)
            return ToolFailure(
                code="internal_error",
                message="The tool hit an unexpected internal error and did not complete.",
                hint="Report this rather than retrying blindly; the server log holds the detail.",
            ).to_payload()
        TOOL_CALLS.labels(tool=fn.__name__, outcome="ok").inc()
        return result

    return wrapper


def _iso_now(clock: Any) -> str:
    return datetime.fromtimestamp(clock(), tz=UTC).isoformat()


def _require_airport(runtime: Runtime, ident: str) -> dict[str, Any]:
    if not runtime.airports.exists:
        raise reference_data_missing(str(runtime.airports.path))
    airport = runtime.airports.get(ident)
    if airport is None:
        raise not_found("airport", ident)
    return airport


# --- Tools -------------------------------------------------------------------
@mcp.tool()
@guarded
async def resolve_airport(query: str, limit: int = 5) -> dict[str, Any]:
    """Resolve an airport from an ICAO/IATA code or a name fragment.

    Offline and free: reads the bundled OurAirports reference database, so it
    costs no API credits and works with no credentials configured.

    Args:
        query: ICAO (ESGG), IATA (GOT), or part of an airport name.
        limit: Maximum matches to return (1-20).
    """
    runtime = get_runtime()
    if not runtime.airports.exists:
        raise reference_data_missing(str(runtime.airports.path))
    matches = runtime.airports.lookup(query, limit=max(1, min(20, limit)))
    if not matches:
        raise not_found("airport", query)
    return {
        "ok": True,
        "query": query,
        "matches": [
            {
                "icao": row["ident"],
                "iata": row["iata"],
                "name": row["name"],
                "type": row["type"],
                "municipality": row["municipality"],
                "country": row["iso_country"],
                "latitude": row["latitude"],
                "longitude": row["longitude"],
                "elevation_ft": row["elevation_ft"],
            }
            for row in matches
        ],
        "source": {
            "name": "OurAirports",
            "licence": "Public domain",
            "kind": "bundled CSV snapshot",
        },
    }


@mcp.tool()
@guarded
async def airport_live_traffic(
    airport: str,
    radius_nm: float = 40.0,
    max_aircraft: int = 40,
) -> dict[str, Any]:
    """Return aircraft currently within a radius of an airport.

    Costs 1 OpenSky credit for a box up to 25 square degrees, up to 4 for a very
    large radius. Aircraft are sorted by most recently heard.

    Args:
        airport: ICAO code, e.g. ESGG.
        radius_nm: Search radius in nautical miles (5-200).
        max_aircraft: Cap on returned aircraft (1-200).
    """
    runtime = get_runtime()
    record = _require_airport(runtime, airport)
    if record.get("latitude") is None or record.get("longitude") is None:
        raise ToolFailure(
            code="airport_has_no_coordinates",
            message=f"{record['ident']} has no reference coordinates.",
            hint="Pick an airport with coordinates; do not guess a bounding box.",
        )
    radius = max(5.0, min(200.0, radius_nm))
    bbox = BBox.around(record["latitude"], record["longitude"], radius)
    snapshot = await runtime.opensky.states_in_bbox(
        bbox, max_vectors=max(1, min(200, max_aircraft))
    )

    airborne = sum(1 for vector in snapshot.vectors if not vector.on_ground)
    on_ground = len(snapshot.vectors) - airborne
    return {
        "ok": True,
        "as_of": _iso_now(runtime.clock),
        "snapshot_time_epoch": snapshot.time,
        "airport": {"icao": record["ident"], "name": record["name"]},
        "search": {
            "radius_nm": radius,
            "bbox": bbox.as_params(),
            "bbox_area_sq_deg": round(bbox.area_sq_deg, 2),
        },
        "counts": {"returned": len(snapshot.vectors), "airborne": airborne, "on_ground": on_ground},
        "aircraft": [vector.to_payload() for vector in snapshot.vectors],
        "bookkeeping": {
            "credits_spent": snapshot.credits_spent,
            "credits_remaining_upstream": snapshot.credits_remaining,
            "truncated": snapshot.truncated,
            "notes": snapshot.notes,
        },
        "unknowns": []
        if snapshot.vectors
        else [
            "No aircraft heard in this box. That can mean quiet airspace, or a gap in "
            "receiver coverage; it is not evidence that the airport is closed."
        ],
        "source": {
            "name": "OpenSky Network",
            "endpoint": "/states/all",
            "licence": (
                "Free for personal and non-profit use; commercial use needs OpenSky's consent"
            ),
        },
    }


@mcp.tool()
@guarded
async def airport_movements(
    airport: str,
    direction: str = "both",
    window_minutes: int = 60,
) -> dict[str, Any]:
    """List estimated arrivals and/or departures for an airport.

    Each direction costs 4 OpenSky credits for a live window of under 24 hours.
    OpenSky derives these movements from ADS-B, so they are estimates: the
    origin/destination fields are inferred from the first and last seen position.

    Args:
        airport: ICAO code, e.g. ESGG.
        direction: "arrival", "departure", or "both".
        window_minutes: Look-back window in minutes (5-60; OpenSky serves at most
            one hour of history).
    """
    runtime = get_runtime()
    record = _require_airport(runtime, airport)
    wanted = direction.lower().strip()
    if wanted not in {"arrival", "departure", "both"}:
        raise ToolFailure(
            code="invalid_argument",
            message="direction must be 'arrival', 'departure' or 'both'",
            hint="Use 'both' if you are unsure; it costs twice the credits of one direction.",
        )
    window = max(5, min(window_minutes, MAX_HISTORY_S // 60))
    end = int(runtime.clock())
    end -= end % TIME_RESOLUTION_S
    begin = end - window * 60

    directions = ["arrival", "departure"] if wanted == "both" else [wanted]
    results: dict[str, list[dict[str, Any]]] = {}
    missing: list[str] = []
    for which in directions:
        try:
            movements = await runtime.opensky.movements(
                record["ident"], which, begin=begin, end=end
            )
        except ToolFailure as exc:
            if exc.code == "credentials_missing":
                missing.append(which)
                continue
            raise
        results[which] = [movement.to_payload() for movement in movements]

    unknowns: list[str] = []
    for which in missing:
        unknowns.append(f"{which} data unavailable: OpenSky credentials are not configured.")
    if not missing and all(not rows for rows in results.values()):
        unknowns.append(
            "No movements recorded in this window. OpenSky only sees ADS-B-equipped "
            "aircraft within receiver coverage, so a quiet result is not proof of no traffic."
        )

    return {
        "ok": True,
        "as_of": _iso_now(runtime.clock),
        "airport": {"icao": record["ident"], "name": record["name"]},
        "window": {"begin_epoch": begin, "end_epoch": end, "minutes": window},
        "movements": results,
        "counts": {which: len(rows) for which, rows in results.items()},
        "bookkeeping": {
            "credits_spent": 4 * len(results),
            "credits_remaining_upstream": runtime.opensky.last_credits_remaining,
        },
        "unknowns": unknowns,
        "source": {
            "name": "OpenSky Network",
            "endpoint": "/flights/arrival, /flights/departure",
            "caveat": "Derived from ADS-B; origin/destination are estimates, not schedules",
        },
    }


@mcp.tool()
@guarded
async def airport_weather(airport: str) -> dict[str, Any]:
    """Return the latest METAR and TAF for an airport, plus impact flags.

    Needs no credentials and spends no OpenSky credits.

    Args:
        airport: ICAO code, e.g. ESGG.
    """
    runtime = get_runtime()
    record = _require_airport(runtime, airport)
    icao = record["ident"]
    metars = await runtime.weather.metar(icao)
    tafs = await runtime.weather.taf(icao)
    flags = fleet_impact_flags(metars)

    unknowns: list[str] = []
    if not metars:
        unknowns.append(
            "No METAR returned. That means NOAA holds no current observation for this "
            "station, not that conditions are good."
        )
    if not tafs:
        unknowns.append("No TAF returned for this station.")

    return {
        "ok": True,
        "as_of": _iso_now(runtime.clock),
        "airport": {"icao": icao, "name": record["name"]},
        "metar": metars,
        "taf": tafs,
        "impact_flags": flags,
        "unknowns": unknowns,
        "source": {
            "name": "AviationWeather.gov (NOAA)",
            "licence": "US Government open data, public domain",
        },
    }


@mcp.tool()
@guarded
async def disruption_triage(airport: str, radius_nm: float = 40.0) -> dict[str, Any]:
    """Assess whether an airport is currently disrupted: weather, traffic, movements.

    This is the composite tool an agent should reach for first. It makes one
    weather call (free), one movements call per direction and one traffic call
    (OpenSky credits), then reports severity together with its reasons and its
    unknowns. Severity is advisory only.

    Args:
        airport: ICAO code, e.g. ESGG.
        radius_nm: Traffic search radius in nautical miles (5-200).
    """
    runtime = get_runtime()
    record = _require_airport(runtime, airport)
    icao = record["ident"]
    degraded: list[str] = []
    unknowns: list[str] = []
    reasons: list[str] = []

    weather_payload = await airport_weather(icao)
    movements_payload = await airport_movements(icao, "both", 60)
    traffic_payload = await airport_live_traffic(icao, radius_nm, 40)

    for name, payload in (
        ("weather", weather_payload),
        ("movements", movements_payload),
        ("traffic", traffic_payload),
    ):
        if not payload.get("ok", False):
            degraded.append(f"{name}:{payload.get('error', {}).get('code', 'unknown')}")
            unknowns.append(f"{name} data unavailable, so severity cannot account for it.")
            continue
        unknowns.extend(payload.get("unknowns", []))

    flags = weather_payload.get("impact_flags", []) if weather_payload.get("ok") else []
    reasons.extend(flags)

    movements = movements_payload.get("movements", {}) if movements_payload.get("ok") else {}
    arrival_count = len(movements.get("arrival", []))
    departure_count = len(movements.get("departure", []))
    movement_total = arrival_count + departure_count

    traffic = traffic_payload.get("counts", {}) if traffic_payload.get("ok") else {}
    aircraft_seen = traffic.get("returned", 0) if traffic else 0
    if aircraft_seen > 25:
        reasons.append(f"{aircraft_seen} aircraft within the search box.")
    # Absence of movements is weak evidence on its own: at 03:00 a quiet airport
    # is normal. It only becomes a reason when aircraft are demonstrably flying
    # nearby and yet nothing is arriving or departing.
    if movement_counts_available(movements_payload) and movement_total == 0 and aircraft_seen > 0:
        reasons.append(
            f"{aircraft_seen} aircraft active nearby but no arrivals or departures recorded."
        )

    severity = _severity(reasons, degraded, movement_counts_available(movements_payload))

    return {
        "ok": True,
        "as_of": _iso_now(runtime.clock),
        "airport": {"icao": icao, "name": record["name"], "municipality": record["municipality"]},
        "assessment": {
            "severity": severity,
            "reasons": reasons,
            "unknowns": unknowns,
            "advisory_only": True,
        },
        "evidence": {
            "weather": weather_payload.get("metar", []) if weather_payload.get("ok") else None,
            "movements_summary": {"arrivals": arrival_count, "departures": departure_count}
            if movement_counts_available(movements_payload)
            else None,
            "traffic_counts": traffic or None,
        },
        "degraded": degraded,
        "bookkeeping": {
            "credits_spent": _credits_from(traffic_payload) + _credits_from(movements_payload),
            "credits_remaining_upstream": runtime.opensky.last_credits_remaining,
        },
        "source": {
            "weather": "AviationWeather.gov (NOAA)",
            "traffic_and_movements": "OpenSky Network",
        },
    }


@mcp.tool()
@guarded
async def upstream_status() -> dict[str, Any]:
    """Report upstream health, credit spend and credential state.

    Use before blaming the data: it separates "no credentials", "rate limited"
    and "quiet airspace".
    """
    runtime = get_runtime()
    return {
        "ok": True,
        "as_of": _iso_now(runtime.clock),
        "credentials": {
            "opensky_configured": runtime.opensky._tokens.has_credentials(),  # noqa: SLF001
            "aviationweather_required": False,
        },
        "budget": {
            "credits_spent_this_hour": runtime.opensky.credits_spent_this_hour,
            "credits_spent_this_process": runtime.opensky.credits_spent_total,
            "local_budget_per_hour": runtime.settings.credit_budget_per_hour,
            "upstream_remaining_last_seen": runtime.opensky.last_credits_remaining,
        },
        "token": {
            "cached": runtime.opensky.token_cached,
            "refreshes_this_process": runtime.opensky._tokens.refresh_count,  # noqa: SLF001
        },
        "reference_data": {
            "airports_db_present": runtime.airports.exists,
            "path": str(runtime.airports.path),
        },
    }


# --- helpers -----------------------------------------------------------------
def movement_counts_available(payload: dict[str, Any]) -> bool:
    if not payload.get("ok"):
        return False
    movements = payload.get("movements") or {}
    return bool(movements) and any(True for _ in movements)


def _credits_from(payload: dict[str, Any]) -> int:
    if not payload.get("ok"):
        return 0
    value = payload.get("bookkeeping", {}).get("credits_spent", 0)
    return value if isinstance(value, int) else 0


def _severity(reasons: list[str], degraded: list[str], has_movements: bool) -> str:
    """A deliberately conservative escalation ladder.

    Unknown outranks elevated: a triage that cannot see the data must not imply
    that operations are fine.
    """
    if not has_movements and degraded:
        return "unknown"
    if len(reasons) >= 3:
        return "high"
    if reasons:
        return "elevated"
    if degraded:
        return "unknown"
    return "low"


def main() -> None:
    """Console entry point: `flightops-mcp [--transport stdio|streamable-http]`."""
    parser = argparse.ArgumentParser(prog="flightops-mcp")
    parser.add_argument(
        "--transport",
        default="stdio",
        choices=("stdio", "streamable-http"),
        help=(
            "stdio for local hosts (Claude Desktop, Inspector); streamable-http to serve remotely."
        ),
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    settings = Settings()
    configure_logging(settings.log_level)
    _log.info("server_starting", transport=args.transport, version="0.1.0")

    if args.transport == "stdio":
        mcp.run(transport="stdio")
    else:  # pragma: no cover - exercised manually, not in CI
        asyncio.run(mcp.run_streamable_http_async(host=args.host, port=args.port))


if __name__ == "__main__":  # pragma: no cover
    main()
