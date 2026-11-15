"""AviationWeather.gov: no-data is not no-problem, and flags are flags."""

from __future__ import annotations

import httpx
import pytest

from flightops_mcp.config import Settings
from flightops_mcp.weather import WeatherClient, fleet_impact_flags


def make_client(settings: Settings, handler: object) -> WeatherClient:
    return WeatherClient(settings, transport=httpx.MockTransport(handler))  # type: ignore[arg-type]


async def test_204_means_no_observation_and_is_not_an_error(settings: Settings) -> None:
    async with make_client(settings, lambda request: httpx.Response(204)) as client:
        assert await client.metar("ESGG") == []


async def test_new_noaa_field_names_are_mapped(settings: Settings) -> None:
    payload = [
        {
            "icaoId": "ESGG",
            "reportTime": "2026-11-14T23:20:00Z",
            "rawOb": "ESGG 142320Z 24012KT 9999 BKN030 08/05 Q1008",
            "fltCat": "VFR",
            "temp": 8.0,
            "dewp": 5.0,
            "wdir": 240,
            "wspd": 12,
            "wgst": 22,
            "visib": 6.0,
            "altim": 1008.0,
            "wxString": None,
        }
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["User-Agent"].startswith("flightops-mcp/")
        return httpx.Response(200, json=payload)

    async with make_client(settings, handler) as client:
        observations = await client.metar("ESGG")

    assert observations[0]["station_id"] == "ESGG"
    assert observations[0]["flight_category"] == "VFR"
    assert observations[0]["wind_gust_kt"] == 22
    assert observations[0]["product"] == "metar"


async def test_legacy_field_names_are_mapped_too(settings: Settings) -> None:
    payload = [{"station_id": "ESSA", "raw_text": "ESSA 142320Z", "flight_category": "IFR"}]
    async with make_client(settings, lambda request: httpx.Response(200, json=payload)) as client:
        observations = await client.metar("ESSA")
    assert observations[0]["raw_text"] == "ESSA 142320Z"
    assert observations[0]["flight_category"] == "IFR"


async def test_server_error_on_noaa_is_a_typed_failure(settings: Settings) -> None:
    from flightops_mcp.errors import ToolFailure

    async with make_client(settings, lambda request: httpx.Response(500)) as client:
        with pytest.raises(ToolFailure) as excinfo:
            await client.metar("ESGG")
    assert excinfo.value.code == "upstream_unavailable"


async def test_taf_request_omits_the_hours_parameter(settings: Settings) -> None:
    """Regression: NOAA returns 400 'Unexpected query parameter' for /taf?hours=N.

    Found by running against the live endpoint; the mock never would have said so.
    """
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, json=[{"icaoId": "ESGG", "rawTAF": "TAF ESGG"}])

    async with make_client(settings, handler) as client:
        await client.taf("ESGG")

    assert "hours" not in seen[0], seen[0]
    assert "format=json" in seen[0]


async def test_stringly_visibility_is_coerced(settings: Settings) -> None:
    # Live METARs encode unlimited visibility as the string "6+".
    payload = [{"icaoId": "ESGG", "visib": "6+", "wgst": "35", "temp": 7}]
    async with make_client(settings, lambda r: httpx.Response(200, json=payload)) as client:
        observations = await client.metar("ESGG")

    assert observations[0]["visibility_statute_mi"] == 6.0
    assert observations[0]["wind_gust_kt"] == 35.0
    assert observations[0]["temperature_c"] == 7.0


def test_flags_name_the_observation_and_their_limit() -> None:
    observations = [
        {
            "station_id": "ESGG",
            "flight_category": "LIFR",
            "wind_gust_kt": 40,
            "visibility_statute_mi": 0.5,
        },
        {"station_id": "ESSA", "flight_category": "VFR", "wind_gust_kt": 10},
    ]
    flags = fleet_impact_flags(observations)

    assert "ESGG: flight category LIFR" in flags
    assert "ESGG: gusts 40 kt" in flags
    assert "ESGG: visibility 0.5 sm" in flags
    assert not any(flag.startswith("ESSA") for flag in flags)


def test_no_flags_is_not_the_same_as_all_clear() -> None:
    assert fleet_impact_flags([]) == []
