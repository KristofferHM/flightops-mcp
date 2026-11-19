"""The accessibility fixture must really put the board in the states the gate claims to walk."""

from __future__ import annotations

import httpx
import pytest

from scripts.a11y_server import OLD_AGE_S, SyntheticSource, build_app

LOCAL = "http://127.0.0.1:8791"


def client(scenario: str, passcode: str | None = None) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=build_app(scenario, passcode))
    return httpx.AsyncClient(transport=transport, base_url=LOCAL)


async def test_the_data_scenario_is_a_live_board_with_every_marker_kind() -> None:
    async with client("data") as http:
        board = (await http.get("/api/board")).json()

    assert board["banner"]["mode"] == "LIVE"
    assert board["last_update"]["lines"][0]["text"] == "Last update 4 min ago"
    assert len(board["airports"]) > 1
    categories = {airport["marker"]["category"] for airport in board["airports"]}
    assert {"VFR", None} <= categories


async def test_the_empty_scenario_is_the_no_data_yet_state() -> None:
    async with client("empty") as http:
        board = (await http.get("/api/board")).json()

    assert board["banner"]["mode"] == "NO DATA"
    assert board["last_update"]["state"] == "no_data"
    assert board["last_update"]["lines"][0]["text"] == "no data yet"
    assert {airport["marker"]["kind"] for airport in board["airports"]} == {"unknown"}


async def test_the_no_time_scenario_claims_no_age() -> None:
    async with client("no_time") as http:
        board = (await http.get("/api/board")).json()

    assert board["last_update"] == {
        "state": "no_upstream_time",
        "lines": [
            {
                "source": "weather",
                "label": "Weather",
                "state": "no_upstream_time",
                "text": "freshness unknown — no upstream time",
            }
        ],
    }
    assert {airport["marker"]["kind"] for airport in board["airports"]} == {"unknown"}


async def test_a_selected_airport_has_weather_and_traffic() -> None:
    async with client("data") as http:
        icao = (await http.get("/api/board")).json()["airports"][0]["icao"]
        detail = (await http.get(f"/api/airport/{icao}")).json()

    assert detail["icao"] == icao
    assert detail["traffic"]["summary"] == "1 heard"
    assert detail["traffic"]["unknowns"]


async def test_the_fixture_keeps_the_real_gate() -> None:
    async with client("data", passcode="fixture-only") as http:
        assert (await http.get("/api/board")).status_code == 401
        signed_in = await http.post("/login", data={"passcode": "fixture-only"})
        assert signed_in.status_code == 303
        assert (await http.get("/api/board")).status_code == 200


async def test_an_airport_without_a_category_is_old_rather_than_current() -> None:
    source = SyntheticSource("data", clock=lambda: 1_000.0)
    uncategorised = next(
        answer
        for answer in [await source.call("airport_weather", {"airport": f"A{n}"}) for n in range(9)]
        if answer["metar"][0]["flight_category"] is None
    )

    assert uncategorised["latest_metar"] == {"age_s": OLD_AGE_S, "current": False}


@pytest.mark.parametrize("tool", ["airport_movements", "schiphol_flights"])
async def test_a_tool_the_fixture_does_not_cover_fails_instead_of_inventing(tool: str) -> None:
    answer = await SyntheticSource("data").call(tool, {})

    assert answer["ok"] is False
    assert answer["error"]["code"] == "not_synthesised"
