"""The mock upstream and the two scripts that drive it, with the whole stack in process.

The mock serves OpenSky, AviationWeather and Schiphol in their documented shapes. Here
the real server's clients decode it, so a mock that drifted from what the clients
read would fail here before it made a load test or a compose run meaningless.

Then the script itself: `scripts/second_upstream.py`
runs against the same stack through one routing transport (IdP, mock, server, each
an ASGI app), so CI runs a small load test on every push and the numbers in the
README come from code that is under test. The rate here is tiny; the measured run
is `scripts/load_test.sh` (docs/LOAD-TEST.md).
"""

from __future__ import annotations

import importlib.util
import sys
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

import httpx
import pytest

from flightops_mcp.config import Settings
from flightops_mcp.runtime import Runtime, set_runtime
from services.mock_upstream.app import MockUpstreamConfig, create_app

from .authkit import ISSUER
from .harness import BASE_URL, RemoteServer, remote_settings, serve, tool_payload
from .idpkit import LocalIdp
from .test_server_tools import install

SCRIPTS = Path(__file__).parent.parent / "scripts"
MOCK = "http://mock.test"
OPENSKY_SECRET = "opensky-" + "y" * 24
SCHIPHOL_SECRET = "schiphol-" + "z" * 24


def load_script(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(f"{name}_under_test", SCRIPTS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class Router(httpx.AsyncBaseTransport):
    """One transport for a client that talks to several in-process apps, by host."""

    def __init__(self, routes: dict[str, httpx.AsyncBaseTransport]) -> None:
        self._routes = routes

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        return await self._routes[request.url.netloc.decode()].handle_async_request(request)


@dataclass(slots=True)
class Stack:
    remote: RemoteServer
    runtime: Runtime
    mock_app: Any
    router: Router

    @property
    def stats(self) -> dict[str, int]:
        return dict(self.mock_app.state.stats)


@pytest.fixture
async def stack(settings: Settings) -> AsyncIterator[Stack]:
    idp = LocalIdp()
    mock_app = create_app(
        MockUpstreamConfig(
            opensky_client_secret=OPENSKY_SECRET, schiphol_client_secret=SCHIPHOL_SECRET
        )
    )
    mock = httpx.ASGITransport(app=mock_app)
    configured = remote_settings(settings).model_copy(
        update={
            "opensky_base_url": f"{MOCK}/opensky/api",
            "opensky_auth_url": f"{MOCK}/opensky/token",
            "opensky_client_id": "flightops-opensky",
            "opensky_client_secret": OPENSKY_SECRET,
            "avwx_base_url": f"{MOCK}/noaa/api/data",
            "schiphol_base_url": f"{MOCK}/schiphol/public",
            "schiphol_auth_url": f"{MOCK}/schiphol/oauth/token",
            "schiphol_client_id": "flightops-schiphol",
            "schiphol_client_secret": SCHIPHOL_SECRET,
        }
    )
    install(configured)  # builds the airport database
    runtime = Runtime.build(
        configured,
        opensky_transport=mock,
        token_transport=mock,
        weather_transport=mock,
        schiphol_transport=mock,
        schiphol_token_transport=mock,
    )
    set_runtime(runtime)
    try:
        async with serve(configured, idp.transport) as remote:
            server = httpx.ASGITransport(app=remote.app)
            router = Router(
                {
                    BASE_URL.removeprefix("http://"): server,
                    ISSUER.removeprefix("http://"): idp.transport,
                    MOCK.removeprefix("http://"): mock,
                }
            )
            yield Stack(remote=remote, runtime=runtime, mock_app=mock_app, router=router)
    finally:
        set_runtime(None)
        await runtime.aclose()


async def user(stack: Stack, scope: str) -> str:
    async with httpx.AsyncClient(transport=stack.router) as client:
        response = await client.post(
            f"{ISSUER}/dev/user-token",
            json={
                "sub": "mock-user",
                "audience": f"{BASE_URL}/mcp",
                "scope": scope,
                "client_id": "flightops-agent",
            },
        )
    response.raise_for_status()
    return str(response.json()["access_token"])


# --- the mock speaks what the clients read ----------------------------------------------
async def test_every_documented_shape_decodes_through_the_real_tools(stack: Stack) -> None:
    token = await user(
        stack,
        "flightops.reference.read flightops.weather.read flightops.traffic.read "
        "flightops.schedule.read",
    )
    triage = tool_payload(
        await stack.remote.call_tool("disruption_triage", {"airport": "ESGG"}, token=token)
    )
    schedule = tool_payload(
        await stack.remote.call_tool("schiphol_flights", {"direction": "arrival"}, token=token)
    )

    assert triage["ok"] is True, triage
    assert triage["degraded"] == []
    assert triage["evidence"]["traffic_counts"]["returned"] == 12
    assert triage["evidence"]["movements_summary"] == {"arrivals": 4, "departures": 4}
    assert triage["evidence"]["weather"][0]["flight_category"] == "VFR"
    assert triage["bookkeeping"]["credits_spent"] == 9
    assert schedule["ok"] is True and schedule["counts"]["arrival"]["listed"] > 0
    stats = stack.stats
    assert stats["opensky.tokens_issued"] == 1
    assert stats["opensky.credits"] == 9
    assert not any(".refused." in key for key in stats)


async def test_the_mock_refuses_what_the_vendor_refuses(stack: Stack) -> None:
    async with httpx.AsyncClient(transport=stack.router, base_url=MOCK) as client:
        no_secret = await client.post(
            "/schiphol/oauth/token",
            data={"grant_type": "client_credentials", "client_id": "flightops-schiphol"},
        )
        no_audience = await client.post(
            "/schiphol/oauth/token",
            data={
                "grant_type": "client_credentials",
                "client_id": "flightops-schiphol",
                "client_secret": SCHIPHOL_SECRET,
            },
        )
        wrong_grant = await client.post("/opensky/token", data={"grant_type": "password"})
        no_bearer = await client.get("/opensky/api/states/all")
        bad_box = await client.get(
            "/schiphol/public/public-flights/v4/flights",
            params={"flightDirection": "A"},
            headers={"app_key": "legacy"},
        )
        unknown_product = await client.get("/noaa/api/data/sigmet")

    assert (no_secret.status_code, no_audience.status_code) == (401, 400)
    assert wrong_grant.status_code == 400
    assert no_bearer.json() == {"error": "invalid_token", "error_description": "missing_token"}
    assert bad_box.status_code == 401
    assert unknown_product.status_code == 404
    assert stack.stats["schiphol.legacy_headers"] == 1


def test_a_mock_with_no_secret_configured_refuses_every_client() -> None:
    config = MockUpstreamConfig()
    assert config.schiphol_client_secret is None
    assert config.opensky_client_secret is None


# --- the scripts, end to end ------------------------------------------------------------
async def test_the_second_upstream_check_passes_against_the_stack(
    stack: Stack, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    script = load_script("second_upstream")
    monkeypatch.setattr(script, "SERVER", BASE_URL)
    monkeypatch.setattr(script, "RESOURCE", f"{BASE_URL}/mcp")
    monkeypatch.setattr(script, "IDP", ISSUER)
    monkeypatch.setattr(script, "MOCK", MOCK)

    status = await script.main(stack.router)

    out = capsys.readouterr().out
    assert status == 0, out
    assert "4/4 passed" in out
    assert "FAIL" not in out
