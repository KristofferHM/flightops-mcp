"""adsb.lol's /v2/point, decoded into the traffic snapshot every tool already reads."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from flightops_mcp.adsblol import (
    MAX_RADIUS_NM,
    AdsbLolClient,
    covering_radius_nm,
    decode_point,
)
from flightops_mcp.config import Settings
from flightops_mcp.errors import ToolFailure
from flightops_mcp.opensky import BBox

from .conftest import SleepRecorder

BOX = BBox.around(57.6628, 12.2798, 40.0)
NOW_MS = 1_763_640_000_000
EMPTY = {"ac": [], "now": NOW_MS}


def row(**fields: Any) -> dict[str, Any]:
    return {"hex": "4aca7e", "lat": 57.7, "lon": 12.3, "seen": 0.0, **fields}


def test_rows_outside_the_box_and_rows_that_are_not_aircraft_are_kept_apart() -> None:
    body = {
        "ac": [
            row(),
            row(hex="48aed2", lat=60.0, lon=20.0),  # in adsb.lol's circle, outside the box
            row(hex="48aed3", lat=None, lon=None),  # no position
            {"hex": "not-hex"},
            "garbage",
        ],
        "now": NOW_MS,
    }

    snapshot = decode_point(body, bbox=BOX, max_vectors=10)

    assert [v.icao24 for v in snapshot.vectors] == ["4aca7e"]
    assert snapshot.unreadable == 2
    assert snapshot.received == 3
    assert snapshot.time == NOW_MS // 1000
    assert "2 aircraft heard in the circle" in snapshot.notes[0]


def test_the_most_recently_heard_are_kept_when_capped() -> None:
    body = {"ac": [row(hex=f"4aca7{i}", seen=float(i)) for i in range(5)], "now": NOW_MS}

    snapshot = decode_point(body, bbox=BOX, max_vectors=2)

    assert [v.icao24 for v in snapshot.vectors] == ["4aca70", "4aca71"]
    assert snapshot.truncated is True


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        ({"alt_baro": "ground"}, (None, True)),
        ({"alt_baro": 1000}, (304.8, False)),
        ({"alt_baro": None}, (None, False)),
        ({"alt_baro": True}, (None, False)),
        ({"alt_baro": float("nan")}, (None, False)),
    ],
)
def test_altitude_is_metres_or_unknown_and_ground_is_ground(
    fields: dict[str, Any], expected: tuple[float | None, bool]
) -> None:
    body = {"ac": [row(**fields)], "now": NOW_MS}
    (vector,) = decode_point(body, bbox=BOX, max_vectors=5).vectors
    assert (vector.baro_altitude_m, vector.on_ground) == expected


def test_no_clock_means_no_snapshot_time_and_no_last_contact() -> None:
    snapshot = decode_point({"ac": [row(flight="   ")], "now": "soon"}, bbox=BOX, max_vectors=5)
    assert snapshot.time is None
    assert snapshot.vectors[0].last_contact is None
    assert snapshot.vectors[0].callsign is None


@pytest.mark.parametrize("body", [None, [], {"ac": "many"}])
def test_an_answer_that_is_not_a_point_answer_is_unavailable_not_empty(body: Any) -> None:
    with pytest.raises(ToolFailure) as failed:
        decode_point(body, bbox=BOX, max_vectors=5)
    assert failed.value.code == "upstream_unavailable"


def test_a_null_aircraft_list_is_an_empty_sky() -> None:
    assert decode_point({"ac": None, "now": NOW_MS}, bbox=BOX, max_vectors=5).vectors == []


def settings(**overrides: Any) -> Settings:
    values = {"adsblol_base_url": "https://adsblol.test", "backoff_base_s": 0.0, "max_retries": 0}
    return Settings(_env_file=None, **{**values, **overrides})  # type: ignore[call-arg]


async def test_the_key_is_a_header_and_the_circle_covers_the_box() -> None:
    seen: list[httpx.Request] = []

    def answer(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"ac": [row()], "now": NOW_MS})

    client = AdsbLolClient(settings(adsblol_api_key="k-123"), transport=httpx.MockTransport(answer))
    snapshot = await client.states_in_bbox(BOX)
    await client.aclose()

    assert seen[0].headers["x-api-key"] == "k-123"
    assert seen[0].url.path == f"/v2/point/57.6628/12.2798/{int(covering_radius_nm(BOX)) + 1}"
    assert client.credentials_mode == "api_key"
    assert client.freshness()["upstream_daily_requests"] is None
    assert len(snapshot.vectors) == 1


async def test_a_box_wider_than_adsblol_answers_says_so() -> None:
    wide = BBox.around(57.0, 12.0, 200.0)
    client = AdsbLolClient(
        settings(),
        transport=httpx.MockTransport(lambda _r: httpx.Response(200, json=EMPTY)),
    )
    snapshot = await client.states_in_bbox(wide)
    await client.aclose()

    assert covering_radius_nm(wide) > MAX_RADIUS_NM
    assert any("at most 250 nm" in note for note in snapshot.notes)
    assert client.freshness()["upstream_daily_requests"] == 500


async def test_the_local_ceiling_refuses_before_a_request_is_made() -> None:
    calls: list[str] = []

    def answer(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(200, json={"ac": [], "now": NOW_MS})

    client = AdsbLolClient(
        settings(adsblol_requests_per_hour=1), transport=httpx.MockTransport(answer)
    )
    await client.states_in_bbox(BOX)
    with pytest.raises(ToolFailure) as refused:
        await client.states_in_bbox(BOX)
    await client.aclose()

    assert refused.value.code == "local_budget_exhausted"
    assert "FLIGHTOPS_ADSBLOL_REQUESTS_PER_HOUR" in (refused.value.hint or "")
    assert len(calls) == 1
    assert client.requests_this_hour == 1 and client.budget_per_hour == 1


async def test_over_quota_is_rate_limited_with_the_wait() -> None:
    client = AdsbLolClient(
        settings(),
        transport=httpx.MockTransport(
            lambda r: httpx.Response(429, headers={"Retry-After": "3600"})
        ),
        sleep=SleepRecorder(),
    )
    with pytest.raises(ToolFailure) as limited:
        await client.states_in_bbox(BOX)
    await client.aclose()
    assert limited.value.code == "upstream_rate_limited"
