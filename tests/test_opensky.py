"""OpenSky client: credit accounting, 429 policy, 401 recovery, decoding."""

from __future__ import annotations

import httpx
import pytest

from flightops_mcp.config import Settings
from flightops_mcp.errors import ToolFailure
from flightops_mcp.opensky import BBox, OpenSkyClient, StateVector

from .conftest import SleepRecorder


def token_transport(requests: list[httpx.Request]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"access_token": "tok", "expires_in": 1800})

    return httpx.MockTransport(handler)


def client(
    settings: Settings,
    api_handler: object,
    *,
    sleep: SleepRecorder | None = None,
    token_calls: list[httpx.Request] | None = None,
) -> OpenSkyClient:
    calls = token_calls if token_calls is not None else []
    return OpenSkyClient(
        settings,
        transport=httpx.MockTransport(api_handler),  # type: ignore[arg-type]
        token_transport=token_transport(calls),
        sleep=sleep or SleepRecorder(),
    )


# --- bounding boxes and credit cost -----------------------------------------
@pytest.mark.parametrize(
    ("lamax", "lomax", "expected"),
    [
        (5.0, 1.0, 1),  # 5 sq deg
        (5.0, 5.0, 1),  # 25 sq deg, top of band 1
        (5.0, 5.02, 2),  # 25.1 sq deg, bottom of band 2
        (20.0, 5.0, 2),  # 100 sq deg, top of band 2
        (20.0, 5.005, 3),  # 100.1 sq deg
        (40.0, 10.0, 3),  # 400 sq deg, top of band 3
        (40.0, 10.01, 4),  # above 400 sq deg
    ],
)
def test_credit_cost_matches_the_documented_table(
    lamax: float, lomax: float, expected: int
) -> None:
    bbox = BBox(lamin=0.0, lomin=0.0, lamax=lamax, lomax=lomax)
    assert bbox.credit_cost == expected


def test_a_40nm_box_around_gothenburg_costs_one_credit() -> None:
    bbox = BBox.around(57.6628, 12.2798, 40.0)
    assert bbox.credit_cost == 1
    assert bbox.area_sq_deg <= 25


def test_bbox_rejects_inverted_coordinates() -> None:
    with pytest.raises(ValueError, match="lamin"):
        BBox(lamin=10.0, lomin=0.0, lamax=5.0, lomax=1.0)
    with pytest.raises(ValueError, match="out of range"):
        BBox(lamin=-95.0, lomin=0.0, lamax=5.0, lomax=1.0)


# --- state vectors -----------------------------------------------------------
def test_state_vector_decoding_handles_nulls() -> None:
    row = [
        "4ca1fa",
        "SAS123  ",
        "Sweden",
        None,
        1_700_000_010,
        12.28,
        57.66,
        3000.0,
        False,
        220.0,
        90.0,
        -2.5,
        None,
        3100.0,
        "1000",
        False,
        0,
    ]
    vector = StateVector.from_row(row)

    assert vector.icao24 == "4ca1fa"
    assert vector.callsign == "SAS123", "callsigns arrive space-padded"
    assert vector.on_ground is False
    payload = vector.to_payload()
    assert payload["baro_altitude_m"] == 3000.0


def test_short_rows_do_not_explode() -> None:
    vector = StateVector.from_row(["abc123"])
    assert vector.icao24 == "abc123"
    assert vector.latitude is None


async def test_states_are_sorted_and_truncated_to_the_cap(settings: Settings) -> None:
    rows = [
        ["a1", "ONE", "SE", None, 100, 1.0, 1.0, None, False, None, None, None],
        ["a2", "TWO", "SE", None, 300, 1.0, 1.0, None, False, None, None, None],
        ["a3", "THREE", "SE", None, 200, 1.0, 1.0, None, False, None, None, None],
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"time": 1_700_000_000, "states": rows},
            headers={"X-Rate-Limit-Remaining": "3999"},
        )

    async with client(settings, handler) as opensky:
        snapshot = await opensky.states_in_bbox(BBox.around(57.66, 12.28, 20.0), max_vectors=2)

    assert [vector.icao24 for vector in snapshot.vectors] == ["a2", "a3"]
    assert snapshot.truncated is True
    assert snapshot.credits_spent == 1
    assert snapshot.credits_remaining == 3999
    assert "3 aircraft seen" in snapshot.notes[0]


async def test_empty_states_is_not_an_error(settings: Settings) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"time": 1, "states": None})

    async with client(settings, handler) as opensky:
        snapshot = await opensky.states_in_bbox(BBox.around(57.66, 12.28, 20.0))

    assert snapshot.vectors == []
    assert snapshot.truncated is False


# --- rate limiting -----------------------------------------------------------
async def test_short_rate_limit_wait_is_honoured_then_retried(
    settings: Settings, sleep: SleepRecorder
) -> None:
    responses = [
        httpx.Response(429, headers={"X-Rate-Limit-Retry-After-Seconds": "2"}, json={}),
        httpx.Response(200, json={"time": 1, "states": []}),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return responses.pop(0)

    async with client(settings, handler, sleep=sleep) as opensky:
        snapshot = await opensky.states_in_bbox(BBox.around(57.66, 12.28, 20.0))

    assert sleep.calls == [2.0], "the upstream's own number is used, not a guess"
    assert snapshot.vectors == []


async def test_long_rate_limit_wait_becomes_a_tool_error_not_a_blocked_call(
    settings: Settings, sleep: SleepRecorder
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"X-Rate-Limit-Retry-After-Seconds": "900"}, json={})

    async with client(settings, handler, sleep=sleep) as opensky:
        with pytest.raises(ToolFailure) as excinfo:
            await opensky.states_in_bbox(BBox.around(57.66, 12.28, 20.0))

    assert excinfo.value.code == "upstream_rate_limited"
    assert excinfo.value.retry_after_s == 900.0
    assert sleep.calls == [], "a 15-minute wait must not be absorbed inside the tool call"


async def test_expired_token_is_refreshed_and_the_call_retried(settings: Settings) -> None:
    token_calls: list[httpx.Request] = []
    api_calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        api_calls.append(request.headers["Authorization"])
        if len(api_calls) == 1:
            return httpx.Response(401, json={"errors": [{"code": 38192}]})
        return httpx.Response(200, json={"time": 1, "states": []})

    async with client(settings, handler, token_calls=token_calls) as opensky:
        snapshot = await opensky.states_in_bbox(BBox.around(57.66, 12.28, 20.0))

    assert snapshot.vectors == []
    assert len(api_calls) == 2
    assert len(token_calls) == 2, "a 401 must invalidate the cache and force a new token"


async def test_persistent_401_surfaces_as_an_auth_failure(settings: Settings) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={})

    async with client(settings, handler) as opensky:
        with pytest.raises(ToolFailure) as excinfo:
            await opensky.states_in_bbox(BBox.around(57.66, 12.28, 20.0))

    assert excinfo.value.code == "upstream_auth_failed"


# --- local budget ------------------------------------------------------------
async def test_local_credit_budget_stops_a_runaway_loop(settings: Settings) -> None:
    bounded = settings.model_copy(update={"credit_budget_per_hour": 2})

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"time": 1, "states": []})

    async with client(bounded, handler) as opensky:
        bbox = BBox.around(57.66, 12.28, 20.0)
        await opensky.states_in_bbox(bbox)
        await opensky.states_in_bbox(bbox)
        with pytest.raises(ToolFailure) as excinfo:
            await opensky.states_in_bbox(bbox)

    assert excinfo.value.code == "local_budget_exhausted"
    assert excinfo.value.details["credits_spent_this_hour"] == 2


# --- movements ---------------------------------------------------------------
async def test_movements_are_decoded(settings: Settings) -> None:
    payload = {
        "begin": 1,
        "end": 2,
        "arrival": [
            {
                "icao24": "4ca1fa",
                "callsign": "SAS123  ",
                "firstSeen": 1,
                "lastSeen": 2,
                "estDepartureAirport": "ESSA",
                "estArrivalAirport": "ESGG",
            }
        ],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert "/flights/arrival" in str(request.url)
        return httpx.Response(200, json=payload)

    async with client(settings, handler) as opensky:
        movements = await opensky.movements("ESGG", "arrival", begin=1, end=2)

    assert movements[0].callsign == "SAS123"
    assert movements[0].est_departure_airport == "ESSA"


async def test_unknown_direction_is_rejected_before_spending_credits(settings: Settings) -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("must not reach the network")

    async with client(settings, handler) as opensky:
        with pytest.raises(ToolFailure) as excinfo:
            await opensky.movements("ESGG", "sideways", begin=1, end=2)

    assert excinfo.value.code == "invalid_argument"
    assert opensky.credits_spent_total == 0


async def test_404_on_movements_is_an_empty_list_not_a_failure(settings: Settings) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={})

    async with client(settings, handler) as opensky:
        assert await opensky.movements("ZZZZ", "arrival", begin=1, end=2) == []
