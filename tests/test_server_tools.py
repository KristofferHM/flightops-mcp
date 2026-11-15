"""End-to-end tool tests through the real MCP client, in memory.

`Client(mcp)` connects to the server object over an in-memory transport, so these
exercise the actual protocol path — schema generation, argument coercion, result
serialisation — without spawning a subprocess. The tools themselves still run
their real code; only the HTTP transport is mocked.
"""

from __future__ import annotations

import time
from typing import Any

import httpx
import pytest
from mcp import Client

from flightops_mcp import server as srv
from flightops_mcp.airports import build_from_csv
from flightops_mcp.config import Settings
from flightops_mcp.runtime import Runtime, set_runtime

from .conftest import FIXTURES, SleepRecorder

EXPECTED_TOOLS = {
    "resolve_airport",
    "airport_live_traffic",
    "airport_movements",
    "airport_weather",
    "disruption_triage",
    "upstream_status",
    "flight_passenger_impact",
}

EMPTY_STATES = {"time": 1_700_000_000, "states": []}
VFR_METAR = [
    {
        "icaoId": "ESGG",
        "reportTime": "2026-11-14T23:20:00Z",
        "rawOb": "ESGG 142320Z 24008KT 9999 BKN030 08/05 Q1008",
        "fltCat": "VFR",
        "wspd": 8,
        "visib": 6.0,
    }
]
LIFR_METAR = [
    {
        "icaoId": "ESGG",
        "reportTime": "2026-11-14T23:20:00Z",
        "rawOb": "ESGG 142320Z 24030G45KT 0500 FG VV002 02/02 Q0995",
        "fltCat": "LIFR",
        "wgst": 45,
        "visib": 0.3,
        "wxString": "FG",
    }
]


def install(
    settings: Settings,
    *,
    states: dict[str, Any] | None = None,
    movements: dict[str, Any] | None = None,
    metar: list[dict[str, Any]] | None = None,
    credentials: bool = True,
) -> Runtime:
    """Build a runtime with mocked upstreams and install it for the tools."""
    if not settings.airports_db.is_file():
        build_from_csv(FIXTURES / "airports_sample.csv", settings.airports_db)
    if not credentials:
        settings = settings.model_copy(
            update={"opensky_client_id": None, "opensky_client_secret": None}
        )

    def opensky_handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if "/states/" in path:
            return httpx.Response(200, json=states or EMPTY_STATES)
        if "/flights/" in path:
            payload = movements or {"begin": 1, "end": 2, "arrival": [], "departure": []}
            return httpx.Response(200, json=payload)
        return httpx.Response(404, json={})

    def token_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"access_token": "tok", "expires_in": 1800})

    def weather_handler(request: httpx.Request) -> httpx.Response:
        if "/taf" in request.url.path:
            return httpx.Response(200, json=[{"icaoId": "ESGG", "rawTAF": "TAF ESGG"}])
        if metar is None:
            return httpx.Response(204)
        return httpx.Response(200, json=metar)

    runtime = Runtime.build(
        settings,
        opensky_transport=httpx.MockTransport(opensky_handler),
        token_transport=httpx.MockTransport(token_handler),
        weather_transport=httpx.MockTransport(weather_handler),
        sleep=SleepRecorder(),
        clock=time.time,
    )
    set_runtime(runtime)
    return runtime


async def call(client: Client, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
    result = await client.call_tool(tool, arguments)
    structured = getattr(result, "structured_content", None)
    if isinstance(structured, dict) and structured:
        return structured
    for block in getattr(result, "content", []) or []:
        text = getattr(block, "text", None)
        if text:
            import json

            try:
                parsed = json.loads(text)
            except ValueError:
                continue
            if isinstance(parsed, dict):
                return parsed
    raise AssertionError(f"no structured payload returned by {tool}: {result!r}")


@pytest.fixture(autouse=True)
def _reset_runtime() -> Any:
    yield
    set_runtime(None)


async def test_all_tools_are_published_with_schemas(settings: Settings) -> None:
    install(settings)
    async with Client(srv.mcp) as client:
        tools = await client.list_tools()

    names = {tool.name for tool in tools.tools}
    assert names == EXPECTED_TOOLS
    for tool in tools.tools:
        assert tool.description and len(tool.description) > 40, (
            f"{tool.name} needs a usable description"
        )
        assert tool.input_schema.get("type") == "object"


async def test_resolve_airport_returns_the_icao_and_iata_pair(settings: Settings) -> None:
    install(settings)
    async with Client(srv.mcp) as client:
        payload = await call(client, "resolve_airport", {"query": "GOT"})

    assert payload["ok"] is True
    assert payload["matches"][0]["icao"] == "ESGG"
    assert payload["matches"][0]["iata"] == "GOT"
    assert payload["source"]["licence"] == "Public domain"


async def test_unknown_airport_is_a_typed_failure_not_an_exception(settings: Settings) -> None:
    install(settings)
    async with Client(srv.mcp) as client:
        payload = await call(client, "resolve_airport", {"query": "ZZZZ"})

    assert payload["ok"] is False
    assert payload["error"]["code"] == "not_found"
    assert "hint" in payload["error"]


async def test_weather_tool_reports_no_data_as_unknown(settings: Settings) -> None:
    install(settings, metar=None)
    async with Client(srv.mcp) as client:
        payload = await call(client, "airport_weather", {"airport": "ESGG"})

    assert payload["ok"] is True
    assert payload["impact_flags"] == []
    assert payload["unknowns"], "a missing METAR must be reported, not glossed over"
    assert "not that conditions are good" in payload["unknowns"][0]


async def test_weather_flags_significant_conditions(settings: Settings) -> None:
    install(settings, metar=LIFR_METAR)
    async with Client(srv.mcp) as client:
        payload = await call(client, "airport_weather", {"airport": "ESGG"})

    assert "ESGG: flight category LIFR" in payload["impact_flags"]
    assert "ESGG: gusts 45 kt" in payload["impact_flags"]
    assert "ESGG: significant weather FG" in payload["impact_flags"]


async def test_quiet_airport_at_night_is_not_reported_as_a_disruption(settings: Settings) -> None:
    """The 03:00 trap: zero movements and zero aircraft is quiet, not broken."""
    install(settings, metar=VFR_METAR)
    async with Client(srv.mcp) as client:
        payload = await call(client, "disruption_triage", {"airport": "ESGG"})

    assert payload["assessment"]["severity"] == "low"
    assert payload["assessment"]["reasons"] == []
    assert payload["degraded"] == []


async def test_aircraft_flying_with_no_movements_escalates(settings: Settings) -> None:
    busy = {
        "time": 1_700_000_000,
        "states": [
            [
                f"aa{i:02d}",
                f"FLT{i}",
                "SE",
                None,
                1_700_000_000,
                12.3,
                57.7,
                1000.0,
                False,
                200.0,
                90.0,
                0.0,
            ]
            for i in range(30)
        ],
    }
    install(settings, metar=VFR_METAR, states=busy)
    async with Client(srv.mcp) as client:
        payload = await call(client, "disruption_triage", {"airport": "ESGG"})

    assessment = payload["assessment"]
    assert assessment["severity"] == "elevated"
    assert any("no arrivals or departures" in reason for reason in assessment["reasons"])
    assert assessment["advisory_only"] is True


async def test_triage_without_credentials_degrades_to_unknown_not_to_all_clear(
    settings: Settings,
) -> None:
    install(settings, metar=VFR_METAR, credentials=False)
    async with Client(srv.mcp) as client:
        payload = await call(client, "disruption_triage", {"airport": "ESGG"})

    assert payload["assessment"]["severity"] == "unknown"
    assert any("credentials_missing" in item for item in payload["degraded"])
    assert any("data unavailable" in item for item in payload["assessment"]["unknowns"])


async def test_invalid_direction_returns_actionable_data(settings: Settings) -> None:
    install(settings)
    async with Client(srv.mcp) as client:
        payload = await call(
            client, "airport_movements", {"airport": "ESGG", "direction": "sideways"}
        )

    assert payload["ok"] is False
    assert payload["error"]["code"] == "invalid_argument"
    assert "arrival" in payload["error"]["message"]
    assert "both" in payload["error"]["hint"]


async def test_live_traffic_reports_counts_and_unknowns(settings: Settings) -> None:
    install(settings)
    async with Client(srv.mcp) as client:
        payload = await call(client, "airport_live_traffic", {"airport": "ESGG", "radius_nm": 40})

    assert payload["ok"] is True
    assert payload["counts"]["returned"] == 0
    assert payload["bookkeeping"]["credits_spent"] == 1
    assert payload["unknowns"], "empty airspace must not read as 'all normal'"
    assert payload["search"]["bbox_area_sq_deg"] <= 25


async def test_upstream_status_explains_the_environment(settings: Settings) -> None:
    install(settings)
    async with Client(srv.mcp) as client:
        payload = await call(client, "upstream_status", {})

    assert payload["credentials"]["opensky_configured"] is True
    assert payload["credentials"]["aviationweather_required"] is False
    assert payload["reference_data"]["airports_db_present"] is True
