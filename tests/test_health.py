"""`/healthz`, `/readyz` and `/metrics` on the HTTP app.

Readiness deliberately does not gate on upstream health: a NOAA outage must not take
every replica out of rotation. These tests pin that distinction, because the
temptation to "make readyz stricter" is exactly how a provider blip becomes an
outage of your own making.
"""

from __future__ import annotations

import sqlite3
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from flightops_mcp import server as srv
from flightops_mcp.airports import build_from_csv
from flightops_mcp.config import Settings
from flightops_mcp.otel import configure_tracing
from flightops_mcp.resilience import configure_breakers, reset_breakers
from flightops_mcp.runtime import Runtime, set_runtime

from .conftest import FIXTURES, SleepRecorder


def make_runtime(settings: Settings) -> Runtime:
    if not settings.airports_db.is_file():
        build_from_csv(FIXTURES / "airports_sample.csv", settings.airports_db)
    runtime = Runtime.build(
        settings,
        opensky_transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"time": 1, "states": []})
        ),
        token_transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"access_token": "t", "expires_in": 1800})
        ),
        weather_transport=httpx.MockTransport(lambda request: httpx.Response(204)),
        sleep=SleepRecorder(),
    )
    set_runtime(runtime)
    return runtime


@pytest.fixture(autouse=True)
def _isolate() -> Any:
    configure_breakers(failure_threshold=5, recovery_timeout_s=30.0)
    reset_breakers()
    configure_tracing(exporter="memory")
    yield
    set_runtime(None)


async def app_client() -> AsyncIterator[httpx.AsyncClient]:
    server = srv.create_server()
    srv.install_observability_routes(server)
    app = server.streamable_http_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        yield client


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    async for value in app_client():
        yield value


async def test_healthz_is_liveness_only(settings: Settings, client: httpx.AsyncClient) -> None:
    make_runtime(settings)
    response = await client.get("/healthz")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["uptime_s"] >= 0
    assert "version" in body


async def test_readyz_is_ready_when_the_reference_data_loads(
    settings: Settings, client: httpx.AsyncClient
) -> None:
    make_runtime(settings)
    response = await client.get("/readyz")

    assert response.status_code == 200
    body = response.json()
    assert body["ready"] is True
    assert body["checks"]["reference_data"]["ok"] is True


async def test_readyz_fails_when_the_reference_database_is_missing(
    tmp_path: Path, client: httpx.AsyncClient
) -> None:
    settings = Settings(_env_file=None, airports_db=tmp_path / "missing.sqlite3")  # type: ignore[call-arg]
    runtime = Runtime.build(
        settings,
        opensky_transport=httpx.MockTransport(lambda request: httpx.Response(200)),
        token_transport=httpx.MockTransport(lambda request: httpx.Response(200)),
        weather_transport=httpx.MockTransport(lambda request: httpx.Response(204)),
        sleep=SleepRecorder(),
    )
    set_runtime(runtime)

    response = await client.get("/readyz")

    assert response.status_code == 503
    body = response.json()
    assert body["ready"] is False
    assert body["checks"]["reference_data"]["ok"] is False


_EMPTY_SCHEMA = """
CREATE TABLE airports (
    ident TEXT PRIMARY KEY, name TEXT, latitude REAL, longitude REAL, iata TEXT,
    type TEXT, municipality TEXT, iso_country TEXT, elevation_ft INTEGER,
    iso_region TEXT, scheduled_service TEXT
)
"""


async def test_an_empty_database_is_not_ready(tmp_path: Path, client: httpx.AsyncClient) -> None:
    db = tmp_path / "empty.sqlite3"
    with sqlite3.connect(db) as connection:
        connection.executescript(_EMPTY_SCHEMA)
    settings = Settings(_env_file=None, airports_db=db)  # type: ignore[call-arg]
    runtime = Runtime.build(
        settings,
        opensky_transport=httpx.MockTransport(lambda request: httpx.Response(200)),
        token_transport=httpx.MockTransport(lambda request: httpx.Response(200)),
        weather_transport=httpx.MockTransport(lambda request: httpx.Response(204)),
        sleep=SleepRecorder(),
    )
    set_runtime(runtime)

    response = await client.get("/readyz")
    assert response.status_code == 503, "an instance with no airports cannot serve a request"


async def test_metrics_exposes_tool_and_circuit_counters(
    settings: Settings, client: httpx.AsyncClient
) -> None:
    make_runtime(settings)
    # Drive a real tool call so the counter has a labelled series to expose.
    from flightops_mcp.runtime import get_runtime

    await srv.resolve_airport("ESGG")
    assert get_runtime().airports.exists

    response = await client.get("/metrics")

    assert response.status_code == 200
    assert "text/plain" in response.headers["content-type"]
    text = response.text
    assert "flightops_tool_calls_total" in text
    assert 'tool="resolve_airport"' in text
    assert "flightops_circuit_open" in text
    assert "flightops_upstream_requests_total" in text


async def test_observability_routes_need_no_token(client: httpx.AsyncClient) -> None:
    # A load balancer has no bearer token and Prometheus has no user identity; the
    # SDK documents custom_route for exactly this case.
    for path in ("/healthz", "/readyz", "/metrics"):
        response = await client.get(path)
        assert response.status_code in {200, 503}, path
