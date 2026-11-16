"""The circuit breaker: fail fast when an upstream is down, recover when it is not."""

from __future__ import annotations

import httpx
import pytest

from flightops_mcp.config import Settings
from flightops_mcp.errors import ToolFailure
from flightops_mcp.http import UpstreamHttp
from flightops_mcp.resilience import (
    CircuitBreaker,
    breakers,
    configure_breakers,
    reset_breakers,
)
from flightops_mcp.telemetry import REGISTRY

from .conftest import FakeClock, SleepRecorder


def breaker(threshold: int = 3, recovery: float = 30.0) -> tuple[CircuitBreaker, FakeClock]:
    clock = FakeClock()
    return (
        CircuitBreaker(
            name="unit", failure_threshold=threshold, recovery_timeout_s=recovery, clock=clock
        ),
        clock,
    )


def test_the_circuit_opens_at_the_threshold() -> None:
    cb, _ = breaker(threshold=2)
    assert cb.state == "closed"
    cb.record_failure()
    assert cb.state == "closed", "one failure is a blip"
    cb.record_failure()
    assert cb.state == "open"
    assert cb.consecutive_failures == 2


def test_an_open_circuit_refuses_with_a_retry_hint() -> None:
    cb, _ = breaker(threshold=1, recovery=30.0)
    cb.record_failure()

    with pytest.raises(ToolFailure) as excinfo:
        cb.before_call()

    assert excinfo.value.code == "upstream_circuit_open"
    assert excinfo.value.retry_after_s == pytest.approx(30.0)
    assert "without an attempt" in excinfo.value.message


def test_the_window_elapses_into_a_single_probe() -> None:
    cb, clock = breaker(threshold=1, recovery=30.0)
    cb.record_failure()

    clock.advance(31)
    cb.before_call()  # allowed: this is the probe
    assert cb.state == "half_open"

    cb.record_success()
    assert cb.state == "closed"
    assert cb.consecutive_failures == 0


def test_a_failed_probe_reopens_the_circuit() -> None:
    cb, clock = breaker(threshold=1, recovery=30.0)
    cb.record_failure()
    clock.advance(31)
    cb.before_call()
    assert cb.state == "half_open"

    cb.record_failure()
    assert cb.state == "open"
    with pytest.raises(ToolFailure):
        cb.before_call()  # the window restarted, so no second probe yet


def test_success_resets_a_closed_circuit_counter() -> None:
    cb, _ = breaker(threshold=5)
    cb.record_failure()
    cb.record_failure()
    cb.record_success()
    assert cb.consecutive_failures == 0
    cb.record_failure()
    assert cb.state == "closed", "the counter starts again after a success"


def test_state_is_published_to_prometheus() -> None:
    cb, _ = breaker(threshold=1)
    cb.record_failure()
    assert REGISTRY.get_sample_value("flightops_circuit_open", {"upstream": "unit"}) == 1.0
    cb.reset()
    assert REGISTRY.get_sample_value("flightops_circuit_open", {"upstream": "unit"}) == 0.0


# --- integration with the HTTP layer -----------------------------------------
def single_attempt_settings(settings: Settings, **overrides: object) -> Settings:
    """One attempt per call, so failure counts are unambiguous in the assertions."""
    base = {"max_retries": 0, "backoff_base_s": 0.0}
    base.update(overrides)
    return settings.model_copy(update=base)


def http_client(
    settings: Settings,
    handler: object,
    *,
    sleep: SleepRecorder,
    clock: FakeClock,
) -> UpstreamHttp:
    return UpstreamHttp(
        settings,
        "opensky",
        "https://upstream.test",
        transport=httpx.MockTransport(handler),  # type: ignore[arg-type]
        sleep=sleep,
        clock=clock,
    )


async def test_a_dead_upstream_opens_the_circuit_and_the_next_call_never_connects(
    settings: Settings, sleep: SleepRecorder
) -> None:
    configure_breakers(failure_threshold=2, recovery_timeout_s=30.0)
    reset_breakers()
    clock = FakeClock()
    requests: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(1)
        return httpx.Response(503)

    http = http_client(single_attempt_settings(settings), handler, sleep=sleep, clock=clock)

    for _ in range(2):
        response = await http.request_json("GET", "/states/all")
        assert response.status_code == 503
    assert len(requests) == 2
    assert breakers().get("opensky").state == "open"

    with pytest.raises(ToolFailure) as excinfo:
        await http.request_json("GET", "/states/all")

    assert excinfo.value.code == "upstream_circuit_open"
    assert len(requests) == 2, "once the circuit is open, nothing may reach the socket"


async def test_the_circuit_fails_fast_rather_than_sleeping(
    settings: Settings, sleep: SleepRecorder
) -> None:
    configure_breakers(failure_threshold=3, recovery_timeout_s=30.0)
    reset_breakers()
    clock = FakeClock()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    # Three attempts open the circuit exactly at the end of the first call, so the
    # second call has a full budget to spend and must spend none of it.
    retrying = settings.model_copy(update={"backoff_base_s": 0.01, "max_retries": 2})
    http = http_client(retrying, handler, sleep=sleep, clock=clock)

    response = await http.request_json("GET", "/x")
    assert response.status_code == 500
    sleeps_after_first_call = len(sleep.calls)
    assert sleeps_after_first_call > 0, "the first call did spend its retry budget"
    assert breakers().get("opensky").state == "open"

    with pytest.raises(ToolFailure):
        await http.request_json("GET", "/x")

    assert len(sleep.calls) == sleeps_after_first_call, (
        "a refused call must not spend time in backoff"
    )


async def test_throttling_does_not_open_the_circuit(
    settings: Settings, sleep: SleepRecorder
) -> None:
    # 429 is a contract with the caller, not evidence the provider is broken: opening
    # the circuit would replace the useful Retry-After with a useless refusal.
    configure_breakers(failure_threshold=1, recovery_timeout_s=30.0)
    reset_breakers()
    clock = FakeClock()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"X-Rate-Limit-Retry-After-Seconds": "600"})

    http = http_client(single_attempt_settings(settings), handler, sleep=sleep, clock=clock)
    response = await http.request_json("GET", "/states/all")

    assert response.status_code == 429
    assert breakers().get("opensky").state == "closed"


async def test_client_errors_do_not_open_the_circuit(
    settings: Settings, sleep: SleepRecorder
) -> None:
    configure_breakers(failure_threshold=1, recovery_timeout_s=30.0)
    reset_breakers()
    clock = FakeClock()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    http = http_client(single_attempt_settings(settings), handler, sleep=sleep, clock=clock)
    response = await http.request_json("GET", "/missing")

    assert response.status_code == 404
    assert breakers().get("opensky").state == "closed", "a 404 proves the upstream is answering"


async def test_a_recovered_upstream_closes_the_circuit(
    settings: Settings, sleep: SleepRecorder
) -> None:
    configure_breakers(failure_threshold=1, recovery_timeout_s=10.0)
    reset_breakers()
    clock = FakeClock()
    healthy = {"value": False}

    def handler(request: httpx.Request) -> httpx.Response:
        if healthy["value"]:
            return httpx.Response(200, json={"time": 1, "states": []})
        return httpx.Response(500)

    http = http_client(single_attempt_settings(settings), handler, sleep=sleep, clock=clock)
    await http.request_json("GET", "/x")
    with pytest.raises(ToolFailure):
        await http.request_json("GET", "/x")

    healthy["value"] = True
    clock.advance(11)
    response = await http.request_json("GET", "/x")

    assert response.status_code == 200
    assert breakers().get("opensky").state == "closed"
