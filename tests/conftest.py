"""Shared fixtures. No network, no real clock, no sleeps."""

from __future__ import annotations

import io
import json
import logging
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from hypothesis import HealthCheck
from hypothesis import settings as hypothesis_settings

from flightops_mcp.airports import build_from_csv
from flightops_mcp.config import Settings
from flightops_mcp.runtime import Runtime, set_runtime
from flightops_mcp.telemetry import configure_logging

FIXTURES = Path(__file__).parent / "fixtures"

# Property-test profiles (docs/QUALITY.md). Locally: hypothesis's default 100 examples,
# random each run, so a laptop keeps exploring. In CI: five times as many, derandomized,
# so a red build reproduces from the commit alone. Neither has a deadline: a test that
# fails because a disk was slow is a timing assertion, and this suite makes none.
# Chosen with HYPOTHESIS_PROFILE.
hypothesis_settings.register_profile("dev", max_examples=100, deadline=None)
hypothesis_settings.register_profile(
    "ci",
    max_examples=500,
    derandomize=True,
    deadline=None,
    print_blob=True,
    suppress_health_check=[HealthCheck.too_slow],
)
hypothesis_settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "dev"))


class FakeClock:
    """Monotonic clock the test drives by hand."""

    def __init__(self, start: float = 1_700_000_000.0) -> None:
        self.value = start

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class SleepRecorder:
    """Captures requested sleeps instead of performing them."""

    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


class LogCapture:
    """What actually shipped to the log stream, after the redaction processor ran."""

    def __init__(self, stream: io.StringIO) -> None:
        self._stream = stream

    @property
    def text(self) -> str:
        return self._stream.getvalue()

    def events(self, name: str | None = None) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        for line in self.text.splitlines():
            try:
                record = json.loads(line)
            except ValueError:
                continue  # a line from another library's logger, not ours
            if isinstance(record, dict) and (name is None or record.get("event") == name):
                records.append(record)
        return records


@pytest.fixture
def logs() -> Iterator[LogCapture]:
    """Capture emitted log lines through a real handler, as a log shipper would."""
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    configure_logging("INFO")
    root = logging.getLogger()
    root.addHandler(handler)
    try:
        yield LogCapture(stream)
    finally:
        root.removeHandler(handler)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def sleep() -> SleepRecorder:
    return SleepRecorder()


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        opensky_client_id="test-client",
        opensky_client_secret="test-secret",
        airports_db=tmp_path / "airports.sqlite3",
        backoff_base_s=0.0,
        max_retries=2,
        max_rate_limit_wait_s=5.0,
    )


@pytest.fixture
def airports_db(tmp_path: Path, settings: Settings) -> Path:
    db_path = tmp_path / "airports.sqlite3"
    build_from_csv(FIXTURES / "airports_sample.csv", db_path)
    return db_path


@pytest.fixture
def runtime(settings: Settings, airports_db: Path) -> Iterator[Any]:
    """A runtime wired to mock transports; tools never touch the network."""
    built = Runtime.build(
        settings,
        opensky_transport=httpx.MockTransport(_empty_opensky),
        token_transport=httpx.MockTransport(_token_handler),
        weather_transport=httpx.MockTransport(lambda request: httpx.Response(204)),
    )
    set_runtime(built)
    try:
        yield built
    finally:
        set_runtime(None)


def _empty_opensky(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"time": 1_700_000_000, "states": []})


def _token_handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"access_token": "token-abc", "expires_in": 1800})


@pytest.fixture(autouse=True)
def _reset_process_state() -> Iterator[None]:
    """Each test starts with closed breakers and an empty span sink.

    The breaker registry and the tracer provider are process-global by design (one
    breaker per upstream, one provider per process), so the suite must reset their
    *state* rather than their objects — OTel refuses to replace a provider.
    """
    from flightops_mcp import otel
    from flightops_mcp.resilience import configure_breakers, reset_breakers

    configure_breakers(failure_threshold=5, recovery_timeout_s=30.0)
    reset_breakers()
    otel.configure_tracing(exporter="memory")
    otel.reset_spans()
    yield
    otel.reset_spans()
