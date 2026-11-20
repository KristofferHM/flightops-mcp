"""`aircraft_track`: an observed transit, an inferred arrival, and a destination with no source.

The three are different kinds of claim, so each test holds one of them apart from the
others: a path is never extended, an estimate is never given for a flight it does not
describe, and a destination is never filled in.
"""

from __future__ import annotations

import time
from typing import Any

import httpx
import pytest

from flightops_mcp import server as srv
from flightops_mcp.airports import build_from_csv
from flightops_mcp.config import Settings
from flightops_mcp.opensky import (
    AIRCRAFT_FLIGHTS_COST,
    TRACK_COST,
    decode_track,
)
from flightops_mcp.runtime import Runtime, set_runtime
from services.mock_upstream.dated import dated

from .conftest import FIXTURES, SleepRecorder

NOW = 1_795_126_400
START = NOW - 3000
TRACK = {
    "icao24": "4ca1fa",
    "callsign": "SAS4047 ",
    "startTime": float(START),
    "endTime": float(NOW - 60),
    "path": [
        # Sent out of order on purpose: a path drawn out of order is invented.
        [NOW - 60, 60.19, 11.10, 0, 190.0, True],
        [START, 57.66, 12.28, 0, 10.0, False],
        [START + 900, 58.40, 11.60, 9144.0, 15.0, False],
        ["late", 59.0, 11.0, 0, 0, False],
        [START + 1800, None, 11.2, 9144.0, 20.0, False],
    ],
}
SAME_FLIGHT = {
    "icao24": "4ca1fa",
    "callsign": "SAS4047 ",
    "firstSeen": START + 60,
    "lastSeen": NOW - 60,
    "estDepartureAirport": "ESGG",
    "estArrivalAirport": "ENGM",
}
EARLIER_FLIGHT = {
    "icao24": "4ca1fa",
    "callsign": "SAS4046 ",
    "firstSeen": START - 20_000,
    "lastSeen": START - 15_000,
    "estDepartureAirport": "ENGM",
    "estArrivalAirport": "ESGG",
}


def install(
    settings: Settings,
    *,
    track: Any = TRACK,
    flights: Any = (SAME_FLIGHT,),
    credentials: bool = True,
    dated_answers: bool = True,
) -> list[httpx.Request]:
    """A runtime whose OpenSky answers tracks and flights; every request is recorded."""
    if not settings.airports_db.is_file():
        build_from_csv(FIXTURES / "airports_sample.csv", settings.airports_db)
    if not credentials:
        settings = settings.model_copy(
            update={
                "opensky_client_id": None,
                "opensky_client_secret": None,
                "opensky_anonymous": True,
            }
        )
    seen: list[httpx.Request] = []

    def opensky(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.endswith("/tracks/all"):
            return httpx.Response(404) if track is None else httpx.Response(200, json=track)
        if request.url.path.endswith("/flights/aircraft"):
            return httpx.Response(200, json=list(flights))
        return httpx.Response(404)

    def token(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"access_token": "tok", "expires_in": 1800})

    def clock() -> float:
        return float(NOW)

    handler = dated(opensky, clock) if dated_answers else opensky
    runtime = Runtime.build(
        settings,
        opensky_transport=httpx.MockTransport(handler),
        token_transport=httpx.MockTransport(token),
        weather_transport=httpx.MockTransport(lambda _r: httpx.Response(204)),
        schiphol_transport=httpx.MockTransport(lambda _r: httpx.Response(200, json={})),
        schiphol_token_transport=httpx.MockTransport(token),
        sleep=SleepRecorder(),
        clock=time.time,
        data_clock=clock,
    )
    set_runtime(runtime)
    return seen


@pytest.fixture(autouse=True)
def _reset_runtime() -> Any:
    yield
    set_runtime(None)


def _paths(seen: list[httpx.Request]) -> list[str]:
    return [request.url.path.rsplit("/api", 1)[-1] for request in seen]


async def test_the_transit_is_the_observed_waypoints_in_time_order(settings: Settings) -> None:
    seen = install(settings)

    payload = await srv.aircraft_track("4CA1FA")

    transit = payload["transit"]
    assert payload["ok"] is True
    assert payload["icao24"] == "4ca1fa"
    assert transit["state"] == "observed"
    assert transit["callsign"] == "SAS4047"
    assert [row[0] for row in transit["waypoints"]] == [START, START + 900, NOW - 60]
    # The last waypoint is the last thing drawn: nothing is extrapolated past it.
    assert transit["last_waypoint_epoch"] == NOW - 60
    assert transit["columns"] == list(srv.WAYPOINT_COLUMNS)
    assert (transit["received"], transit["unreadable"]) == (5, 2)
    assert any("2 of 5 waypoints" in line for line in payload["unknowns"])
    assert _paths(seen) == ["/tracks/all"]
    assert seen[0].url.params["time"] == "0"


async def test_the_estimate_is_not_asked_for_unless_requested(settings: Settings) -> None:
    seen = install(settings)

    payload = await srv.aircraft_track("4ca1fa")

    assert payload["estimated_arrival"]["state"] == "not_requested"
    assert payload["bookkeeping"]["credits_spent"] == TRACK_COST == 4
    assert "/flights/aircraft" not in _paths(seen)


async def test_an_estimate_for_the_observed_flight_is_labelled_an_estimate(
    settings: Settings,
) -> None:
    seen = install(settings, flights=(EARLIER_FLIGHT, SAME_FLIGHT))

    payload = await srv.aircraft_track("4ca1fa", estimate_arrival=True)

    arrival = payload["estimated_arrival"]
    assert arrival["state"] == "estimated"
    assert arrival["airport"] == "ENGM"
    assert arrival["flight"]["departure_airport"] == "ESGG"
    assert arrival["previous_flight"]["arrival_airport"] == "ESGG"
    assert "not a schedule" in arrival["basis"]
    assert payload["bookkeeping"]["credits_spent"] == TRACK_COST + AIRCRAFT_FLIGHTS_COST == 34
    flights = next(r for r in seen if r.url.path.endswith("/flights/aircraft"))
    assert int(flights.url.params["end"]) - int(flights.url.params["begin"]) == 86_400


async def test_an_estimate_for_another_flight_is_not_this_flights(settings: Settings) -> None:
    install(settings, flights=(EARLIER_FLIGHT,))

    payload = await srv.aircraft_track("4ca1fa", estimate_arrival=True)

    arrival = payload["estimated_arrival"]
    assert arrival["state"] == "no_record_for_this_flight"
    assert arrival["airport"] is None
    assert arrival["previous_flight"]["arrival_airport"] == "ESGG"
    assert any("publishes a flight once it has ended" in u for u in payload["unknowns"])


async def test_a_record_without_an_arrival_airport_says_so(settings: Settings) -> None:
    install(settings, flights=({**SAME_FLIGHT, "estArrivalAirport": None},))

    arrival = (await srv.aircraft_track("4ca1fa", estimate_arrival=True))["estimated_arrival"]

    assert (arrival["state"], arrival["airport"]) == ("not_estimated", None)


async def test_the_destination_has_no_source_whatever_else_is_known(settings: Settings) -> None:
    install(settings)

    payload = await srv.aircraft_track("4ca1fa", estimate_arrival=True)

    assert payload["destination"]["state"] == "no_source"
    assert payload["destination"]["text"].startswith("No source.")
    assert "ENGM" not in str(payload["destination"])


async def test_no_track_is_none_and_says_so(settings: Settings) -> None:
    install(settings, track=None)

    payload = await srv.aircraft_track("4ca1fa")

    assert payload["transit"]["state"] == "none"
    assert payload["transit"]["waypoints"] == []
    assert any("holds no track" in line for line in payload["unknowns"])


async def test_anonymous_access_gets_the_path_but_no_estimate(settings: Settings) -> None:
    seen = install(settings, credentials=False)

    payload = await srv.aircraft_track("4ca1fa", estimate_arrival=True)

    assert payload["transit"]["state"] == "observed"
    assert payload["estimated_arrival"]["state"] == "unavailable"
    assert payload["estimated_arrival"]["error"] == "credentials_missing"
    assert "/flights/aircraft" not in _paths(seen)


async def test_without_a_date_header_no_recency_is_claimed(settings: Settings) -> None:
    install(settings, dated_answers=False)

    payload = await srv.aircraft_track("4ca1fa")

    assert payload["upstream_time_epoch"] is None
    assert any("no Date header" in line for line in payload["unknowns"])


@pytest.mark.parametrize("bad", ["SAS4047", "4ca1f", "4ca1fz", "", "4ca1fa0"])
async def test_a_callsign_or_a_malformed_address_is_refused_before_spending(
    settings: Settings, bad: str
) -> None:
    seen = install(settings)

    payload = await srv.aircraft_track(bad)

    assert payload["ok"] is False
    assert payload["error"]["code"] == "invalid_argument"
    assert seen == []


@pytest.mark.parametrize(
    "body",
    [
        "not an object",
        {"icao24": "4ca1fa", "path": "not a list"},
    ],
)
def test_a_track_that_cannot_be_read_is_not_an_empty_path(body: object) -> None:
    with pytest.raises(Exception, match="upstream_unavailable"):
        decode_track(body, "4ca1fa")


def test_a_null_path_and_a_404_are_no_track() -> None:
    assert decode_track(None, "4ca1fa").waypoints == []
    assert decode_track({"icao24": "4ca1fa", "path": None}, "4ca1fa").received == 0


def test_a_waypoint_off_the_globe_is_unreadable() -> None:
    track = decode_track(
        {"path": [[START, 91.0, 0.0, 0, 0, False], [START, 0.0, 181.0], [START, 1.0, 2.0]]},
        "4ca1fa",
    )

    assert [(p.latitude, p.longitude) for p in track.waypoints] == [(1.0, 2.0)]
    assert track.unreadable == 2
    # A short row is a position with nothing else known, not zeros.
    assert track.waypoints[0].baro_altitude_m is None
    assert track.waypoints[0].on_ground is False
