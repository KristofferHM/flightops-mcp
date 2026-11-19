"""The instrument behind the before/after table: it counts what it sees and sends nothing."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import httpx
import pytest

from flightops_mcp.airports import build_from_csv
from flightops_mcp.config import Settings

from .test_weather_batch import DATE, WORLD_HEADER, metars

REPO = Path(__file__).resolve().parent.parent


def _script() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "measure_world_weather", REPO / "scripts" / "measure_world_weather.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_each_line_is_sorted_into_the_column_the_table_uses() -> None:
    classify = _script().classify

    assert classify({"summary": "VFR, METAR 4 min old"}) == "own report"
    assert classify({"summary": "Unknown: no METAR returned"}) == "no observation"
    assert classify({"summary": "No report at A — B", "neighbour": {"icao": "B"}}) == (
        "no observation"
    )
    assert classify({"summary": "Unknown: weather unavailable (station_rejected)"}) == (
        "id refused"
    )
    assert classify({"summary": "Unknown: weather unavailable (upstream_unavailable)"}) == (
        "upstream failed"
    )
    assert classify({"summary": "Unknown: METAR 3 h 0 min old"}) == "other unknown"


async def test_one_snapshot_is_measured_through_the_real_board(tmp_path: Path) -> None:
    rows = [
        f"{code},large_airport,{code} Airport,{lat},10.0,0,EU,XX,XX-1,,yes,"
        for code, lat in (("EAAA", 50.0), ("EBBB", 50.1), ("XX-0001", 51.0), ("ECCC", 52.0))
    ]
    csv_path = tmp_path / "four.csv"
    csv_path.write_text("\n".join([WORLD_HEADER, *rows]) + "\n", encoding="utf-8")
    database = tmp_path / "four.sqlite3"
    build_from_csv(csv_path, database)
    settings = Settings(_env_file=None, airports_db=database)  # type: ignore[call-arg]

    def noaa(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=metars(["EAAA"]), headers={"Date": DATE})

    module = _script()
    result = await module.measure("world", settings, httpx.MockTransport(noaa))

    log = result.pop("request_log")
    assert [(line["path"], line["ids"], line["status"], line["records"]) for line in log] == [
        ("metar", 3, 200, 1)
    ]
    assert result["requests"] == 1
    assert (result["own_report"], result["no_observation"], result["id_refused"]) == (1, 2, 1)
    assert result["neighbour_annotated"] == 1
    assert result["upstream_failed"] == 0


async def test_a_transport_error_is_counted_and_still_raised() -> None:
    def broken(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route", request=request)

    transport = _script().CountingTransport(httpx.MockTransport(broken))
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(httpx.ConnectError):
            await client.get("https://aviationweather.gov/api/data/metar?ids=ESGG")

    assert transport.log[0]["status"] == "ConnectError"
    assert transport.log[0]["ids"] == 1
