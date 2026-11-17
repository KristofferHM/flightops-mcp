"""The second upstream adapter, against a mock of Schiphol's documented auth shape.

Two halves. The adapter alone: the token request names the audience, the token is
refreshed before it expires and never after, fifty callers make one token request,
a wrong audience is a credential failure and not an empty schedule. Then the same
adapter behind the authenticated server, which is the point of the exercise: the
scope check, the credit ledger, the passthrough guard and the error taxonomy are
the ones OpenSky goes through, with no code written for Schiphol in any of them.

No live Schiphol key is used anywhere in this file or this repository.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from flightops_mcp.auth import Scopes
from flightops_mcp.config import Settings
from flightops_mcp.errors import ToolFailure
from flightops_mcp.runtime import Runtime, set_runtime
from flightops_mcp.schiphol import (
    SchipholClient,
    amsterdam_offset,
    decode_flights,
    local_stamp,
)
from flightops_mcp.telemetry import REGISTRY
from services.mock_upstream.app import MockUpstreamConfig, create_app

from .authkit import JwksEndpoint, signing_key
from .conftest import FakeClock, SleepRecorder
from .harness import RemoteServer, remote_settings, serve, tool_payload
from .test_server_tools import install

KEY = signing_key("key-schiphol")
MOCK = "http://mock.test"
# Generated per test run: the mock refuses every client without one.
SECRET = "test-" + "x" * 24
# 2026-11-16 09:00 UTC, 10:00 in Amsterdam (CET).
NOW = datetime(2026, 11, 16, 9, 0, tzinfo=UTC).timestamp()


@dataclass(slots=True)
class Mock:
    app: Any
    transport: httpx.ASGITransport

    @property
    def stats(self) -> dict[str, int]:
        return dict(self.app.state.stats)

    @property
    def bearers(self) -> list[str]:
        bearers: list[str] = self.app.state.bearers
        return bearers


def mock(clock: FakeClock, *, ttl_s: int = 1800) -> Mock:
    app = create_app(
        MockUpstreamConfig(schiphol_client_secret=SECRET, schiphol_token_ttl_s=ttl_s),
        clock=clock,
    )
    return Mock(app=app, transport=httpx.ASGITransport(app=app))


def pointed_at_mock(settings: Settings, **changes: Any) -> Settings:
    return settings.model_copy(
        update={
            "schiphol_base_url": f"{MOCK}/schiphol/public",
            "schiphol_auth_url": f"{MOCK}/schiphol/oauth/token",
            "schiphol_client_id": "flightops-schiphol",
            "schiphol_client_secret": SECRET,
            **changes,
        }
    )


@pytest.fixture
def world(clock: FakeClock) -> Mock:
    clock.value = NOW
    return mock(clock)


@pytest.fixture
async def schiphol(settings: Settings, world: Mock, clock: FakeClock) -> AsyncIterator[Any]:
    client = SchipholClient(
        pointed_at_mock(settings),
        transport=world.transport,
        token_transport=world.transport,
        clock=clock,
    )
    try:
        yield client
    finally:
        await client.aclose()


# --- local time, by hand -----------------------------------------------------------
@pytest.mark.parametrize(
    ("instant", "hours"),
    [
        (datetime(2026, 3, 29, 0, 59, 59, tzinfo=UTC), 1),
        (datetime(2026, 3, 29, 1, 0, 0, tzinfo=UTC), 2),
        (datetime(2026, 10, 25, 0, 59, 59, tzinfo=UTC), 2),
        (datetime(2026, 10, 25, 1, 0, 0, tzinfo=UTC), 1),
        (datetime(2026, 12, 31, 23, 0, 0, tzinfo=UTC), 1),
        (datetime(2027, 3, 28, 1, 0, 0, tzinfo=UTC), 2),
    ],
)
def test_amsterdam_offset_follows_the_eu_rule_at_both_boundaries(
    instant: datetime, hours: int
) -> None:
    assert amsterdam_offset(instant).total_seconds() == hours * 3600


def test_the_window_is_sent_in_schiphols_local_format() -> None:
    assert local_stamp(NOW) == "2026-11-16T10:00:00"
    assert local_stamp(datetime(2026, 7, 1, 12, tzinfo=UTC).timestamp()) == "2026-07-01T14:00:00"


# --- the documented auth shape ---------------------------------------------------------
async def test_the_token_request_names_the_audience_and_the_call_uses_it(
    schiphol: SchipholClient, world: Mock
) -> None:
    schedule = await schiphol.schedule("departure", begin=NOW, end=NOW + 3600)

    assert schedule.flights, "the mock serves one flight every ten minutes"
    stats = world.stats
    assert stats["schiphol.tokens_issued"] == 1
    assert stats.get("schiphol.token_without_audience", 0) == 0
    assert stats.get("schiphol.token_wrong_audience", 0) == 0
    # The v4 path carries the version; the retired headers are never sent.
    assert stats.get("schiphol.legacy_headers", 0) == 0
    assert not any(key.startswith("schiphol.refused") for key in stats)


async def test_a_wrong_audience_is_a_credential_failure_not_an_empty_schedule(
    settings: Settings, world: Mock, clock: FakeClock
) -> None:
    client = SchipholClient(
        pointed_at_mock(settings, schiphol_audience="https://api.schiphol.nl/partner"),
        transport=world.transport,
        token_transport=world.transport,
        clock=clock,
    )
    try:
        with pytest.raises(ToolFailure) as excinfo:
            await client.schedule("arrival", begin=NOW, end=NOW + 3600)
    finally:
        await client.aclose()

    assert excinfo.value.code == "upstream_auth_failed"
    assert excinfo.value.details == {"upstream": "Schiphol auth", "status": 403}
    assert world.stats["schiphol.token_wrong_audience"] == 1
    assert world.stats.get("schiphol.flight_requests", 0) == 0


async def test_the_token_is_refreshed_sixty_seconds_before_expiry_and_never_used_after(
    settings: Settings, clock: FakeClock
) -> None:
    clock.value = NOW
    world = mock(clock, ttl_s=300)
    client = SchipholClient(
        pointed_at_mock(settings),
        transport=world.transport,
        token_transport=world.transport,
        clock=clock,
    )
    try:
        await client.schedule("arrival", begin=NOW, end=NOW + 900)
        clock.advance(239)  # 61 s left: still inside the margin
        await client.schedule("arrival", begin=NOW, end=NOW + 900)
        assert world.stats["schiphol.tokens_issued"] == 1
        clock.advance(2)  # 59 s left: refreshed before use, not after a 401
        await client.schedule("arrival", begin=NOW, end=NOW + 900)
        assert world.stats["schiphol.tokens_issued"] == 2
        clock.advance(10_000)  # long past the second token's expiry
        await client.schedule("arrival", begin=NOW, end=NOW + 900)
    finally:
        await client.aclose()

    assert world.stats["schiphol.tokens_issued"] == 3
    # Not one request reached the API with an expired token.
    assert world.stats.get("schiphol.refused.expired_token", 0) == 0


async def test_fifty_concurrent_callers_make_one_token_request(
    schiphol: SchipholClient, world: Mock
) -> None:
    await asyncio.gather(
        *(schiphol.schedule("arrival", begin=NOW, end=NOW + 600) for _ in range(50))
    )

    assert world.stats["schiphol.token_requests"] == 1
    assert world.stats["schiphol.flight_requests"] == 50


async def test_a_token_the_api_rejects_is_replaced_once(
    schiphol: SchipholClient, world: Mock
) -> None:
    await schiphol.schedule("arrival", begin=NOW, end=NOW + 600)
    world.app.state.stats.clear()
    # The API forgets every token, as after a key rotation at the vendor.
    schiphol._tokens._state.access_token = "revoked-at-the-vendor"  # noqa: SLF001

    schedule = await schiphol.schedule("arrival", begin=NOW, end=NOW + 600)

    assert schedule.flights
    assert world.stats["schiphol.refused.unknown_token"] == 1
    assert world.stats["schiphol.tokens_issued"] == 1


async def test_no_credentials_is_named_for_schiphol(settings: Settings) -> None:
    client = SchipholClient(
        settings.model_copy(update={"schiphol_client_id": None, "schiphol_client_secret": None})
    )
    try:
        with pytest.raises(ToolFailure) as excinfo:
            await client.schedule("arrival", begin=NOW, end=NOW + 600)
    finally:
        await client.aclose()

    assert excinfo.value.code == "credentials_missing"
    assert "FLIGHTOPS_SCHIPHOL_CLIENT_ID" in (excinfo.value.hint or "")
    assert excinfo.value.details == {"upstream": "Schiphol"}


# --- the documented response shape -------------------------------------------------------
async def test_pages_are_followed_by_the_link_header_up_to_the_cap(
    schiphol: SchipholClient, world: Mock
) -> None:
    # Twelve hours is 73 flights at one per ten minutes: four pages of 20.
    schedule = await schiphol.schedule("departure", begin=NOW, end=NOW + 12 * 3600)

    assert schedule.requests == 3
    assert len(schedule.flights) == 60
    assert schedule.truncated is True
    assert schiphol.requests_this_hour == 3


async def test_cancelled_diverted_and_delayed_are_read_from_states_and_estimates() -> None:
    page = decode_flights(
        {
            "flights": [
                {
                    "flightName": "KL1",
                    "flightDirection": "A",
                    "scheduleDateTime": "2026-11-16T10:00:00.000+01:00",
                    "estimatedLandingTime": "2026-11-16T10:20:00.000+01:00",
                    "publicFlightState": {"flightStates": ["EXP"]},
                },
                {
                    "flightName": "KL2",
                    "flightDirection": "A",
                    "scheduleDateTime": "2026-11-16T10:00:00.000+01:00",
                    "estimatedLandingTime": "2026-11-16T10:14:00.000+01:00",
                    "publicFlightState": {"flightStates": ["DIV"]},
                },
                {
                    "flightName": "KL3",
                    "flightDirection": "D",
                    "scheduleDateTime": "2026-11-16T10:00:00.000+01:00",
                    "publicFlightState": {"flightStates": ["CNX"]},
                    "route": {"destinations": ["LHR", 7]},
                },
            ]
        }
    )

    late, diverted, cancelled = page.flights
    assert (late.delayed, late.cancelled, late.diverted) == (True, False, False)
    assert (diverted.delayed, diverted.diverted) == (False, True)
    assert (cancelled.cancelled, cancelled.direction, cancelled.route) == (
        True,
        "departure",
        ("LHR",),
    )


def test_unreadable_rows_are_counted_never_dropped_silently() -> None:
    page = decode_flights(
        {
            "flights": [
                {
                    "flightName": "KL1",
                    "flightDirection": "A",
                    "scheduleDateTime": "2026-11-16T10:00:00.000+01:00",
                },
                {
                    "flightName": "KL2",
                    "flightDirection": "X",
                    "scheduleDateTime": "2026-11-16T10:00:00.000+01:00",
                },
                {"flightName": "KL3", "flightDirection": "D", "scheduleDateTime": "10:00"},
                {
                    "flightName": "KL4",
                    "flightDirection": "D",
                    "scheduleDateTime": "2026-11-16T10:00:00",
                },
                "not a flight",
            ]
        }
    )
    assert (len(page.flights), page.unreadable, page.received) == (1, 4, 5)


@pytest.mark.parametrize("body", [[], "flights", {"flights": {"a": 1}}])
def test_an_answer_that_is_not_the_documented_shape_is_unavailable_not_empty(body: Any) -> None:
    with pytest.raises(ToolFailure) as excinfo:
        decode_flights(body)
    assert excinfo.value.code == "upstream_unavailable"


async def test_a_long_rate_limit_is_data_with_the_wait_in_it(settings: Settings) -> None:
    """The same 429 contract as OpenSky (http.authorized_get), not a copy of it."""

    def api(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token"):
            return httpx.Response(200, json={"access_token": "t", "expires_in": 1800})
        return httpx.Response(429, headers={"Retry-After": "60"})

    sleep = SleepRecorder()
    client = SchipholClient(
        settings,
        transport=httpx.MockTransport(api),
        token_transport=httpx.MockTransport(api),
        sleep=sleep,
    )
    try:
        with pytest.raises(ToolFailure) as excinfo:
            await client.schedule("arrival", begin=NOW, end=NOW + 600)
    finally:
        await client.aclose()

    assert excinfo.value.code == "upstream_rate_limited"
    assert excinfo.value.retry_after_s == 60
    assert sleep.calls == []


# --- the governance layer, shared --------------------------------------------------------
@dataclass(slots=True)
class Served:
    remote: RemoteServer
    runtime: Runtime
    world: Mock


@pytest.fixture
def served_world(clock: FakeClock) -> Mock:
    clock.value = NOW
    return mock(clock)


@pytest.fixture
async def served(settings: Settings, served_world: Mock, clock: FakeClock) -> AsyncIterator[Served]:
    configured = pointed_at_mock(remote_settings(settings), schiphol_request_budget_per_hour=5)
    install(configured)  # builds the airport database
    runtime = Runtime.build(
        configured,
        schiphol_transport=served_world.transport,
        schiphol_token_transport=served_world.transport,
        clock=clock,
        data_clock=clock,
    )
    set_runtime(runtime)
    try:
        async with serve(configured, JwksEndpoint(KEY).transport) as remote:
            yield Served(remote=remote, runtime=runtime, world=served_world)
    finally:
        set_runtime(None)
        await runtime.aclose()


def spent(upstream: str) -> float:
    return (
        REGISTRY.get_sample_value("flightops_upstream_credits_spent_total", {"upstream": upstream})
        or 0.0
    )


async def test_a_scope_refusal_through_the_new_adapter_reaches_nothing_and_spends_nothing(
    served: Served,
) -> None:
    before = spent("schiphol")
    token = KEY.mint(scp=f"{Scopes.TRAFFIC_READ} {Scopes.REFERENCE_READ}")

    response = await served.remote.call_tool(
        "schiphol_flights", {"direction": "arrival"}, token=token
    )

    assert response.status_code == 403
    assert 'scope="flightops.schedule.read"' in response.headers["www-authenticate"]
    assert served.runtime.schiphol.requests_this_hour == 0
    assert spent("schiphol") == before
    assert served.world.stats == {}, "not even the token endpoint was asked"


async def test_a_call_through_the_new_adapter_debits_the_shared_ledger(served: Served) -> None:
    before = spent("schiphol")
    token = KEY.mint(scp=Scopes.SCHEDULE_READ)

    response = await served.remote.call_tool(
        "schiphol_flights", {"direction": "both", "window_minutes": 60}, token=token
    )

    payload = tool_payload(response)
    assert payload["ok"] is True
    assert payload["counts"]["arrival"]["listed"] == 7  # 10:00 to 11:00, every ten minutes
    assert payload["bookkeeping"]["credits_spent"] == 2
    assert served.runtime.schiphol.requests_this_hour == 2
    assert spent("schiphol") == before + 2
    assert payload["source"]["verification"].startswith("Adapter built to the vendor's")


async def test_the_shared_ledger_stops_the_new_adapter_with_the_same_error(
    served: Served,
) -> None:
    token = KEY.mint(scp=Scopes.SCHEDULE_READ)
    windows = (60, 90, 120)  # different arguments, so the result cache cannot answer
    payloads = []
    for window in windows:
        response = await served.remote.call_tool(
            "schiphol_flights", {"direction": "both", "window_minutes": window}, token=token
        )
        payloads.append(tool_payload(response))

    assert [p["ok"] for p in payloads] == [True, True, False]
    refusal = payloads[-1]["error"]
    assert refusal["code"] == "local_budget_exhausted"  # OpenSky's code, OpenSky's ledger
    assert refusal["details"] == {"credits_spent_this_hour": 5, "budget": 5}


async def test_the_callers_token_never_reaches_schiphol(served: Served) -> None:
    token = KEY.mint(scp=Scopes.SCHEDULE_READ)

    response = await served.remote.call_tool("schiphol_flights", {}, token=token)

    assert tool_payload(response)["ok"] is True
    assert served.world.bearers, "the mock saw the server's own bearer"
    assert all(token not in bearer for bearer in served.world.bearers)


async def test_a_repeat_is_served_from_the_cache_and_spends_nothing(served: Served) -> None:
    token = KEY.mint(scp=Scopes.SCHEDULE_READ)

    first = tool_payload(await served.remote.call_tool("schiphol_flights", {}, token=token))
    again = tool_payload(await served.remote.call_tool("schiphol_flights", {}, token=token))

    assert first["bookkeeping"]["credits_spent"] == 2
    assert again["cache"] == {"hit": True, "ttl_ms": 60_000, "scope": "public"}
    assert again["bookkeeping"]["credits_spent"] == 0
    assert served.world.stats["schiphol.flight_requests"] == 2
    assert served.runtime.schiphol.requests_this_hour == 2


async def test_upstream_status_reports_the_new_adapter(served: Served) -> None:
    token = KEY.mint(scp=Scopes.STATUS_READ)
    payload = tool_payload(await served.remote.call_tool("upstream_status", {}, token=token))

    assert payload["credentials"]["schiphol_configured"] is True
    assert payload["budget"]["schiphol_local_budget_per_hour"] == 5


@pytest.fixture(autouse=True)
def _no_runtime_left() -> Iterator[None]:
    yield
    set_runtime(None)
