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
import functools
import json
import time
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime
from typing import Any, Final, TypedDict

import httpx
from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.server.request_state import RequestStateSecurity
from mcp.types import (
    ClientCapabilities,
    ElicitRequest,
    ElicitRequestFormParams,
    ElicitResult,
    InputRequiredResult,
)
from mcp_types.version import is_version_at_least
from opentelemetry.trace import SpanKind, Status, StatusCode
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from . import __version__
from .auth import (
    AccessToken,
    AuthConfigError,
    AuthSettings,
    ScopeChallengingServer,
    Scopes,
    TokenVerifier,
    build_resource_server_auth,
    current_access_token,
)
from .auth.approval import (
    APPROVAL_DETAIL_TYPE,
    approval_declined,
    approval_invalid,
    approval_required,
)
from .cache import LIST_CACHE_HINTS, cached
from .config import Settings
from .errors import (
    ToolFailure,
    authentication_required,
    insufficient_scope,
    not_found,
    reference_data_missing,
)
from .opensky import (
    AIRSPACE_REGIONS,
    ANONYMOUS_LIMITATION,
    MAX_HISTORY_S,
    STATES_STALE_AFTER_S,
    TIME_RESOLUTION_S,
    BBox,
    StatesSnapshot,
    credentials_mode,
    daily_metered,
)
from .ops import TIGHT_CONNECTION_MINUTES, DownstreamTokenRejected, normalise_flight, summarise
from .otel import configure_tracing, tracer, tracing_kind
from .rebooking import Proposal, approved_digest, build_proposal
from .resilience import breakers, configure_breakers
from .runtime import Runtime, get_runtime
from .schiphol import DIRECTIONS as SCHIPHOL_DIRECTIONS
from .telemetry import REGISTRY as METRICS_REGISTRY
from .telemetry import TOOL_CALLS, configure_logging, get_logger
from .weather import (
    GUST_FLAG_KT,
    IFR_CATEGORIES,
    VISIBILITY_FLAG_SM,
    Reports,
    ceiling,
    fleet_impact_flags,
)

# METARs are issued at least hourly, half-hourly at most European airports. One
# more than 90 minutes old means at least one routine report is missing, and the
# observation is no longer "conditions now".
METAR_STALE_AFTER_S: Final = 90 * 60

# The parts of a triage that can observe something, in the order they are reported.
EVIDENCE_LEGS: Final = ("weather", "traffic", "movements")
# The triage ladder's thresholds, named once so the ladder and its why-not lines agree.
BUSY_AIRCRAFT: Final = 25
HIGH_REASONS: Final = 3
# One NOAA request per call. A longer `ids` list risks a URL some proxy truncates.
WEATHER_OVERVIEW_MAX: Final = 100
WEATHER_SOURCE: Final = {
    "name": "AviationWeather.gov (NOAA)",
    "licence": "US Government open data, public domain",
}

# What `airspace_snapshot` returns per aircraft, in order. Columns, because a repeated
# key per row is most of the size of a wide answer.
AIRSPACE_COLUMNS: Final = (
    "icao24",
    "callsign",
    "latitude",
    "longitude",
    "baro_altitude_m",
    "on_ground",
    "velocity_ms",
    "true_track_deg",
    "last_contact",
)
AIRSPACE_MAX_AIRCRAFT: Final = 10_000
OPENSKY_LICENCE: Final = (
    "Free for personal and non-profit use; commercial use needs OpenSky's consent"
)

INSTRUCTIONS = """\
Use this server for questions about the operational state of an airport and the \
traffic around it.

Rules that the tools will enforce:

1. Resolve identifiers first. `resolve_airport` is offline and free; it returns \
ICAO (ident), IATA, name and coordinates. Never guess a coordinate.
2. `airport_live_traffic`, `airport_movements`, `airspace_snapshot` and \
`disruption_triage` spend OpenSky API credits. Prefer one `disruption_triage` call \
over three separate ones.
3. `airport_weather` needs no credentials and no credits.
4. Absence of data is not evidence. Each payload lists `unknowns`; if it is \
non-empty, say so instead of concluding that operations are normal.
5. This server is advisory. It reports observations and flags; it never \
authorises an operational decision.
6. `flight_passenger_impact` reads the internal operations API as the signed-in \
user and returns counts only. Never ask it for, or infer, an individual passenger.
7. `propose_rebooking` is the only tool that changes anything, and it cannot \
complete on your say-so. It answers with a request for a human approval; a person \
other than the user you act for must approve the exact proposal. If the approval \
is refused, missing or declined, report that and stop. Never try to supply, guess \
or work around an approval.
8. `schiphol_flights` reads Amsterdam Schiphol's published schedule. It is a \
schedule, not an observation: compare it with `airport_movements` before saying \
a flight has operated.
9. A payload with `cache.hit: true` was served from this server's cache. Its \
`upstream_time_epoch` is the upstream's clock when the data was fetched; a repeat \
inside the TTL costs no credits.
10. Ages are measured against `upstream_time_epoch`, the upstream's own `Date` \
header. When it is null the age is unknown, and the payload says so in `unknowns`.
"""

_log = get_logger(__name__)

# A tool answers with a payload, or with the protocol's request for more input.
ToolResult = dict[str, Any] | InputRequiredResult
ToolFn = Callable[..., Awaitable[ToolResult]]

# The first revision that can answer `tools/call` with `input_required`.
MRTR_REVISION: Final = "2026-07-28"
# The key of the one question `propose_rebooking` asks, and of its answer.
APPROVAL_REQUEST: Final = "approval"

# Tools register here rather than on a server object, because there are two
# servers: the unauthenticated stdio one and the authenticated HTTP one. The SDK
# takes auth settings at construction, so one module-level instance cannot be both.
_TOOLS: list[ToolFn] = []


def guarded(fn: Any) -> Any:
    """Convert expected failures into payloads; keep the tool signature intact.

    `functools.wraps` matters more than it looks: the MCP SDK derives the JSON
    schema for a tool from the decorated function's signature, so a `*args,
    **kwargs` wrapper would publish an empty, useless schema.
    """

    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> ToolResult:
        # One span per tool body, parented to whatever the transport made current:
        # the request middleware span on an inbound call, or the composite tool's
        # span when one tool calls another. That is what makes a triage show its
        # three nested lookups instead of one opaque block.
        with tracer().start_as_current_span(
            f"tool.{fn.__name__}",
            kind=SpanKind.INTERNAL,
            attributes={"mcp.tool.name": fn.__name__},
        ) as span:
            try:
                result: ToolResult = await fn(*args, **kwargs)
            except ToolFailure as exc:
                TOOL_CALLS.labels(tool=fn.__name__, outcome=exc.code).inc()
                _log.info("tool_failure", tool=fn.__name__, code=exc.code)
                span.set_attribute("mcp.tool.outcome", exc.code)
                span.set_attribute("mcp.tool.ok", False)
                span.set_status(Status(StatusCode.ERROR, exc.code))
                return exc.to_payload()
            except Exception as exc:  # noqa: BLE001 - a tool must never crash the session
                TOOL_CALLS.labels(tool=fn.__name__, outcome="internal_error").inc()
                _log.exception(
                    "tool_internal_error", tool=fn.__name__, error_class=type(exc).__name__
                )
                span.record_exception(exc)
                span.set_attribute("mcp.tool.outcome", "internal_error")
                span.set_status(Status(StatusCode.ERROR, type(exc).__name__))
                return ToolFailure(
                    code="internal_error",
                    message="The tool hit an unexpected internal error and did not complete.",
                    hint=(
                        "Report this rather than retrying blindly; the server log holds the detail."
                    ),
                ).to_payload()
            if isinstance(result, InputRequiredResult):
                # Neither success nor failure: the tool stopped to ask, and did nothing.
                TOOL_CALLS.labels(tool=fn.__name__, outcome="input_required").inc()
                span.set_attribute("mcp.tool.outcome", "input_required")
                return result
            TOOL_CALLS.labels(tool=fn.__name__, outcome="ok").inc()
            span.set_attribute("mcp.tool.outcome", "ok")
            span.set_attribute("mcp.tool.ok", bool(result.get("ok", True)))
            return result

    return wrapper


def tool(fn: Any) -> Any:
    """Guard a tool function and add it to the registry every server is built from.

    Registration fails for a tool with no entry in the scope table, so a new tool
    cannot ship unprotected by omission, and likewise for the cache hint table, so
    a new tool is never cached (or left uncached) by default.

    The cache sits inside `guarded` and, on the HTTP server, inside `scoped`: the
    scope is checked before the cache is consulted, every time.
    """
    Scopes.for_tool(fn.__name__)
    wrapped: ToolFn = guarded(cached(fn, cache=lambda: get_runtime().cache))
    _TOOLS.append(wrapped)
    return wrapped


def scoped(fn: ToolFn) -> ToolFn:
    """Require the tool's scopes on the caller's token. Only the HTTP server uses this.

    The HTTP middleware has normally refused the request already; this is the
    second check, next to the tool, for any request shape that got past it. It
    fails closed: no verified token in context is a refusal, not a pass, so a
    wiring mistake cannot quietly turn authorization off.
    """
    name: str = fn.__name__

    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> ToolResult:
        token = current_access_token()
        if token is None:
            failure = authentication_required(name)
        else:
            missing = Scopes.missing(name, token.scopes)
            if not missing:
                return await fn(*args, **kwargs)
            failure = insufficient_scope(name, Scopes.for_tool(name), missing)
        TOOL_CALLS.labels(tool=name, outcome=failure.code).inc()
        _log.warning("tool_refused", tool=name, code=failure.code, details=failure.details)
        return failure.to_payload()

    return wrapper


def _iso_now(clock: Any) -> str:
    return datetime.fromtimestamp(clock(), tz=UTC).isoformat()


def _epoch(value: object) -> float | None:
    """An upstream timestamp as epoch seconds: NOAA sends ISO strings, OpenSky integers."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.strip())
        except ValueError:
            return None
        return (parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)).timestamp()
    return None


def _age(seconds: float) -> str:
    minutes = int(seconds // 60)
    return f"{minutes} min" if minutes < 120 else f"{minutes // 60} h {minutes % 60} min"


def _newest(*epochs: object) -> int | None:
    """The latest of several upstream times, ignoring the ones that are missing."""
    known = [e for e in epochs if isinstance(e, int) and not isinstance(e, bool) and e > 0]
    return max(known) if known else None


def _snapshot_unknowns(snapshot: StatesSnapshot, where: str) -> tuple[list[str], bool]:
    """What a traffic snapshot cannot show, and whether it describes the present.

    "The present" is OpenSky's clock when it answered, from its `Date` header. This
    machine's clock is not used: a host running minutes fast made every snapshot stale.
    """
    now = snapshot.upstream_time
    unknowns: list[str] = []
    if not snapshot.vectors:
        unknowns.append(
            f"No aircraft heard in {where}. That can mean quiet airspace, or a gap in "
            "receiver coverage; it is not evidence that the airport is closed."
        )
    if snapshot.unreadable:
        unknowns.append(
            f"{snapshot.unreadable} of {snapshot.received} rows in OpenSky's answer were not "
            "readable state vectors. The counts cover only the readable ones, so they are a "
            "lower bound, not the number of aircraft."
        )
    current = False
    if snapshot.time is None or snapshot.time <= 0:
        if snapshot.vectors:
            unknowns.append(
                "OpenSky's answer carries no snapshot time, so how current these positions "
                "are is unknown."
            )
    elif now is None:
        if snapshot.vectors:
            unknowns.append(
                "OpenSky's response carried no Date header, so how old this snapshot is "
                "cannot be judged. This server does not substitute its own clock."
            )
    elif now - snapshot.time > STATES_STALE_AFTER_S:
        unknowns.append(
            f"The snapshot is {_age(now - snapshot.time)} old. These positions are not where "
            "the aircraft are now."
        )
    elif snapshot.time - now > STATES_STALE_AFTER_S:
        unknowns.append(
            f"The snapshot is dated {_age(snapshot.time - now)} in the future, so how current "
            "these positions are is unknown."
        )
    else:
        current = True
    return unknowns, current


def _require_airport(runtime: Runtime, ident: str) -> dict[str, Any]:
    if not runtime.airports.exists:
        raise reference_data_missing(str(runtime.airports.path))
    airport = runtime.airports.get(ident)
    if airport is None:
        raise not_found("airport", ident)
    return airport


# --- Tools -------------------------------------------------------------------
@tool
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


@tool
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

    unknowns, snapshot_current = _snapshot_unknowns(snapshot, "this box")
    if runtime.opensky.credentials_mode == "anonymous":
        unknowns.append(ANONYMOUS_LIMITATION)
    return {
        "ok": True,
        "upstream_time_epoch": snapshot.upstream_time,
        "credentials": runtime.opensky.credentials_mode,
        "freshness": runtime.opensky.freshness(),
        "snapshot_time_epoch": snapshot.time,
        "snapshot_current": snapshot_current,
        "airport": {"icao": record["ident"], "name": record["name"]},
        "search": {
            "radius_nm": radius,
            "bbox": bbox.as_params(),
            "bbox_area_sq_deg": round(bbox.area_sq_deg, 2),
        },
        "counts": {
            "returned": len(snapshot.vectors),
            "airborne": airborne,
            "on_ground": on_ground,
            # What OpenSky sent, and how much of it was not a readable state vector.
            # `returned` is lower than `received - unreadable` only when capped.
            "received": snapshot.received,
            "unreadable": snapshot.unreadable,
        },
        "aircraft": [vector.to_payload() for vector in snapshot.vectors],
        "bookkeeping": {
            "credits_spent": snapshot.credits_spent,
            "credits_remaining_upstream": snapshot.credits_remaining,
            "truncated": snapshot.truncated,
            "notes": snapshot.notes,
        },
        "unknowns": unknowns,
        "source": {
            "name": "OpenSky Network",
            "endpoint": "/states/all",
            "licence": OPENSKY_LICENCE,
        },
    }


@tool
async def airspace_snapshot(region: str, max_aircraft: int = 500) -> dict[str, Any]:
    """Return aircraft across a whole region, as compact columns, for an overview.

    Costs 4 OpenSky credits per uncached call, for every region: each one is in
    OpenSky's top price tier. A repeat within 60 seconds is served from the cache and
    costs nothing. Also counts against the daily credit guard. For one airport use
    `airport_live_traffic`, which costs 1 credit.

    Args:
        region: "europe", "north_america", "south_america", "africa", "asia", "oceania"
            or "world". Free-form boxes are not accepted.
        max_aircraft: Cap on returned aircraft (1-10000), most recently heard first.
    """
    runtime = get_runtime()
    wanted = region.lower().strip()
    bbox = AIRSPACE_REGIONS.get(wanted)
    if bbox is None:
        raise ToolFailure(
            code="invalid_argument",
            message=f"region must be one of: {', '.join(AIRSPACE_REGIONS)}",
            hint="Regions are fixed. For the area around one airport use airport_live_traffic.",
        )
    with daily_metered():
        snapshot = await runtime.opensky.states_in_bbox(
            bbox, max_vectors=max(1, min(AIRSPACE_MAX_AIRCRAFT, max_aircraft))
        )

    placed = [v for v in snapshot.vectors if v.latitude is not None and v.longitude is not None]
    unknowns, current = _snapshot_unknowns(snapshot, "this region")
    if runtime.opensky.credentials_mode == "anonymous":
        unknowns.append(ANONYMOUS_LIMITATION)
    if snapshot.truncated:
        unknowns.append(
            f"Only the {len(snapshot.vectors)} most recently heard aircraft are included. "
            "More were heard, so this is not the whole picture."
        )
    if len(placed) < len(snapshot.vectors):
        unknowns.append(
            f"{len(snapshot.vectors) - len(placed)} aircraft had no position and are not listed."
        )
    return {
        "ok": True,
        "upstream_time_epoch": snapshot.upstream_time,
        "region": wanted,
        "credentials": runtime.opensky.credentials_mode,
        "freshness": runtime.opensky.freshness(),
        "snapshot_time_epoch": snapshot.time,
        "snapshot_current": current,
        "columns": list(AIRSPACE_COLUMNS),
        "rows": [
            [
                v.icao24,
                v.callsign,
                v.latitude,
                v.longitude,
                v.baro_altitude_m,
                v.on_ground,
                v.velocity_ms,
                v.true_track_deg,
                v.last_contact,
            ]
            for v in placed
        ],
        "counts": {"received": snapshot.received, "returned": len(placed)},
        "truncated": snapshot.truncated,
        "bookkeeping": {
            "credits_spent": snapshot.credits_spent,
            "credits_remaining_upstream": snapshot.credits_remaining,
            "spent_with": runtime.opensky.credentials_mode,
        },
        "unknowns": unknowns,
        "source": {
            "name": "OpenSky Network",
            "endpoint": "/states/all",
            "licence": OPENSKY_LICENCE,
        },
    }


@tool
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
    # The window is a request parameter, asked for before any upstream has answered,
    # so it is the one thing here that a local clock decides.
    end = int(runtime.data_clock())
    end -= end % TIME_RESOLUTION_S
    begin = end - window * 60

    directions = ["arrival", "departure"] if wanted == "both" else [wanted]
    results: dict[str, list[dict[str, Any]]] = {}
    received: dict[str, int] = {}
    missing: list[str] = []
    unknowns: list[str] = []
    upstream_time: int | None = None
    for which in directions:
        try:
            page = await runtime.opensky.movements(record["ident"], which, begin=begin, end=end)
        except ToolFailure as exc:
            if exc.code == "credentials_missing":
                missing.append(which)
                continue
            raise
        results[which] = [movement.to_payload() for movement in page.movements]
        received[which] = page.received
        upstream_time = _newest(upstream_time, page.upstream_time)
        if page.unreadable:
            unknowns.append(
                f"{page.unreadable} of {page.received} {which} rows in OpenSky's answer were "
                "not readable flights. The count covers only the readable ones."
            )

    for which in missing:
        unknowns.append(f"{which} data unavailable: OpenSky credentials are not configured.")
    if not missing and all(not rows for rows in results.values()):
        unknowns.append(
            "No movements recorded in this window. OpenSky only sees ADS-B-equipped "
            "aircraft within receiver coverage, so a quiet result is not proof of no traffic."
        )

    return {
        "ok": True,
        # None when nothing was fetched, or when no answer carried a `Date`.
        "upstream_time_epoch": upstream_time,
        "credentials": runtime.opensky.credentials_mode,
        "airport": {"icao": record["ident"], "name": record["name"]},
        "window": {"begin_epoch": begin, "end_epoch": end, "minutes": window},
        "movements": results,
        "counts": {which: len(rows) for which, rows in results.items()},
        # Rows OpenSky sent per direction, readable or not. `counts` holds the readable ones.
        "received": received,
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


@tool
async def airport_weather(airport: str) -> dict[str, Any]:
    """Return the latest METAR and TAF for an airport, plus impact flags.

    Needs no credentials and spends no OpenSky credits.

    Args:
        airport: ICAO code, e.g. ESGG.
    """
    runtime = get_runtime()
    record = _require_airport(runtime, airport)
    icao = record["ident"]
    metar_reports = await runtime.weather.metar_reports(icao)
    metars = metar_reports.items
    tafs = (await runtime.weather.taf_reports(icao)).items
    # NOAA's clock when it sent the METARs. Their age is measured against it.
    upstream_time = metar_reports.upstream_time
    age, unknowns = _metar_age(metars, upstream_time)
    if not tafs:
        # After "no METAR" when there is one, before any statement about the METAR's age.
        unknowns.insert(0 if metars else 1, "No TAF returned for this station.")

    return {
        "ok": True,
        "upstream_time_epoch": upstream_time,
        "airport": {"icao": icao, "name": record["name"]},
        "metar": metars,
        "taf": tafs,
        "latest_metar": _latest_metar(age),
        "impact_flags": fleet_impact_flags(metars),
        "unknowns": unknowns,
        "source": WEATHER_SOURCE,
    }


@tool
async def weather_overview(airports: list[str]) -> dict[str, Any]:
    """Return the latest METAR for many airports at once, for an overview or a map.

    Needs no credentials and spends no OpenSky credits. One NOAA request covers up to
    100 airports. No TAF: for one airport's full picture use `airport_weather`.

    Args:
        airports: ICAO codes, 1-100, e.g. ["ESGG", "EKCH"].
    """
    runtime = get_runtime()
    if not runtime.airports.exists:
        raise reference_data_missing(str(runtime.airports.path))
    wanted = list(dict.fromkeys(code.strip().upper() for code in airports if code.strip()))
    if not wanted or len(wanted) > WEATHER_OVERVIEW_MAX:
        raise ToolFailure(
            code="invalid_argument",
            message=f"airports must list 1 to {WEATHER_OVERVIEW_MAX} ICAO codes",
            hint="Split a longer list into several calls.",
        )
    listed = [code for code in wanted if runtime.airports.get(code) is not None]
    not_listed = [code for code in wanted if code not in listed]
    reports = await runtime.weather.metar_reports_many(listed) if listed else Reports()
    by_station: dict[str, list[dict[str, Any]]] = {}
    for metar in reports.items:
        by_station.setdefault(str(metar.get("station_id") or "").upper(), []).append(metar)

    entries: dict[str, Any] = {}
    for code in listed:
        metars = by_station.get(code, [])
        age, unknowns = _metar_age(metars, reports.upstream_time)
        entries[code] = {
            "ok": True,
            "upstream_time_epoch": reports.upstream_time,
            "metar": metars,
            "latest_metar": _latest_metar(age),
            "impact_flags": fleet_impact_flags(metars),
            "unknowns": unknowns,
        }
    return {
        "ok": True,
        "upstream_time_epoch": reports.upstream_time,
        "airports": entries,
        "not_in_reference_data": not_listed,
        "counts": {
            "asked": len(wanted),
            "with_metar": sum(1 for entry in entries.values() if entry["metar"]),
            "current": sum(1 for entry in entries.values() if entry["latest_metar"]["current"]),
        },
        "unknowns": [
            f"{code} is not in the reference data, so it was not asked about."
            for code in not_listed
        ],
        "source": WEATHER_SOURCE,
    }


def _latest_metar(age: float | None) -> dict[str, Any]:
    """Whether the newest METAR describes conditions now.

    A METAR that is old, dated ahead of the clock, or whose time cannot be read is
    shown but proves nothing about the present. NOAA's report time can sit a few
    minutes ahead of an observation, so "ahead" gets the same tolerance as "old".
    """
    return {
        "age_s": None if age is None else round(age),
        "current": age is not None and abs(age) <= METAR_STALE_AFTER_S,
    }


def _metar_age(
    metars: list[dict[str, Any]], upstream_time: int | None
) -> tuple[float | None, list[str]]:
    """The newest METAR's age by NOAA's clock, and what makes it unknowable or not current."""
    if not metars:
        return None, [
            "No METAR returned. That means NOAA holds no current observation for this "
            "station, not that conditions are good."
        ]
    observed = [t for t in (_epoch(m.get("observation_time")) for m in metars) if t]
    if not observed:
        return None, [
            "The METAR carries no readable observation time, so whether it describes "
            "conditions now is unknown."
        ]
    if upstream_time is None:
        return None, [
            "NOAA's response carried no Date header, so how old the METAR is cannot be "
            "judged. This server does not substitute its own clock."
        ]
    age = upstream_time - max(observed)
    if age > METAR_STALE_AFTER_S:
        return age, [
            f"The latest METAR is {_age(age)} old. It is not current conditions, and "
            "its impact flags describe the weather then, not now."
        ]
    if age < -METAR_STALE_AFTER_S:
        return age, [
            f"The latest METAR is dated {_age(-age)} in the future, so whether it "
            "describes conditions now is unknown."
        ]
    return age, []


@tool
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

    mode = runtime.opensky.credentials_mode
    if (
        mode == "anonymous"
        and movements_payload.get("ok")
        and not movement_counts_available(movements_payload)
    ):
        # Anonymous traffic can succeed where movements are not fetched, and a triage
        # missing a whole leg must not read as a complete one.
        degraded.append("movements:not_fetched_anonymously")

    # Each observation that raises severity, with the leg it came from. Two METARs in
    # the window that both say IFR are one reason seen twice, not two reasons.
    observed: list[tuple[str, str]] = []
    if weather_payload.get("ok"):
        observed.extend(("weather", flag) for flag in weather_payload.get("impact_flags", []))

    movements = movements_payload.get("movements", {}) if movements_payload.get("ok") else {}
    arrival_count = len(movements.get("arrival", []))
    departure_count = len(movements.get("departure", []))
    movement_total = arrival_count + departure_count

    traffic = traffic_payload.get("counts", {}) if traffic_payload.get("ok") else {}
    aircraft_seen = traffic.get("returned", 0) if traffic else 0
    if aircraft_seen > BUSY_AIRCRAFT:
        observed.append(("traffic", f"{aircraft_seen} aircraft within the search box."))
    # Absence of movements is weak evidence on its own: at 03:00 a quiet airport
    # is normal. It only becomes a reason when aircraft are demonstrably flying
    # nearby and yet nothing is arriving or departing.
    if movement_counts_available(movements_payload) and movement_total == 0 and aircraft_seen > 0:
        observed.append(
            (
                "movements",
                f"{aircraft_seen} aircraft active nearby but no arrivals or departures recorded.",
            )
        )
    findings = _distinct_reasons(observed, len(weather_payload.get("metar") or []))
    reasons.extend(finding["reason"] for finding in findings)

    silent = _legs_without_evidence(weather_payload, traffic_payload, movements_payload)
    if len(silent) == len(EVIDENCE_LEGS):
        unknowns.append(
            f"No positive evidence from any source ({', '.join(silent)}). Nothing here shows "
            "that operations are normal."
        )
    has_movements = movement_counts_available(movements_payload)
    severity = _severity(
        reasons, degraded, has_movements, has_evidence=len(silent) < len(EVIDENCE_LEGS)
    )
    weather = _weather_values(weather_payload)
    counts = _triage_counts(movements_payload, traffic_payload, radius_nm)
    why_higher, why_lower = _why_not(
        severity,
        reasons=reasons,
        degraded=degraded,
        silent=silent,
        weather=weather,
        counts=counts,
    )

    return {
        "ok": True,
        # The newest of the legs' upstream times. Each leg judged its own age by its own.
        "upstream_time_epoch": _newest(
            *(
                leg.get("upstream_time_epoch")
                for leg in (weather_payload, movements_payload, traffic_payload)
            )
        ),
        "credentials": mode,
        "airport": {"icao": icao, "name": record["name"], "municipality": record["municipality"]},
        "assessment": {
            "severity": severity,
            "reasons": reasons,
            # The same reasons with the leg each came from and how many observations
            # (METARs, snapshots, windows) said it.
            "reason_details": findings,
            # The evidence for the next rung up, and for standing down a rung.
            "why_not_higher": why_higher,
            "why_not_lower": why_lower,
            # Several legs can say the same thing; saying it once is enough.
            "unknowns": list(dict.fromkeys(unknowns)),
            "legs_without_evidence": silent,
            "advisory_only": True,
        },
        "counts": counts,
        "weather": weather,
        "legs": {
            "weather": _weather_leg(weather_payload),
            "traffic": _traffic_leg(traffic_payload),
            "movements": _movements_leg(movements_payload, degraded),
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


@tool
async def schiphol_flights(direction: str = "both", window_minutes: int = 120) -> dict[str, Any]:
    """Amsterdam Schiphol's published schedule: flights due in the next window, with state.

    Reads the Schiphol Public Flight API (v4) as this server, with its own OAuth2
    client. EHAM only. A schedule is what the airport has published, not what has
    happened: a flight listed here may not have operated. Counts cancelled,
    diverted and delayed flights (delayed: state DEL, or an estimate 15 min or more
    after schedule). One request per page of 20 flights, charged to a local
    per-hour request budget; no OpenSky credits.

    This adapter has been exercised only against a mock of Schiphol's documented
    API, never with a live key.

    Args:
        direction: "arrival", "departure", or "both".
        window_minutes: How far ahead of now to look, in minutes (15-720).
    """
    runtime = get_runtime()
    wanted = direction.lower().strip()
    if wanted not in {*SCHIPHOL_DIRECTIONS, "both"}:
        raise ToolFailure(
            code="invalid_argument",
            message="direction must be 'arrival', 'departure' or 'both'",
            hint="Use 'both' if you are unsure; it costs twice the requests of one direction.",
        )
    window = max(15, min(720, window_minutes))
    # A request parameter, asked for before Schiphol has answered: a local clock.
    begin = runtime.data_clock()
    end = begin + window * 60
    directions = ["arrival", "departure"] if wanted == "both" else [wanted]

    flights: dict[str, list[dict[str, Any]]] = {}
    counts: dict[str, dict[str, int]] = {}
    unknowns: list[str] = []
    requests = 0
    upstream_time: int | None = None
    for which in directions:
        schedule = await runtime.schiphol.schedule(which, begin=begin, end=end)
        requests += schedule.requests
        upstream_time = _newest(upstream_time, schedule.upstream_time)
        flights[which] = [flight.to_payload() for flight in schedule.flights]
        counts[which] = {
            "listed": len(schedule.flights),
            "cancelled": sum(1 for f in schedule.flights if f.cancelled),
            "diverted": sum(1 for f in schedule.flights if f.diverted),
            "delayed": sum(1 for f in schedule.flights if f.delayed),
        }
        if schedule.unreadable:
            unknowns.append(
                f"{schedule.unreadable} of {schedule.received} {which} rows in Schiphol's answer "
                "were not readable flights. The counts cover only the readable ones."
            )
        if schedule.truncated:
            unknowns.append(
                f"More {which} pages exist than the {runtime.settings.schiphol_max_pages} "
                "fetched. The counts are a lower bound for the window."
            )
        if not schedule.flights and not schedule.unreadable:
            unknowns.append(
                f"No {which}s listed in this window. That can be a quiet period or a gap in "
                "the published schedule; it is not evidence that nothing is moving."
            )

    return {
        "ok": True,
        "upstream_time_epoch": upstream_time,
        "airport": {"icao": "EHAM", "iata": "AMS", "name": "Amsterdam Airport Schiphol"},
        "window": {"begin_epoch": int(begin), "end_epoch": int(end), "minutes": window},
        "counts": counts,
        "flights": flights,
        "bookkeeping": {
            "credits_spent": requests,
            "unit": "requests",
            "requests_this_hour": runtime.schiphol.requests_this_hour,
            "local_budget_per_hour": runtime.schiphol.budget_per_hour,
        },
        "unknowns": unknowns,
        "source": {
            "name": "Schiphol Public Flight API",
            "endpoint": "/public-flights/v4/flights",
            "caveat": "Published schedule and states, not observed movements",
            "verification": "Adapter built to the vendor's documented shape and tested "
            "against a mock of it; not yet run against the live API.",
        },
    }


@tool
async def upstream_status() -> dict[str, Any]:
    """Report upstream health, credit spend and credential state.

    Use before blaming the data: it separates "no credentials", "rate limited"
    and "quiet airspace".
    """
    runtime = get_runtime()
    cache_requests = runtime.cache.hits + runtime.cache.misses
    return {
        "ok": True,
        "credentials": {
            "opensky": runtime.opensky.credentials_mode,
            "opensky_configured": runtime.opensky._tokens.has_credentials(),  # noqa: SLF001
            "schiphol_configured": runtime.schiphol.configured,
            "aviationweather_required": False,
        },
        "budget": {
            "credits_spent_this_hour": runtime.opensky.credits_spent_this_hour,
            "credits_spent_this_process": runtime.opensky.credits_spent_total,
            "credits_spent_by_mode": dict(runtime.opensky.credits_spent_by_mode),
            "dashboard_credits_spent_24h": runtime.opensky.credits_spent_24h,
            "dashboard_budget_per_24h": runtime.opensky.daily_budget,
            "daily_ledger_survives_restart": False,
            "local_budget_per_hour": runtime.settings.credit_budget_per_hour,
            "upstream_remaining_last_seen": runtime.opensky.last_credits_remaining,
            "schiphol_requests_this_hour": runtime.schiphol.requests_this_hour,
            "schiphol_local_budget_per_hour": runtime.schiphol.budget_per_hour,
        },
        "cache": {
            "enabled": runtime.settings.cache_enabled,
            "entries": len(runtime.cache),
            "hits": runtime.cache.hits,
            "misses": runtime.cache.misses,
            "hit_rate": (round(runtime.cache.hits / cache_requests, 3) if cache_requests else None),
        },
        "token": {
            "cached": runtime.opensky.token_cached,
            "refreshes_this_process": runtime.opensky._tokens.refresh_count,  # noqa: SLF001
        },
        "reference_data": {
            "airports_db_present": runtime.airports.exists,
            "airports_db_ready": runtime.airports.healthy(),
            "path": str(runtime.airports.path),
        },
        "circuits": breakers().snapshots(),
        "tracing": tracing_kind() or "none",
    }


@tool
async def flight_passenger_impact(flight: str) -> dict[str, Any]:
    """Count who a disrupted flight affects: booked, connecting, needing assistance.

    Reads the internal operations API on behalf of the signed-in user, so it
    needs a user's token carrying `flightops.passengers.read`; a service
    principal cannot use it and it is unavailable over stdio. Spends no OpenSky
    credits. Returns counts only: no names, contact details or booking
    references leave the server, so do not ask it about an individual.

    Args:
        flight: Flight designator, e.g. SK1429.
    """
    runtime = get_runtime()
    designator = normalise_flight(flight)
    inbound = current_access_token()
    if inbound is None:
        raise ToolFailure(
            code="user_token_required",
            message="Passenger data is only read on behalf of a signed-in user.",
            hint="Call this tool through the authenticated HTTP server with a user's token. "
            "It is not available over stdio.",
        )

    audience = runtime.settings.ops_api_audience
    scopes = [Scopes.PASSENGERS_READ]
    rows: list[dict[str, Any]] = await _on_behalf_of(
        runtime, inbound, scopes, lambda token: runtime.ops.manifest(designator, token)
    )

    return {
        "ok": True,
        "as_of": _iso_now(runtime.clock),
        "flight": designator,
        "passengers": summarise(rows),
        "definitions": {
            "tight_connections": f"connecting with under {TIGHT_CONNECTION_MINUTES} minutes",
        },
        "unknowns": []
        if rows
        else [
            "No passengers on file for this flight. That can mean a positioning or cargo "
            "flight, or a manifest that is not loaded yet; it is not evidence that nobody "
            "is affected."
        ],
        "delegation": {
            "flow": "on-behalf-of (RFC 8693 token exchange)",
            "acting_for": "the signed-in user",
            "downstream_audience": audience,
            "downstream_scope": scopes,
        },
        "source": {
            "name": "Internal operations API",
            "data_minimisation": "Counts only. Passenger-identifying fields are dropped "
            "before this payload is built and are never logged.",
        },
    }


@tool
async def propose_rebooking(
    *,
    from_flight: str,
    flight_date: str,
    to_flight: str,
    cohort: str,
    reason_code: str,
    passenger_count: int,
    ctx: Context[Any, Any],
) -> dict[str, Any] | InputRequiredResult:
    """Propose moving a group of passengers to another flight. Needs a human approval.

    The only tool that changes anything. It does not complete on the first call:
    it answers with a request for approval, and executes only when a person other
    than the signed-in user approves this exact proposal. You cannot approve it,
    and changing any argument makes it a new proposal needing a new approval.
    Calling it again with the same arguments is safe: the same proposal is
    executed at most once and a repeat returns the original result.

    It names a group, never a passenger, and returns counts only. It needs a
    user's token carrying `flightops.rebooking.write`, so it is unavailable to a
    service principal and over stdio. Spends no OpenSky credits.

    Args:
        from_flight: Flight the passengers are booked on, e.g. SK1429.
        flight_date: Departure date of from_flight, YYYY-MM-DD.
        to_flight: Flight to move them to, e.g. SK1431.
        cohort: Which passengers: "tight_connections", "connecting",
            "needing_assistance" or "all".
        reason_code: Why: "weather", "technical", "crew", "atc", "late_inbound"
            or "other".
        passenger_count: How many passengers this moves, as reported by
            flight_passenger_impact for that cohort. The rebooking is refused if
            the number has changed since.
    """
    runtime = get_runtime()
    proposal = build_proposal(
        from_flight=from_flight,
        flight_date=flight_date,
        to_flight=to_flight,
        cohort=cohort,
        reason_code=reason_code,
        passenger_count=passenger_count,
    )
    inbound = current_access_token()
    if inbound is None:
        raise ToolFailure(
            code="user_token_required",
            message="A rebooking is only proposed on behalf of a signed-in user.",
            hint="Call this tool through the authenticated HTTP server with a user's token. "
            "It is not available over stdio.",
        )
    proposer = inbound.subject or inbound.client_id
    audience = runtime.settings.ops_api_audience
    scopes = [Scopes.REBOOKING_WRITE]
    # Exchanged before anyone is asked to approve: a caller who could never
    # execute the rebooking must find that out without spending a person's time.
    await runtime.obo.token_for(inbound, audience=audience, scopes=scopes)

    answer = (ctx.input_responses or {}).get(APPROVAL_REQUEST)
    if answer is None:
        if ctx.request_state is not None:
            raise approval_required("The request carried no approval response.")
        return _ask_for_approval(ctx, runtime, proposal, proposer)

    _check_proposal_state(ctx.request_state, proposal)
    if not isinstance(answer, ElicitResult):
        raise approval_invalid()
    if answer.action != "accept":
        _log.info(
            "rebooking_declined",
            proposed_by=proposer,
            proposal_sha256=proposal.sha256,
            action=answer.action,
        )
        raise approval_declined(answer.action)
    grant = (answer.content or {}).get("approval_token")
    if not isinstance(grant, str) or not grant:
        raise approval_required("The approval response carried no approval grant.")
    approval = await runtime.approvals.verify(
        grant, proposal_sha256=proposal.sha256, proposer=proposer, digest=approved_digest
    )

    # Only now, with a verified approval, is the operations API called at all.
    body = {
        "from_flight": proposal.from_flight,
        "flight_date": proposal.flight_date,
        "to_flight": proposal.to_flight,
        "cohort": proposal.cohort,
        "reason_code": proposal.reason_code,
        "expected_passengers": proposal.passenger_count,
        "approved_by": approval.approved_by,
        "proposal_sha256": proposal.sha256,
    }
    executed: dict[str, Any] = await _on_behalf_of(
        runtime,
        inbound,
        scopes,
        lambda token: runtime.ops.rebook(
            body, idempotency_key=proposal.idempotency_key, token=token
        ),
    )
    replayed = bool(executed.pop("replayed"))
    approved_at = datetime.fromtimestamp(approval.approved_at, tz=UTC).isoformat()
    # The audit record: who approved, what was proposed, what executed. Every
    # field is a closed vocabulary, a count, a digest or an opaque subject id.
    _log.info(
        "rebooking_audit",
        proposed_by=proposer,
        approved_by=approval.approved_by,
        approved_at=approved_at,
        proposal=proposal.as_dict(),
        proposal_sha256=proposal.sha256,
        idempotency_key=proposal.idempotency_key,
        rebooking_id=executed["rebooking_id"],
        passengers_rebooked=executed["passengers_rebooked"],
        replayed=replayed,
    )
    return {
        "ok": True,
        "as_of": _iso_now(runtime.clock),
        # Identical for the original call and for every replay of it.
        "result": executed,
        "idempotency": {"key": proposal.idempotency_key, "replayed": replayed},
        "audit": {
            "proposed_by": proposer,
            "proposed": proposal.as_dict(),
            "proposal_sha256": proposal.sha256,
            "approved_by": approval.approved_by,
            "approved_at": approved_at,
            "executed": {
                "rebooking_id": executed["rebooking_id"],
                "passengers_rebooked": executed["passengers_rebooked"],
            },
        },
        "unknowns": [],
        "delegation": {
            "flow": "on-behalf-of (RFC 8693 token exchange)",
            "acting_for": "the signed-in user",
            "downstream_audience": audience,
            "downstream_scope": scopes,
        },
        "source": {
            "name": "Internal operations API",
            "data_minimisation": "Counts only. The passengers moved are not returned, "
            "logged or kept.",
        },
    }


# --- the approval round trip ---------------------------------------------------
def _ask_for_approval(
    ctx: Context[Any, Any], runtime: Runtime, proposal: Proposal, proposer: str
) -> InputRequiredResult:
    """Answer a proposal with the question a human must answer. Writes nothing."""
    version = ctx.protocol_version
    if version is None or not is_version_at_least(version, MRTR_REVISION):
        raise approval_required(
            f"This connection predates MCP {MRTR_REVISION} and cannot carry an approval request."
        )
    if not _can_elicit(ctx.client_capabilities):
        raise approval_required("This client cannot relay an approval request to a person.")

    _log.info(
        "rebooking_proposed",
        proposed_by=proposer,
        proposal=proposal.as_dict(),
        proposal_sha256=proposal.sha256,
        idempotency_key=proposal.idempotency_key,
    )
    message = (
        f"Approve rebooking: move {proposal.passenger_count} passenger(s) "
        f"({proposal.cohort}) from {proposal.from_flight} on {proposal.flight_date} to "
        f"{proposal.to_flight}. Reason: {proposal.reason_code}. Proposed by {proposer}. "
        f"Proposal SHA-256: {proposal.sha256}. An approver other than the proposer must "
        "approve this exact proposal; return the approval grant, or decline."
    )
    request = ElicitRequest(
        params=ElicitRequestFormParams(
            message=message,
            requested_schema={
                "type": "object",
                "properties": {
                    # The canonical proposal, as the form's own data. An approval
                    # client reads it from here instead of parsing the prose above,
                    # and the approver's grant must describe exactly this object.
                    "proposal": {
                        "type": "string",
                        "title": "Proposal (canonical JSON, read-only)",
                        "description": "Exactly what will be executed. The approval grant "
                        f"must carry this object as a {APPROVAL_DETAIL_TYPE!r} authorization "
                        f"detail, for audience {runtime.settings.approval_audience!r}, with "
                        f"scope {Scopes.REBOOKING_APPROVE!r}.",
                        "default": proposal.canonical().decode("ascii"),
                    },
                    "approval_token": {
                        "type": "string",
                        "title": "Approval grant",
                        "description": "The signed grant issued to the approver for this "
                        "proposal. Not a password and not an access token.",
                    },
                },
                "required": ["approval_token"],
            },
        )
    )
    state = json.dumps(
        {"v": 1, "proposal_sha256": proposal.sha256, "proposed_at": runtime.clock()},
        separators=(",", ":"),
    )
    return InputRequiredResult(input_requests={APPROVAL_REQUEST: request}, request_state=state)


def _can_elicit(capabilities: ClientCapabilities | None) -> bool:
    """Form elicitation declared. A bare `elicitation: {}` predates modes and means form."""
    elicitation = capabilities.elicitation if capabilities is not None else None
    return elicitation is not None and (elicitation.form is not None or elicitation.url is None)


def _check_proposal_state(state: str | None, proposal: Proposal) -> None:
    """An approval answers a question this server asked, about this proposal.

    The SDK has already authenticated the state and bound it to the caller, the
    tool and the arguments. This is the second check, and the one that says so in
    this codebase's own terms.
    """
    if state is None:
        raise approval_required("An approval was sent for a proposal this server never issued.")
    try:
        recorded = json.loads(state)
    except ValueError:
        recorded = None
    if not isinstance(recorded, dict) or recorded.get("proposal_sha256") != proposal.sha256:
        raise approval_required("The approval does not belong to this proposal.")


async def _on_behalf_of(
    runtime: Runtime,
    inbound: AccessToken,
    scopes: list[str],
    call: Callable[[str], Awaitable[Any]],
) -> Any:
    """Run `call` with a token exchanged for the operations API, as the user.

    A cached token the API no longer accepts is exchanged again, once. A second
    refusal is a real failure, not something to loop on.
    """
    audience = runtime.settings.ops_api_audience
    token = await runtime.obo.token_for(inbound, audience=audience, scopes=scopes)
    try:
        return await call(token)
    except DownstreamTokenRejected:
        runtime.obo.invalidate(inbound, audience=audience, scopes=scopes)
        token = await runtime.obo.token_for(inbound, audience=audience, scopes=scopes)
        try:
            return await call(token)
        except DownstreamTokenRejected:
            raise ToolFailure(
                code="downstream_auth_failed",
                message="The internal operations API rejected a freshly exchanged token.",
                hint="Not retryable by the caller; an operator must check the API's audience "
                "and issuer configuration.",
            ) from None


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


def _legs_without_evidence(
    weather: dict[str, Any], traffic: dict[str, Any], movements: dict[str, Any]
) -> list[str]:
    """The triage legs that observed nothing, in `EVIDENCE_LEGS` order.

    Evidence is something seen: a current METAR, an aircraft in a current snapshot,
    a recorded flight. A leg that succeeded with nothing in it has seen nothing, and
    neither has one whose only observation is too old to describe the present.
    """
    seen = {
        "weather": bool(weather.get("ok") and weather.get("latest_metar", {}).get("current")),
        "traffic": bool(
            traffic.get("ok")
            and traffic.get("snapshot_current")
            and traffic.get("counts", {}).get("returned", 0) > 0
        ),
        "movements": bool(movements.get("ok") and any(movements.get("counts", {}).values())),
    }
    return [leg for leg in EVIDENCE_LEGS if not seen[leg]]


def _severity(
    reasons: list[str], degraded: list[str], has_movements: bool, *, has_evidence: bool
) -> str:
    """A deliberately conservative escalation ladder.

    Unknown outranks elevated: a triage that cannot see the data must not imply
    that operations are fine. `low` is a claim, so it needs an observation behind
    it: three legs that succeeded and saw nothing are not calm.
    """
    if not has_movements and degraded:
        return "unknown"
    if len(reasons) >= HIGH_REASONS:
        return "high"
    if reasons:
        return "elevated"
    if degraded or not has_evidence:
        return "unknown"
    return "low"


def _distinct_reasons(observed: list[tuple[str, str]], metars: int) -> list[dict[str, Any]]:
    """Each reason once, in first-seen order, with how many observations said it.

    A weather flag is said once per METAR in the window, so its line names how many of
    the METARs carried it. The ladder counts these distinct reasons, never the repeats.
    """
    seen: dict[tuple[str, str], int] = {}
    for leg, text in observed:
        seen[(leg, text)] = seen.get((leg, text), 0) + 1
    findings: list[dict[str, Any]] = []
    for (leg, text), times in seen.items():
        reason = text
        if leg == "weather":
            reason = f"{text} (in {times} of {metars} METARs from the last 2 h)"
        findings.append({"reason": reason, "leg": leg, "observations": times})
    return findings


def _counted(readable: int, received: int, **extra: Any) -> dict[str, Any]:
    return {"count": readable, "received": received, "unreadable": received - readable, **extra}


def _triage_counts(
    movements: dict[str, Any], traffic: dict[str, Any], radius_nm: float
) -> dict[str, Any]:
    """What each counting leg counted, and how much of its answer was readable.

    None for a count that was never made: a failed leg, or a direction not fetched.
    Zero is a count; None is not one.
    """
    rows = (movements.get("movements") or {}) if movements.get("ok") else {}
    received = movements.get("received") or {}
    directions: dict[str, Any] = {}
    for name, which in (("arrivals", "arrival"), ("departures", "departure")):
        if which in rows:
            readable = len(rows[which])
            directions[name] = _counted(readable, int(received.get(which, readable)))
        else:
            directions[name] = None
    aircraft: dict[str, Any] | None = None
    if traffic.get("ok"):
        counts = traffic.get("counts") or {}
        listed = int(counts.get("returned", 0))
        got = int(counts.get("received", listed))
        unreadable = int(counts.get("unreadable", 0))
        aircraft = _counted(
            got - unreadable,
            got,
            listed=listed,
            airborne=counts.get("airborne"),
            on_ground=counts.get("on_ground"),
            truncated=bool((traffic.get("bookkeeping") or {}).get("truncated")),
        )
    window = (movements.get("window") or {}).get("minutes") if movements.get("ok") else None
    return {
        **directions,
        "movements_window_minutes": window,
        # A box around the radius, not a circle: what OpenSky's /states/all accepts.
        "aircraft_in_search_box": aircraft,
        "search_radius_nm": max(5.0, min(200.0, radius_nm)),
    }


def _newest_metar(payload: dict[str, Any]) -> dict[str, Any] | None:
    metars: list[dict[str, Any]] = (payload.get("metar") or []) if payload.get("ok") else []
    if not metars:
        return None
    return max(metars, key=lambda metar: _epoch(metar.get("observation_time")) or 0.0)


def _weather_values(payload: dict[str, Any]) -> dict[str, Any] | None:
    """The newest METAR as values: ceiling, visibility, wind, category. None when absent."""
    newest = _newest_metar(payload)
    if newest is None:
        return None
    latest = payload.get("latest_metar") or {}
    return {
        "station": newest.get("station_id"),
        "observed_at": newest.get("observation_time"),
        "age_s": latest.get("age_s"),
        "current": bool(latest.get("current")),
        "category": newest.get("flight_category"),
        "ceiling": ceiling(newest),
        "visibility": {
            "statute_mi": newest.get("visibility_statute_mi"),
            "at_least": bool(newest.get("visibility_at_least")),
        },
        "wind": {
            "direction_deg": newest.get("wind_dir_deg"),
            "variable": bool(newest.get("wind_variable")),
            "speed_kt": newest.get("wind_speed_kt"),
            "gust_kt": newest.get("wind_gust_kt"),
        },
        "significant_weather": newest.get("wx_string"),
        "raw": newest.get("raw_text"),
    }


def _leg(
    status: str,
    source: str,
    upstream: object,
    data_time: float | None,
    *,
    error: str | None = None,
) -> dict[str, Any]:
    """One leg's provenance and age. The age is the upstream's clock minus the data's time."""
    upstream_epoch = _newest(upstream)
    age = (
        round(upstream_epoch - data_time)
        if upstream_epoch is not None and data_time is not None
        else None
    )
    return {
        "status": status,
        "source": source,
        "upstream_time_epoch": upstream_epoch,
        "data_time_epoch": None if data_time is None else int(data_time),
        "age_s": age,
        "error": error,
    }


def _failed_code(payload: dict[str, Any]) -> str:
    return str((payload.get("error") or {}).get("code", "unknown"))


def _weather_leg(payload: dict[str, Any]) -> dict[str, Any]:
    source = "AviationWeather.gov (NOAA) /metar"
    if not payload.get("ok"):
        return _leg("unavailable", source, None, None, error=_failed_code(payload))
    newest = _newest_metar(payload)
    observed = _epoch(newest.get("observation_time")) if newest else None
    current = bool((payload.get("latest_metar") or {}).get("current"))
    status = "observed" if current else ("empty" if newest is None else "not_current")
    return _leg(status, source, payload.get("upstream_time_epoch"), observed)


def _traffic_leg(payload: dict[str, Any]) -> dict[str, Any]:
    source = "OpenSky Network /states/all"
    if not payload.get("ok"):
        return _leg("unavailable", source, None, None, error=_failed_code(payload))
    snapshot = _epoch(payload.get("snapshot_time_epoch"))
    heard = int((payload.get("counts") or {}).get("returned", 0))
    current = bool(payload.get("snapshot_current"))
    status = "not_current" if not current and heard else ("observed" if heard else "empty")
    return _leg(status, source, payload.get("upstream_time_epoch"), snapshot)


def _movements_leg(payload: dict[str, Any], degraded: list[str]) -> dict[str, Any]:
    source = "OpenSky Network /flights/arrival, /flights/departure"
    if not payload.get("ok"):
        return _leg("unavailable", source, None, None, error=_failed_code(payload))
    rows = payload.get("movements") or {}
    if not rows:
        why = next((d.split(":", 1)[1] for d in degraded if d.startswith("movements:")), None)
        return _leg("not_fetched", source, None, None, error=why or "credentials_missing")
    last_seen = [
        t for flights in rows.values() for f in flights if (t := _epoch(f.get("last_seen")))
    ]
    status = "observed" if last_seen or any(rows.values()) else "empty"
    return _leg(
        status, source, payload.get("upstream_time_epoch"), max(last_seen) if last_seen else None
    )


def _headroom(weather: dict[str, Any] | None, counts: dict[str, Any]) -> list[str]:
    """Measured values that sit below a threshold: the evidence for not going higher."""
    lines: list[str] = []
    if weather is not None and weather["current"]:
        category = weather["category"]
        if category and category not in IFR_CATEGORIES:
            lines.append(f"Flight category {category}, not IFR or LIFR.")
        gust = weather["wind"]["gust_kt"]
        speed = weather["wind"]["speed_kt"]
        if isinstance(gust, int | float):
            if gust < GUST_FLAG_KT:
                lines.append(f"Gusts {gust:g} kt, below the {GUST_FLAG_KT} kt flag.")
        elif isinstance(speed, int | float):
            lines.append(f"Wind {speed:g} kt with no gusts reported.")
        visibility = weather["visibility"]["statute_mi"]
        if isinstance(visibility, int | float) and visibility >= VISIBILITY_FLAG_SM:
            floor = " or more" if weather["visibility"]["at_least"] else ""
            lines.append(
                f"Visibility {visibility:g}{floor} sm, not below {VISIBILITY_FLAG_SM:g} sm."
            )
        if not weather["significant_weather"]:
            lines.append("No significant weather group in the METAR.")
    elif weather is not None:
        lines.append("The newest METAR is not current, so it cannot rule weather out.")
    aircraft = counts["aircraft_in_search_box"]
    if aircraft is not None and aircraft["listed"] <= BUSY_AIRCRAFT:
        lines.append(
            f"{aircraft['listed']} aircraft in the search box, not more than {BUSY_AIRCRAFT}."
        )
    arrivals, departures = counts["arrivals"], counts["departures"]
    if arrivals is not None and departures is not None:
        total = arrivals["count"] + departures["count"]
        window = counts["movements_window_minutes"]
        if total:
            lines.append(
                f"{arrivals['count']} arrivals and {departures['count']} departures in the "
                f"last {window} min: traffic is moving."
            )
        elif aircraft is not None and aircraft["listed"] == 0:
            lines.append(
                "No movements and no aircraft nearby: a quiet hour, not a stoppage with "
                "aircraft waiting."
            )
    return lines


def _why_not(
    severity: str,
    *,
    reasons: list[str],
    degraded: list[str],
    silent: list[str],
    weather: dict[str, Any] | None,
    counts: dict[str, Any],
) -> tuple[list[str], list[str]]:
    """Why the severity is not one rung higher, and why it is not one rung lower.

    Each line is a measured value or a missing leg, never an adjective, so a reader can
    check it against the payload and decide whether to stand down or escalate.
    """
    headroom = _headroom(weather, counts)
    missing = [entry.replace(":", " unavailable: ", 1) for entry in degraded]
    if severity == "high":
        return (
            ["High is the top of this ladder."],
            [f"{len(reasons)} distinct reasons; high needs {HIGH_REASONS}.", *reasons],
        )
    if severity == "elevated":
        return (
            [f"{len(reasons)} distinct reason(s); high needs {HIGH_REASONS}.", *headroom],
            [f"Low needs no reason; this triage has {len(reasons)}.", *reasons],
        )
    if severity == "low":
        return (
            ["No reason to raise it was observed.", *headroom],
            ["Low is the bottom of this ladder."],
        )
    higher = [f"{leg}." for leg in missing]
    if reasons:
        rung = "high" if len(reasons) >= HIGH_REASONS else "elevated"
        higher.append(
            f"The legs that answered give {len(reasons)} reason(s), which alone would be "
            f"{rung}; unknown outranks it while a leg cannot be seen."
        )
    else:
        higher.append("No reason to raise it was observed by the legs that answered.")
    lower: list[str] = []
    if len(silent) == len(EVIDENCE_LEGS):
        lower.append("Low needs positive evidence from at least one leg; none had any.")
    if degraded:
        lower.append("Low needs every leg to answer; " + ", ".join(missing) + ".")
    return higher, lower


def log_opensky_mode(settings: Settings) -> None:
    """Say at startup how this process reaches OpenSky. Names a mode, never a credential."""
    _log.info(
        "opensky_mode",
        credentials=credentials_mode(settings),
        partly_configured=bool(settings.opensky_client_id) != bool(settings.opensky_client_secret),
    )


def create_server(
    *,
    auth: AuthSettings | None = None,
    token_verifier: TokenVerifier | None = None,
    request_state_security: RequestStateSecurity | None = None,
) -> MCPServer:
    """Build a server with every registered tool, optionally behind bearer auth.

    With auth, every tool is wrapped in its scope check and the HTTP app answers
    a missing scope with a `WWW-Authenticate` challenge.

    The tracing middleware is registered on both servers: on stdio an operator
    gets spans for local debugging, and on HTTP the client's `traceparent` from
    `_meta` becomes the parent span.
    """
    authenticated = auth is not None
    # No tracing middleware is registered here on purpose: `MCPServer` already
    # installs its own `OpenTelemetryMiddleware` as the outermost built-in
    # (`mcp/server/mcpserver/server.py`: "User middleware runs inside the SDK's
    # built-ins (OpenTelemetry, then the request-state boundary)"). Adding a second
    # one produced two spans per request — verified, then removed.
    server: MCPServer = (ScopeChallengingServer if authenticated else MCPServer)(
        name="flightops-mcp",
        version=__version__,
        instructions=INSTRUCTIONS,
        auth=auth,
        token_verifier=token_verifier,
        request_state_security=request_state_security,
        cache_hints=LIST_CACHE_HINTS,
    )
    # Sorted by name, not registration order. A host puts the tool list near the top
    # of a model's prompt, and a prompt cache only hits on an identical prefix: an
    # order that followed import order would change when a module was reorganised.
    for fn in sorted(_TOOLS, key=lambda tool_fn: tool_fn.__name__):
        server.add_tool(scoped(fn) if authenticated else fn)
    return server


def readiness_checks(runtime: Runtime | None = None) -> dict[str, dict[str, Any]]:
    """Local ability to serve. Deliberately excludes upstream health.

    An open circuit or a NOAA outage must not take every replica out of rotation:
    the traffic tools degrade and the weather tool keeps working. Readiness answers
    "can this instance answer at all", and the upstream states are reported next to
    it for the operator to read, not to gate on.
    """
    current = runtime or get_runtime()
    airports_ok = current.airports.healthy()
    return {
        "reference_data": {
            "ok": airports_ok,
            "detail": str(current.airports.path),
        },
        "upstreams": {
            "ok": True,
            "detail": breakers().snapshots(),
        },
        "tracing": {
            "ok": True,
            "detail": tracing_kind() or "none",
        },
    }


def install_observability_routes(
    server: MCPServer, *, clock: Callable[[], float] = time.time
) -> None:
    """`/healthz`, `/readyz` and `/metrics` on the HTTP transport.

    The SDK documents `custom_route` for exactly this: endpoints outside the
    protocol, reachable without the authorization the MCP path requires, because a
    load balancer has no token and a Prometheus scrape has no user.
    """
    started_at = clock()

    @server.custom_route("/healthz", methods=["GET"], name="healthz")  # type: ignore[untyped-decorator]
    async def healthz(_request: Request) -> Response:
        """Liveness: the process is up and serving. Never touches an upstream."""
        return JSONResponse(
            {
                "status": "ok",
                "version": __version__,
                "uptime_s": round(clock() - started_at, 1),
            }
        )

    @server.custom_route("/readyz", methods=["GET"], name="readyz")  # type: ignore[untyped-decorator]
    async def readyz(_request: Request) -> Response:
        """Readiness: this instance can answer requests, checked locally."""
        checks = readiness_checks()
        ready = all(bool(check["ok"]) for check in checks.values())
        return JSONResponse({"ready": ready, "checks": checks}, status_code=200 if ready else 503)

    @server.custom_route("/metrics", methods=["GET"], name="metrics")  # type: ignore[untyped-decorator]
    async def metrics(_request: Request) -> Response:
        """Prometheus scrape endpoint for the registry the tools already write to.

        Uses the module's own registry, not `prometheus_client.REGISTRY`: the
        metrics in `telemetry.py` are registered on a private collector registry so
        a host process cannot collide with them, and scraping the default registry
        would export only the Python runtime collectors.
        """
        return Response(generate_latest(METRICS_REGISTRY), media_type=CONTENT_TYPE_LATEST)


def create_http_server(
    settings: Settings,
    *,
    jwks_transport: httpx.AsyncBaseTransport | None = None,
    clock: Callable[[], float] = time.time,
) -> MCPServer:
    """The remote server: same tools, every request authenticated.

    Raises `AuthConfigError` when the issuer is not configured. There is no flag
    to serve HTTP without authentication.
    """
    resource_server = build_resource_server_auth(
        settings, jwks_transport=jwks_transport, clock=clock
    )
    server = create_server(
        auth=resource_server.settings,
        token_verifier=resource_server.verifier,
        request_state_security=request_state_security(settings),
    )
    install_observability_routes(server)
    return server


def request_state_security(settings: Settings) -> RequestStateSecurity:
    """How the state between a proposal and its approval is protected.

    The SDK seals it, binds it to the caller, the tool and the arguments, and
    expires it. The TTL is the approval window. With no shared secret the key is
    per process, which fails closed across replicas instead of trusting state
    another process may not have minted.
    """
    ttl = settings.approval_window_s
    if settings.request_state_secret:
        return RequestStateSecurity(keys=[settings.request_state_secret], ttl=ttl)
    return RequestStateSecurity.ephemeral(ttl=ttl)


class HttpOptions(TypedDict):
    stateless_http: bool
    json_response: bool


# Stateless: the 2026-07-28 revision carries no session, so any replica can
# serve any request and there is no session table to protect or expire.
HTTP_OPTIONS: Final[HttpOptions] = {"stateless_http": True, "json_response": True}

# The stdio server. Local hosts launch it as a subprocess of the user's own
# session, so the operating system is the trust boundary and there is no token.
mcp: MCPServer = create_server()


def main(argv: Sequence[str] | None = None) -> None:
    """Console entry point: `flightops-mcp [--transport stdio|streamable-http]`."""
    parser = argparse.ArgumentParser(prog="flightops-mcp")
    parser.add_argument(
        "--transport",
        default="stdio",
        choices=("stdio", "streamable-http"),
        help=(
            "stdio for local hosts (Claude Desktop, Inspector); streamable-http to serve "
            "remotely, which requires FLIGHTOPS_AUTH_ISSUER and FLIGHTOPS_AUTH_JWKS_URL."
        ),
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args(argv)

    settings = Settings()
    configure_logging(settings.log_level)
    configure_breakers(
        failure_threshold=settings.breaker_failure_threshold,
        recovery_timeout_s=settings.breaker_recovery_timeout_s,
    )
    configure_tracing(settings)
    log_opensky_mode(settings)
    _log.info(
        "observability_configured",
        tracing=tracing_kind() or "none",
        breaker_failure_threshold=settings.breaker_failure_threshold,
        breaker_recovery_s=settings.breaker_recovery_timeout_s,
    )

    if args.transport == "stdio":
        _log.info("server_starting", transport="stdio", version=__version__)
        mcp.run(transport="stdio")
        return

    try:
        server = create_http_server(settings)
    except AuthConfigError as exc:
        parser.error(str(exc))
    _log.info(
        "server_starting",
        transport="streamable-http",
        version=__version__,
        issuer=settings.auth_issuer,
        resource=settings.resource_server_url,
    )
    server.run(transport="streamable-http", host=args.host, port=args.port, **HTTP_OPTIONS)


if __name__ == "__main__":  # pragma: no cover
    main()
