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
import time
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime
from typing import Any, Final, TypedDict

import httpx
from mcp.server import MCPServer
from opentelemetry.trace import SpanKind, Status, StatusCode
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from . import __version__
from .auth import (
    AuthConfigError,
    AuthSettings,
    ScopeChallengingServer,
    Scopes,
    TokenVerifier,
    build_resource_server_auth,
    current_access_token,
)
from .config import Settings
from .errors import (
    ToolFailure,
    authentication_required,
    insufficient_scope,
    not_found,
    reference_data_missing,
)
from .opensky import MAX_HISTORY_S, TIME_RESOLUTION_S, BBox
from .ops import TIGHT_CONNECTION_MINUTES, DownstreamTokenRejected, normalise_flight, summarise
from .otel import configure_tracing, tracer, tracing_kind
from .resilience import breakers, configure_breakers
from .runtime import Runtime, get_runtime
from .telemetry import REGISTRY as METRICS_REGISTRY
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
6. `flight_passenger_impact` reads the internal operations API as the signed-in \
user and returns counts only. Never ask it for, or infer, an individual passenger.
"""

_log = get_logger(__name__)

ToolFn = Callable[..., Awaitable[dict[str, Any]]]

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
    async def wrapper(*args: Any, **kwargs: Any) -> dict[str, Any]:
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
                result: dict[str, Any] = await fn(*args, **kwargs)
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
            TOOL_CALLS.labels(tool=fn.__name__, outcome="ok").inc()
            span.set_attribute("mcp.tool.outcome", "ok")
            span.set_attribute("mcp.tool.ok", bool(result.get("ok", True)))
            return result

    return wrapper


def tool(fn: Any) -> Any:
    """Guard a tool function and add it to the registry every server is built from.

    Registration fails for a tool with no entry in the scope table, so a new tool
    cannot ship unprotected by omission.
    """
    Scopes.for_tool(fn.__name__)
    wrapped: ToolFn = guarded(fn)
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
    async def wrapper(*args: Any, **kwargs: Any) -> dict[str, Any]:
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


@tool
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
    token = await runtime.obo.token_for(inbound, audience=audience, scopes=scopes)
    try:
        rows = await runtime.ops.manifest(designator, token)
    except DownstreamTokenRejected:
        # A cached token the API no longer accepts: exchange again, once. A second
        # refusal is a real failure, not something to loop on.
        runtime.obo.invalidate(inbound, audience=audience, scopes=scopes)
        token = await runtime.obo.token_for(inbound, audience=audience, scopes=scopes)
        try:
            rows = await runtime.ops.manifest(designator, token)
        except DownstreamTokenRejected:
            raise ToolFailure(
                code="downstream_auth_failed",
                message="The internal operations API rejected a freshly exchanged token.",
                hint="Not retryable by the caller; an operator must check the API's audience "
                "and issuer configuration.",
            ) from None

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


def create_server(
    *,
    auth: AuthSettings | None = None,
    token_verifier: TokenVerifier | None = None,
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
    )
    for fn in _TOOLS:
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
    server = create_server(auth=resource_server.settings, token_verifier=resource_server.verifier)
    install_observability_routes(server)
    return server


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
