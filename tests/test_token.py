"""Token lifecycle: caching, the thundering herd, expiry and rejection.

These are the tests that justify the token manager existing at all. A naive
implementation passes the first one and fails the second.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from flightops_mcp.config import Settings
from flightops_mcp.errors import ToolFailure
from flightops_mcp.opensky_token import OpenSkyTokenManager

from .conftest import FakeClock


def token_transport(
    requests: list[httpx.Request], *, expires_in: int = 1800
) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"access_token": "tok-1", "expires_in": expires_in})

    return httpx.MockTransport(handler)


async def test_token_is_cached_across_calls(settings: Settings) -> None:
    requests: list[httpx.Request] = []
    manager = OpenSkyTokenManager(settings, transport=token_transport(requests))

    first = await manager.token()
    second = await manager.token()

    assert first == second == "tok-1"
    assert len(requests) == 1, "a cached token must not be re-requested"
    body = requests[0].content.decode()
    assert "grant_type=client_credentials" in body


async def test_fifty_concurrent_callers_make_exactly_one_token_request(settings: Settings) -> None:
    """The thundering-herd guard: single flight, proven by count, not by claim."""
    requests: list[httpx.Request] = []

    async def slow_handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        await asyncio.sleep(0.05)  # widen the window a naive implementation fails in
        return httpx.Response(200, json={"access_token": "tok-1", "expires_in": 1800})

    manager = OpenSkyTokenManager(settings, transport=httpx.MockTransport(slow_handler))

    tokens = await asyncio.gather(*(manager.token() for _ in range(50)))

    assert len(requests) == 1, f"expected 1 token request, saw {len(requests)}"
    assert set(tokens) == {"tok-1"}


async def test_token_refreshes_after_expiry_skew(settings: Settings) -> None:
    clock = FakeClock()
    requests: list[httpx.Request] = []
    manager = OpenSkyTokenManager(
        settings, transport=token_transport(requests, expires_in=1800), clock=clock
    )

    await manager.token()
    clock.advance(1800 - settings.token_expiry_skew_s - 1)
    await manager.token()
    assert len(requests) == 1, "still inside the validity window"

    clock.advance(5)  # now inside the skew
    await manager.token()
    assert len(requests) == 2, "refresh should happen before expiry, not after a 401"


async def test_invalidate_forces_a_fresh_token(settings: Settings) -> None:
    requests: list[httpx.Request] = []
    manager = OpenSkyTokenManager(settings, transport=token_transport(requests))
    await manager.token()
    manager.invalidate()
    await manager.token()
    assert len(requests) == 2


async def test_rejected_credentials_raise_a_typed_failure_without_leaking_the_secret(
    settings: Settings,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "invalid_client"})

    manager = OpenSkyTokenManager(settings, transport=httpx.MockTransport(handler))

    with pytest.raises(ToolFailure) as excinfo:
        await manager.token()

    assert excinfo.value.code == "upstream_auth_failed"
    assert "test-secret" not in str(excinfo.value.to_payload())
    assert "do not retry with the same credentials" in (excinfo.value.hint or "")


async def test_missing_credentials_are_reported_as_actionable_data() -> None:
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    manager = OpenSkyTokenManager(
        settings, transport=httpx.MockTransport(lambda r: httpx.Response(500))
    )

    with pytest.raises(ToolFailure) as excinfo:
        await manager.token()

    assert excinfo.value.code == "credentials_missing"
    payload = excinfo.value.to_payload()
    assert payload["ok"] is False
    assert "FLIGHTOPS_OPENSKY_CLIENT_ID" in payload["error"]["hint"]


async def test_malformed_token_response_is_not_reported_as_success(settings: Settings) -> None:
    manager = OpenSkyTokenManager(
        settings,
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"nope": True})),
    )
    with pytest.raises(ToolFailure) as excinfo:
        await manager.token()
    assert excinfo.value.code == "upstream_unavailable"
