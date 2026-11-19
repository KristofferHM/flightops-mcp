"""The three ways of reaching OpenSky, the daily guard, and the wide snapshot.

The owner's constraint is tested literally: credentials arrive as lines in a `.env`
file, and nothing else changes between the test's two halves.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from mcp import Client

from flightops_mcp import server as srv
from flightops_mcp.airports import build_from_csv
from flightops_mcp.config import Settings
from flightops_mcp.opensky import AIRSPACE_REGIONS, credentials_mode, daily_metered
from flightops_mcp.runtime import Runtime, set_runtime
from services.mock_upstream.dated import dated

from .conftest import FIXTURES, FakeClock, LogCapture, SleepRecorder

NOW = int(datetime(2026, 11, 14, 23, 30, tzinfo=UTC).timestamp())
METAR = [
    {
        "icaoId": "ESGG",
        "reportTime": "2026-11-14T23:20:00Z",
        "rawOb": "ESGG 142320Z 24008KT 9999 BKN030 08/05 Q1008",
        "fltCat": "VFR",
    }
]


def row(icao24: str = "abc123", *, lat: float | None = 57.7) -> list[Any]:
    return [icao24, "SAS1 ", "Sweden", NOW, NOW, 12.0, lat, 3000.0, False, 200.0, 90.0, 0.0]


@dataclass
class FakeOpenSky:
    """Records what reached the upstream, and answers like it."""

    states: list[list[Any]] = field(default_factory=lambda: [row()])
    remaining: str = "3990"
    status: int = 200
    requests: list[httpx.Request] = field(default_factory=list)
    token_requests: int = 0

    def api(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        headers = {"X-Rate-Limit-Remaining": self.remaining}
        if self.status != 200:
            return httpx.Response(self.status, headers=headers)
        if "/flights/" in request.url.path:
            return httpx.Response(200, json=[], headers=headers)
        return httpx.Response(200, json={"time": NOW, "states": self.states}, headers=headers)

    def token(self, _request: httpx.Request) -> httpx.Response:
        self.token_requests += 1
        return httpx.Response(200, json={"access_token": "fake-token", "expires_in": 1800})

    @property
    def authorization_headers(self) -> list[str | None]:
        return [request.headers.get("authorization") for request in self.requests]


@pytest.fixture(autouse=True)
def _clear_runtime() -> Iterator[None]:
    yield
    set_runtime(None)


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("CLIENT_ID", "CLIENT_SECRET", "ANONYMOUS"):
        monkeypatch.delenv(f"FLIGHTOPS_OPENSKY_{name}", raising=False)


def make_settings(tmp_path: Path, **overrides: Any) -> Settings:
    database = tmp_path / "airports.sqlite3"
    if not database.is_file():
        build_from_csv(FIXTURES / "airports_sample.csv", database)
    values: dict[str, Any] = {
        "opensky_client_id": None,
        "opensky_client_secret": None,
        "airports_db": database,
        "backoff_base_s": 0.0,
        "max_retries": 0,
    }
    return Settings(_env_file=None, **{**values, **overrides})  # type: ignore[call-arg]


def install(settings: Settings, upstream: FakeOpenSky, clock: Any = None) -> Runtime:
    clock = clock or (lambda: float(NOW))
    runtime = Runtime.build(
        settings,
        opensky_transport=httpx.MockTransport(dated(upstream.api, clock)),
        token_transport=httpx.MockTransport(upstream.token),
        weather_transport=httpx.MockTransport(
            dated(
                lambda r: httpx.Response(200, json=METAR if r.url.path.endswith("/metar") else []),
                clock,
            )
        ),
        sleep=SleepRecorder(),
        clock=clock,
        data_clock=clock,
    )
    set_runtime(runtime)
    return runtime


def authenticated(tmp_path: Path, **overrides: Any) -> Settings:
    credentials = {"opensky_client_id": "id", "opensky_client_secret": "secret"}
    return make_settings(tmp_path, **{**credentials, **overrides})


# --- credentials are configuration ---------------------------------------------------
@pytest.mark.usefixtures("clean_env")
async def test_adding_credentials_is_a_dotenv_edit_and_nothing_else(tmp_path: Path) -> None:
    database = tmp_path / "airports.sqlite3"
    build_from_csv(FIXTURES / "airports_sample.csv", database)
    dotenv = tmp_path / ".env"
    dotenv.write_text(f"FLIGHTOPS_AIRPORTS_DB={database.as_posix()}\n", encoding="utf-8")

    before = FakeOpenSky()
    runtime = install(Settings(_env_file=dotenv), before)  # type: ignore[call-arg]
    refused = await srv.airport_live_traffic("ESGG")
    status = await srv.upstream_status()

    assert refused["ok"] is False
    assert refused["error"]["code"] == "credentials_missing"
    assert status["credentials"]["opensky"] == "none"
    assert before.requests == [], "no credentials, so nothing may reach the upstream"
    assert runtime.opensky.credits_spent_this_hour == 0, "a call never sent spends nothing"

    # The restart: the same code, the same file with two more lines.
    with dotenv.open("a", encoding="utf-8") as handle:
        handle.write("FLIGHTOPS_OPENSKY_CLIENT_ID=later-id\n")
        handle.write("FLIGHTOPS_OPENSKY_CLIENT_SECRET=later-secret\n")
    after = FakeOpenSky()
    install(Settings(_env_file=dotenv), after)  # type: ignore[call-arg]
    live = await srv.airport_live_traffic("ESGG")
    status = await srv.upstream_status()

    assert live["ok"] is True
    assert live["credentials"] == "client_credentials"
    assert live["counts"]["returned"] == 1
    assert after.authorization_headers == ["Bearer fake-token"]
    assert status["credentials"]["opensky"] == "client_credentials"
    assert status["budget"]["upstream_remaining_last_seen"] == 3990


# --- the three modes -----------------------------------------------------------------
def test_anonymous_is_off_unless_asked_for(tmp_path: Path) -> None:
    assert make_settings(tmp_path).opensky_anonymous is False
    assert credentials_mode(make_settings(tmp_path)) == "none"
    assert credentials_mode(make_settings(tmp_path, opensky_anonymous=True)) == "anonymous"


@pytest.mark.parametrize(
    ("client_id", "secret"), [("id", None), (None, "secret"), ("", ""), ("id", "")]
)
def test_half_a_credential_is_no_credential(
    tmp_path: Path, client_id: str | None, secret: str | None
) -> None:
    settings = make_settings(tmp_path, opensky_client_id=client_id, opensky_client_secret=secret)
    assert credentials_mode(settings) == "none"


def test_real_credentials_win_over_the_anonymous_flag(tmp_path: Path) -> None:
    assert credentials_mode(authenticated(tmp_path, opensky_anonymous=True)) == (
        "client_credentials"
    )


async def test_anonymous_traffic_sends_no_token_and_admits_its_limits(tmp_path: Path) -> None:
    upstream = FakeOpenSky(remaining="394")
    install(make_settings(tmp_path, opensky_anonymous=True), upstream)

    payload = await srv.airport_live_traffic("ESGG")
    status = await srv.upstream_status()

    assert payload["ok"] is True
    assert upstream.authorization_headers == [None]
    assert upstream.token_requests == 0
    assert payload["credentials"] == "anonymous"
    assert payload["freshness"] == {
        "credentials": "anonymous",
        "time_resolution_s": 10,
        "most_recent_only": True,
        "time_parameter_honoured": False,
        "upstream_daily_credits": 400,
    }
    assert any("10-second resolution" in item for item in payload["unknowns"])
    assert status["credentials"]["opensky"] == "anonymous"
    assert status["credentials"]["opensky_configured"] is False
    assert status["budget"]["credits_spent_by_mode"] == {"anonymous": 1}
    assert status["budget"]["upstream_remaining_last_seen"] == 394


async def test_an_authenticated_answer_makes_no_anonymous_caveat(tmp_path: Path) -> None:
    install(authenticated(tmp_path), FakeOpenSky())

    payload = await srv.airport_live_traffic("ESGG")

    assert payload["freshness"]["time_resolution_s"] == 5
    assert payload["freshness"]["most_recent_only"] is False
    assert payload["unknowns"] == []


async def test_anonymous_mode_never_calls_flights_and_cannot_triage_to_low(
    tmp_path: Path,
) -> None:
    upstream = FakeOpenSky()
    runtime = install(make_settings(tmp_path, opensky_anonymous=True), upstream)

    movements = await srv.airport_movements("ESGG")
    triage = await srv.disruption_triage("ESGG")

    assert movements["ok"] is True
    assert movements["credentials"] == "anonymous"
    assert movements["movements"] == {}
    assert not [r for r in upstream.requests if "/flights/" in r.url.path]
    assert runtime.opensky.credits_spent_this_hour == 1, "only the traffic call was charged"
    assert triage["credentials"] == "anonymous"
    assert "movements:not_fetched_anonymously" in triage["degraded"]
    assert triage["assessment"]["severity"] == "unknown"


async def test_no_credentials_triage_keeps_its_explicit_failure(tmp_path: Path) -> None:
    upstream = FakeOpenSky()
    install(make_settings(tmp_path), upstream)

    triage = await srv.disruption_triage("ESGG")

    assert triage["credentials"] == "none"
    assert "traffic:credentials_missing" in triage["degraded"]
    assert upstream.requests == []


async def test_a_rejected_anonymous_call_is_final(tmp_path: Path) -> None:
    upstream = FakeOpenSky(status=401)
    install(make_settings(tmp_path, opensky_anonymous=True), upstream)

    payload = await srv.airport_live_traffic("ESGG")

    assert payload["error"]["code"] == "upstream_auth_failed"
    assert len(upstream.requests) == 1, "there is no token to refresh, so nothing to retry"


def test_the_mode_is_logged_at_startup_without_the_credential(
    tmp_path: Path, logs: LogCapture
) -> None:
    srv.log_opensky_mode(
        make_settings(tmp_path, opensky_client_id="visible-id", opensky_client_secret=None)
    )
    srv.log_opensky_mode(authenticated(tmp_path, opensky_client_secret="canary-secret-value"))

    first, second = logs.events("opensky_mode")
    assert (first["credentials"], first["partly_configured"]) == ("none", True)
    assert (second["credentials"], second["partly_configured"]) == ("client_credentials", False)
    assert "canary-secret-value" not in logs.text
    assert "visible-id" not in logs.text


# --- airspace_snapshot ---------------------------------------------------------------
@pytest.mark.parametrize("region", sorted(AIRSPACE_REGIONS))
async def test_every_region_costs_the_top_tier(tmp_path: Path, region: str) -> None:
    upstream = FakeOpenSky()
    runtime = install(authenticated(tmp_path), upstream)

    payload = await srv.airspace_snapshot(region)

    assert payload["ok"] is True
    assert payload["bookkeeping"]["credits_spent"] == 4
    assert payload["bookkeeping"]["spent_with"] == "client_credentials"
    assert runtime.opensky.credits_spent_this_hour == 4
    assert runtime.opensky.credits_spent_24h == 4
    assert dict(upstream.requests[0].url.params) == {
        name: str(value) for name, value in AIRSPACE_REGIONS[region].as_params().items()
    }


async def test_the_snapshot_is_compact_and_says_where_it_came_from(tmp_path: Path) -> None:
    install(authenticated(tmp_path), FakeOpenSky())

    payload = await srv.airspace_snapshot("Europe ")

    assert payload["region"] == "europe"
    assert payload["credentials"] == "client_credentials"
    assert payload["snapshot_current"] is True
    aircraft = dict(zip(payload["columns"], payload["rows"][0], strict=True))
    assert aircraft["icao24"] == "abc123"
    assert (aircraft["latitude"], aircraft["longitude"]) == (57.7, 12.0)
    assert payload["truncated"] is False
    assert payload["unknowns"] == []
    assert "OpenSky" in payload["source"]["name"]
    assert "non-profit" in payload["source"]["licence"]


async def test_a_free_form_region_is_refused_before_anything_is_spent(tmp_path: Path) -> None:
    upstream = FakeOpenSky()
    runtime = install(authenticated(tmp_path), upstream)

    payload = await srv.airspace_snapshot("10,20,30,40")

    assert payload["error"]["code"] == "invalid_argument"
    assert upstream.requests == []
    assert runtime.opensky.credits_spent_this_hour == 0


async def test_a_repeat_inside_the_minute_is_free_and_says_so(tmp_path: Path) -> None:
    clock = FakeClock(float(NOW))
    upstream = FakeOpenSky()
    runtime = install(authenticated(tmp_path), upstream, clock)

    await srv.airspace_snapshot("world")
    clock.advance(59)
    repeat = await srv.airspace_snapshot("world")
    status = await srv.upstream_status()

    assert len(upstream.requests) == 1
    assert repeat["cache"]["hit"] is True
    assert repeat["bookkeeping"]["credits_spent"] == 0
    assert runtime.opensky.credits_spent_24h == 4
    assert status["cache"]["hits"] == 1
    assert status["cache"]["misses"] == 1
    assert status["cache"]["hit_rate"] == 0.5

    clock.advance(2)
    await srv.airspace_snapshot("world")
    assert len(upstream.requests) == 2


async def test_truncation_and_missing_positions_are_stated(tmp_path: Path) -> None:
    states = [row("aaa111"), row("bbb222", lat=None), row("ccc333")]
    install(authenticated(tmp_path), FakeOpenSky(states=states))

    capped = await srv.airspace_snapshot("europe", max_aircraft=2)
    whole = await srv.airspace_snapshot("world")

    assert capped["truncated"] is True
    assert any("most recently heard" in item for item in capped["unknowns"])
    assert whole["counts"] == {"received": 3, "returned": 2}
    assert any("no position" in item for item in whole["unknowns"])


async def test_an_empty_sky_is_not_reported_as_one(tmp_path: Path) -> None:
    install(authenticated(tmp_path), FakeOpenSky(states=[]))

    payload = await srv.airspace_snapshot("europe")

    assert payload["rows"] == []
    assert any("No aircraft heard in this region" in item for item in payload["unknowns"])


async def test_the_snapshot_follows_the_credential_mode(tmp_path: Path) -> None:
    install(make_settings(tmp_path), FakeOpenSky())
    refused = await srv.airspace_snapshot("europe")
    assert refused["error"]["code"] == "credentials_missing"

    upstream = FakeOpenSky()
    install(make_settings(tmp_path, opensky_anonymous=True), upstream)
    anonymous = await srv.airspace_snapshot("europe")
    status = await srv.upstream_status()

    assert anonymous["credentials"] == "anonymous"
    assert anonymous["bookkeeping"]["spent_with"] == "anonymous"
    assert anonymous["freshness"]["time_resolution_s"] == 10
    assert any("10-second resolution" in item for item in anonymous["unknowns"])
    assert status["budget"]["credits_spent_by_mode"] == {"anonymous": 4}


async def test_the_description_states_the_cost_and_the_regions(tmp_path: Path) -> None:
    install(authenticated(tmp_path), FakeOpenSky())
    async with Client(srv.mcp) as client:
        tools = await client.list_tools()

    (tool,) = [t for t in tools.tools if t.name == "airspace_snapshot"]
    assert "4 OpenSky credits" in (tool.description or "")
    for region in AIRSPACE_REGIONS:
        assert region in (tool.description or "")


# --- the daily guard -----------------------------------------------------------------
async def test_the_daily_guard_refuses_and_charges_neither_ledger(tmp_path: Path) -> None:
    upstream = FakeOpenSky()
    runtime = install(authenticated(tmp_path, dashboard_daily_credit_budget=4), upstream)

    assert (await srv.airspace_snapshot("europe"))["ok"] is True
    refused = await srv.airspace_snapshot("world")
    status = await srv.upstream_status()

    assert refused["error"]["code"] == "local_budget_exhausted"
    assert refused["error"]["details"] == {"credits_spent_24h": 4, "budget": 4}
    assert "FLIGHTOPS_DASHBOARD_DAILY_CREDIT_BUDGET" in refused["error"]["hint"]
    assert len(upstream.requests) == 1
    assert runtime.opensky.credits_spent_this_hour == 4
    assert status["budget"]["dashboard_credits_spent_24h"] == 4
    assert status["budget"]["dashboard_budget_per_24h"] == 4
    assert status["budget"]["daily_ledger_survives_restart"] is False


async def test_the_hourly_guard_refusal_leaves_the_daily_ledger_alone(tmp_path: Path) -> None:
    runtime = install(authenticated(tmp_path, credit_budget_per_hour=3), FakeOpenSky())

    refused = await srv.airspace_snapshot("europe")

    assert refused["error"]["code"] == "local_budget_exhausted"
    assert runtime.opensky.credits_spent_24h == 0


async def test_daily_spend_leaves_the_window_after_a_day(tmp_path: Path) -> None:
    clock = FakeClock(float(NOW))
    upstream = FakeOpenSky()
    install(authenticated(tmp_path, dashboard_daily_credit_budget=4), upstream, clock)

    await srv.airspace_snapshot("europe")
    clock.advance(86_401)

    assert (await srv.airspace_snapshot("world"))["ok"] is True


async def test_an_agent_outside_the_dashboard_is_not_held_to_its_budget(tmp_path: Path) -> None:
    runtime = install(authenticated(tmp_path, dashboard_daily_credit_budget=0), FakeOpenSky())

    assert (await srv.airport_live_traffic("ESGG"))["ok"] is True
    assert runtime.opensky.credits_spent_24h == 0

    with daily_metered():
        metered = await srv.airport_live_traffic("ESGG", radius_nm=41.0)
    assert metered["error"]["code"] == "local_budget_exhausted"
