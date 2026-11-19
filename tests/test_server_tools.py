"""End-to-end tool tests through the real MCP client, in memory.

`Client(mcp)` connects to the server object over an in-memory transport, so these
exercise the actual protocol path — schema generation, argument coercion, result
serialisation — without spawning a subprocess. The tools themselves still run
their real code; only the HTTP transport is mocked.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from mcp import Client

from flightops_mcp import server as srv
from flightops_mcp.airports import build_from_csv
from flightops_mcp.config import Settings
from flightops_mcp.opensky import OpenSkyClient
from flightops_mcp.runtime import Runtime, set_runtime
from flightops_mcp.weather import WeatherClient
from services.mock_upstream.dated import dated

from .conftest import FIXTURES, SleepRecorder

EXPECTED_TOOLS = {
    "resolve_airport",
    "airport_live_traffic",
    "airport_movements",
    "airport_weather",
    "airspace_snapshot",
    "disruption_triage",
    "schiphol_flights",
    "upstream_status",
    "flight_passenger_impact",
    "propose_rebooking",
}

# The instant the fixtures below describe: ten minutes after their METAR.
FIXTURE_NOW = int(datetime(2026, 11, 14, 23, 30, tzinfo=UTC).timestamp())
EMPTY_STATES = {"time": FIXTURE_NOW, "states": []}
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
    movements: list[dict[str, Any]] | None = None,
    metar: list[dict[str, Any]] | None = None,
    credentials: bool = True,
    data_clock: Any = None,
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
            # OpenSky documents a JSON array of flights.
            payload = movements or []
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

    # The upstreams' `Date` is the moment the fixture describes; ages are judged by it.
    world_clock = data_clock or (lambda: float(FIXTURE_NOW))
    runtime = Runtime.build(
        settings,
        opensky_transport=httpx.MockTransport(dated(opensky_handler, world_clock)),
        token_transport=httpx.MockTransport(token_handler),
        weather_transport=httpx.MockTransport(dated(weather_handler, world_clock)),
        schiphol_transport=httpx.MockTransport(
            dated(lambda request: httpx.Response(200, json={"flights": []}), world_clock)
        ),
        schiphol_token_transport=httpx.MockTransport(token_handler),
        sleep=SleepRecorder(),
        clock=time.time,
        data_clock=world_clock,
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


async def test_the_write_tool_publishes_its_arguments_and_not_its_plumbing(
    settings: Settings,
) -> None:
    """The model sees the six fields of a proposal. The request context is not an argument."""
    install(settings)
    async with Client(srv.mcp) as client:
        tools = await client.list_tools()

    (write,) = [tool for tool in tools.tools if tool.name == "propose_rebooking"]
    assert set(write.input_schema["properties"]) == {
        "from_flight",
        "flight_date",
        "to_flight",
        "cohort",
        "reason_code",
        "passenger_count",
    }
    assert set(write.input_schema["required"]) == set(write.input_schema["properties"])
    assert "approval" in write.description.lower(), "the caveat lives where the model reads it"


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


async def test_triage_with_nothing_heard_from_any_leg_is_unknown_not_low(
    settings: Settings,
) -> None:
    """Three legs answered and none of them said anything: that is not calm."""
    install(settings, metar=None)
    async with Client(srv.mcp) as client:
        payload = await call(client, "disruption_triage", {"airport": "ESGG"})

    assessment = payload["assessment"]
    assert payload["degraded"] == [], "every leg succeeded; this is the case the bug hid in"
    assert assessment["severity"] == "unknown"
    assert assessment["legs_without_evidence"] == ["weather", "traffic", "movements"]
    assert any("weather, traffic, movements" in item for item in assessment["unknowns"])


async def test_a_stale_metar_is_not_evidence_of_calm(settings: Settings) -> None:
    old = [{**VFR_METAR[0], "reportTime": "2026-11-14T18:00:00Z"}]
    install(settings, metar=old)
    async with Client(srv.mcp) as client:
        payload = await call(client, "disruption_triage", {"airport": "ESGG"})

    assert payload["assessment"]["severity"] == "unknown"
    assert "weather" in payload["assessment"]["legs_without_evidence"]


async def test_one_leg_with_evidence_is_enough_for_low(settings: Settings) -> None:
    """A current METAR is an observation. The two silent legs are still named."""
    install(settings, metar=VFR_METAR)
    async with Client(srv.mcp) as client:
        payload = await call(client, "disruption_triage", {"airport": "ESGG"})

    assert payload["assessment"]["severity"] == "low"
    assert payload["assessment"]["legs_without_evidence"] == ["traffic", "movements"]


async def test_a_metar_dated_in_the_future_is_not_evidence_of_calm(settings: Settings) -> None:
    ahead = [{**VFR_METAR[0], "reportTime": "2026-11-15T06:00:00Z"}]
    install(settings, metar=ahead)
    async with Client(srv.mcp) as client:
        payload = await call(client, "disruption_triage", {"airport": "ESGG"})

    assert payload["assessment"]["severity"] == "unknown"
    assert "weather" in payload["assessment"]["legs_without_evidence"]
    assert any("in the future" in item for item in payload["assessment"]["unknowns"])


@pytest.mark.parametrize("offset_s", [-3600, 3600], ids=["old", "future"])
async def test_aircraft_in_a_snapshot_that_is_not_current_are_not_evidence(
    settings: Settings, offset_s: int
) -> None:
    when = FIXTURE_NOW + offset_s
    row = ["abc123", "SAS1 ", "Sweden", when, when, 12.0, 57.7, 3000.0, False, 200.0, 90.0, 0.0]
    install(settings, metar=VFR_METAR, states={"time": when, "states": [row]})
    async with Client(srv.mcp) as client:
        payload = await call(client, "disruption_triage", {"airport": "ESGG"})

    assert "traffic" in payload["assessment"]["legs_without_evidence"]


async def test_a_current_snapshot_with_aircraft_is_evidence(settings: Settings) -> None:
    row = [
        "abc123",
        "SAS1 ",
        "Sweden",
        FIXTURE_NOW,
        FIXTURE_NOW,
        12.0,
        57.7,
        3000.0,
        False,
        200.0,
        90.0,
        0.0,
    ]
    install(settings, metar=None, states={"time": FIXTURE_NOW, "states": [row]})
    async with Client(srv.mcp) as client:
        payload = await call(client, "disruption_triage", {"airport": "ESGG"})

    assert payload["assessment"]["legs_without_evidence"] == ["weather", "movements"]


async def test_a_recorded_flight_alone_is_enough_for_low(settings: Settings) -> None:
    flight = {"icao24": "abc123", "callsign": "SAS1", "firstSeen": FIXTURE_NOW - 600}
    install(settings, metar=None, movements=[flight])
    async with Client(srv.mcp) as client:
        payload = await call(client, "disruption_triage", {"airport": "ESGG"})

    assert payload["assessment"]["severity"] == "low"
    assert payload["assessment"]["legs_without_evidence"] == ["weather", "traffic"]


async def test_aircraft_flying_with_no_movements_escalates(settings: Settings) -> None:
    busy = {
        "time": FIXTURE_NOW,
        "states": [
            [
                f"aa{i:02d}",
                f"FLT{i}",
                "SE",
                None,
                FIXTURE_NOW,
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


def _state(icao24: str, contact: int) -> list[Any]:
    return [icao24, "SAS1", "SE", contact, contact, 12.3, 57.7, 900.0, False, 120.0, 90.0, 0.0]


async def traffic(settings: Settings, states: dict[str, Any]) -> dict[str, Any]:
    install(settings, states=states)
    async with Client(srv.mcp) as client:
        return await call(client, "airport_live_traffic", {"airport": "ESGG"})


async def test_a_fresh_snapshot_with_aircraft_has_no_unknowns(settings: Settings) -> None:
    """The control for the three below: nothing is flagged when nothing is missing."""
    payload = await traffic(
        settings, {"time": FIXTURE_NOW - 5, "states": [_state("a1", FIXTURE_NOW - 6)]}
    )

    assert payload["counts"]["returned"] == 1
    assert payload["unknowns"] == []


async def test_a_stale_snapshot_is_reported_as_not_current(settings: Settings) -> None:
    four_hours = FIXTURE_NOW - 4 * 3600
    payload = await traffic(settings, {"time": four_hours, "states": [_state("a1", four_hours)]})

    assert payload["counts"]["returned"] == 1, "the aircraft are still shown"
    assert any("4 h 0 min old" in item for item in payload["unknowns"])


async def test_a_snapshot_without_a_time_cannot_be_called_current(settings: Settings) -> None:
    payload = await traffic(settings, {"states": [_state("a1", FIXTURE_NOW)]})

    assert any("no snapshot time" in item for item in payload["unknowns"])


async def test_unreadable_rows_make_the_count_a_lower_bound(settings: Settings) -> None:
    rows = [_state("a1", FIXTURE_NOW), _state("a2", FIXTURE_NOW), {"icao24": "a3"}, "a4", None]
    payload = await traffic(settings, {"time": FIXTURE_NOW, "states": rows})

    assert payload["counts"]["returned"] == 2
    assert any("3 of 5 rows" in item and "lower bound" in item for item in payload["unknowns"])


async def weather(settings: Settings, metar: list[dict[str, Any]]) -> dict[str, Any]:
    install(settings, metar=metar)
    async with Client(srv.mcp) as client:
        return await call(client, "airport_weather", {"airport": "ESGG"})


async def test_a_current_metar_has_no_unknowns(settings: Settings) -> None:
    payload = await weather(settings, VFR_METAR)

    assert payload["unknowns"] == []


async def test_a_metar_older_than_ninety_minutes_is_not_current_conditions(
    settings: Settings,
) -> None:
    # The fixture clock is 23:30; 21:59 is 91 minutes before it.
    old = [{**VFR_METAR[0], "reportTime": "2026-11-14T21:59:00Z"}]
    payload = await weather(settings, old)

    assert payload["metar"], "the observation is still shown"
    assert any("91 min old" in item for item in payload["unknowns"])


async def test_a_metar_at_ninety_minutes_is_still_current(settings: Settings) -> None:
    edge = [{**VFR_METAR[0], "reportTime": "2026-11-14T22:00:00Z"}]

    assert (await weather(settings, edge))["unknowns"] == []


async def test_a_metar_without_an_observation_time_is_not_current(settings: Settings) -> None:
    untimed = [{key: value for key, value in VFR_METAR[0].items() if key != "reportTime"}]
    payload = await weather(settings, untimed)

    assert any("no readable observation time" in item for item in payload["unknowns"])


async def test_movements_in_the_documented_shape_are_counted(settings: Settings) -> None:
    flight = {"icao24": "4ca1fa", "callsign": "SAS1  ", "estArrivalAirport": "ESGG"}
    install(settings, movements=[flight, {**flight, "icao24": "4ca1fb"}])
    async with Client(srv.mcp) as client:
        payload = await call(client, "airport_movements", {"airport": "ESGG"})

    assert payload["ok"] is True
    assert payload["counts"] == {"arrival": 2, "departure": 2}
    assert payload["unknowns"] == []
    assert payload["window"]["end_epoch"] == FIXTURE_NOW, "the window ends at the data clock"


async def test_upstream_status_explains_the_environment(settings: Settings) -> None:
    install(settings)
    async with Client(srv.mcp) as client:
        payload = await call(client, "upstream_status", {})

    assert payload["credentials"]["opensky_configured"] is True
    assert payload["credentials"]["aviationweather_required"] is False
    assert payload["reference_data"]["airports_db_present"] is True


# --- ages are judged by the upstream's `Date`, never by this machine's clock --------------
async def test_a_weather_answer_carries_noaas_clock(settings: Settings) -> None:
    payload = await weather(settings, VFR_METAR)

    assert payload["upstream_time_epoch"] == FIXTURE_NOW
    assert "as_of" not in payload, "a host-clock time would contradict the upstream's"
    assert payload["latest_metar"] == {"age_s": 600, "current": True}


async def test_a_metar_from_an_upstream_without_a_date_has_no_age(settings: Settings) -> None:
    runtime = install(settings, metar=VFR_METAR)
    await runtime.weather.aclose()
    runtime.weather = WeatherClient(
        runtime.settings,
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=VFR_METAR)),
    )
    async with Client(srv.mcp) as client:
        payload = await call(client, "airport_weather", {"airport": "ESGG"})

    assert payload["upstream_time_epoch"] is None
    assert payload["latest_metar"] == {"age_s": None, "current": False}
    assert any("no Date header" in item for item in payload["unknowns"])


async def test_a_snapshot_from_an_upstream_without_a_date_is_not_current(
    settings: Settings,
) -> None:
    states = {"time": FIXTURE_NOW, "states": [_state("a1", FIXTURE_NOW - 6)]}
    runtime = install(settings, states=states)
    await runtime.opensky.aclose()
    runtime.opensky = OpenSkyClient(
        runtime.settings,
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=states)),
        token_transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"access_token": "t", "expires_in": 1800})
        ),
    )
    async with Client(srv.mcp) as client:
        payload = await call(client, "airport_live_traffic", {"airport": "ESGG"})

    assert payload["upstream_time_epoch"] is None
    assert payload["snapshot_current"] is False
    assert any("no Date header" in item for item in payload["unknowns"])


async def test_every_data_tool_states_its_upstream_time(settings: Settings) -> None:
    install(
        settings,
        states={"time": FIXTURE_NOW, "states": [_state("a1", FIXTURE_NOW - 6)]},
        metar=VFR_METAR,
    )
    async with Client(srv.mcp) as client:
        answers = {
            tool: await call(client, tool, arguments)
            for tool, arguments in [
                ("airport_live_traffic", {"airport": "ESGG"}),
                ("airspace_snapshot", {"region": "europe"}),
                ("airport_movements", {"airport": "ESGG"}),
                ("airport_weather", {"airport": "ESGG"}),
                ("disruption_triage", {"airport": "ESGG"}),
            ]
        }

    assert {
        tool: answer["upstream_time_epoch"] for tool, answer in answers.items()
    } == dict.fromkeys(answers, FIXTURE_NOW)
    assert not any("as_of" in answer for answer in answers.values())
