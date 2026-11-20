"""The scope model and its two enforcement points.

First the HTTP layer answers a missing scope with 403 and a `WWW-Authenticate`
challenge naming what is needed. Then, for anything that reaches a tool anyway,
the tool refuses with an error payload naming the same scope.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
from mcp import MCPError

from flightops_mcp import server as srv
from flightops_mcp.auth import Scopes, UnknownToolError
from flightops_mcp.auth.challenge import INSUFFICIENT_SCOPE_RPC_CODE
from flightops_mcp.config import Settings
from flightops_mcp.runtime import Runtime

from .authkit import JwksEndpoint, signing_key
from .conftest import LogCapture
from .harness import RemoteServer, remote_settings, serve, tool_payload
from .test_server_tools import EXPECTED_TOOLS, call, install

KEY = signing_key("key-1")
METADATA_URL = "http://127.0.0.1:8000/.well-known/oauth-protected-resource/mcp"

ARGUMENTS: dict[str, dict[str, Any]] = {
    "resolve_airport": {"query": "ESGG"},
    "search_airports": {"query": "ES"},
    "airport_weather": {"airport": "ESGG"},
    "weather_overview": {"airports": ["ESGG", "EGLL"]},
    "airport_live_traffic": {"airport": "ESGG"},
    "airport_movements": {"airport": "ESGG"},
    "airspace_snapshot": {"region": "europe"},
    "aircraft_track": {"icao24": "4ca1fa"},
    "disruption_triage": {"airport": "ESGG"},
    "schiphol_flights": {"direction": "departure"},
    "upstream_status": {},
    "flight_passenger_impact": {"flight": "SK1429"},
    "propose_rebooking": {
        "from_flight": "SK1429",
        "flight_date": "2026-11-16",
        "to_flight": "SK1431",
        "cohort": "tight_connections",
        "reason_code": "late_inbound",
        "passenger_count": 2,
    },
}


@pytest.fixture
def runtime(settings: Settings) -> Runtime:
    return install(remote_settings(settings))


@pytest.fixture
async def remote(runtime: Runtime) -> AsyncIterator[RemoteServer]:
    async with serve(runtime.settings, JwksEndpoint(KEY).transport) as server:
        yield server


def challenge_of(response: Any) -> dict[str, str]:
    """Parse `Bearer k="v", ...` into a dict."""
    scheme, _, parameters = response.headers["www-authenticate"].partition(" ")
    assert scheme == "Bearer"
    pairs = (item.split("=", 1) for item in parameters.split('", '))
    return {name.strip(): value.strip('"') for name, value in pairs}


# --- the table ---------------------------------------------------------------
def test_every_published_tool_has_a_scope_entry() -> None:
    for name in EXPECTED_TOOLS:
        assert Scopes.for_tool(name), f"{name} must require at least one scope"


def test_the_write_has_a_scope_of_its_own() -> None:
    """Reading passengers and moving them are different permissions, in both directions."""
    assert Scopes.for_tool("propose_rebooking") == {Scopes.REBOOKING_WRITE}
    assert Scopes.missing("propose_rebooking", [Scopes.PASSENGERS_READ]) == [Scopes.REBOOKING_WRITE]
    assert Scopes.missing("flight_passenger_impact", [Scopes.REBOOKING_WRITE]) == [
        Scopes.PASSENGERS_READ
    ]


def test_the_approve_permission_opens_no_tool() -> None:
    """It belongs on an approval grant. As a caller's scope it is worth nothing."""
    assert Scopes.REBOOKING_APPROVE not in Scopes.all()
    for name in EXPECTED_TOOLS:
        assert Scopes.missing(name, [Scopes.REBOOKING_APPROVE]) == sorted(Scopes.for_tool(name))


def test_an_unknown_tool_has_no_scopes_rather_than_empty_scopes() -> None:
    """Failing closed: 'requires nothing' must never be the answer for a typo."""
    with pytest.raises(UnknownToolError):
        Scopes.for_tool("delete_everything")


def test_the_composite_tool_requires_everything_its_parts_require() -> None:
    parts = ("airport_weather", "airport_movements", "airport_live_traffic")
    needed = frozenset().union(*(Scopes.for_tool(part) for part in parts))

    assert Scopes.for_tool("disruption_triage") >= needed


def test_a_tool_without_a_scope_entry_cannot_be_registered() -> None:
    before = list(srv._TOOLS)

    async def export_all_passengers() -> dict[str, Any]:
        return {"ok": True}

    with pytest.raises(UnknownToolError):
        srv.tool(export_all_passengers)

    assert before == srv._TOOLS, "the unprotected tool must not have been added"


def test_missing_is_the_difference_between_required_and_granted() -> None:
    assert Scopes.missing("disruption_triage", []) == [Scopes.TRAFFIC_READ, Scopes.WEATHER_READ]
    assert Scopes.missing("disruption_triage", [Scopes.WEATHER_READ]) == [Scopes.TRAFFIC_READ]
    assert Scopes.missing("disruption_triage", sorted(Scopes.all())) == []


# --- the HTTP challenge ------------------------------------------------------
async def test_a_missing_scope_is_a_403_with_a_challenge_naming_it(
    remote: RemoteServer, runtime: Runtime, logs: LogCapture
) -> None:
    token = KEY.mint(scp=Scopes.WEATHER_READ, azp="agent-app")

    response = await remote.call_tool("airport_live_traffic", {"airport": "ESGG"}, token=token)

    assert response.status_code == 403
    challenge = challenge_of(response)
    assert challenge["error"] == "insufficient_scope"
    assert challenge["scope"] == Scopes.TRAFFIC_READ
    assert challenge["resource_metadata"] == METADATA_URL

    error = response.json()["error"]
    assert response.json()["id"] == 1
    assert error["code"] == INSUFFICIENT_SCOPE_RPC_CODE
    assert Scopes.TRAFFIC_READ in error["message"]
    failure = error["data"]
    assert failure["ok"] is False
    assert failure["error"]["code"] == "insufficient_scope"
    assert failure["error"]["details"] == {
        "tool": "airport_live_traffic",
        "required_scopes": [Scopes.TRAFFIC_READ],
        "missing_scopes": [Scopes.TRAFFIC_READ],
    }
    assert failure["error"]["hint"]

    assert runtime.opensky.credits_spent_total == 0, "the tool must not have run"
    logged = logs.events("scope_challenge")[-1]
    assert logged["tool"] == "airport_live_traffic"
    assert logged["missing_scopes"] == [Scopes.TRAFFIC_READ]
    assert logged["client_id"] == "agent-app"
    assert token not in logs.text


@pytest.mark.parametrize("tool", sorted(EXPECTED_TOOLS))
async def test_every_tool_refuses_a_token_with_no_scopes(tool: str, remote: RemoteServer) -> None:
    response = await remote.call_tool(tool, ARGUMENTS[tool], token=KEY.mint())

    assert response.status_code == 403
    assert challenge_of(response)["scope"].split() == sorted(Scopes.for_tool(tool))


# The on-behalf-of tools also need an IdP and a downstream. Their success paths are in
# test_obo.py and test_rebooking_gate.py.
ON_BEHALF_OF = {"flight_passenger_impact", "propose_rebooking"}


@pytest.mark.parametrize("tool", sorted(EXPECTED_TOOLS - ON_BEHALF_OF))
async def test_every_tool_runs_with_exactly_its_own_scopes(tool: str, remote: RemoteServer) -> None:
    token = KEY.mint(scp=" ".join(Scopes.for_tool(tool)))

    response = await remote.call_tool(tool, ARGUMENTS[tool], token=token)

    assert response.status_code == 200
    assert tool_payload(response)["ok"] is True


async def test_a_scope_for_one_tool_does_not_open_another(remote: RemoteServer) -> None:
    token = KEY.mint(scp=f"{Scopes.REFERENCE_READ} {Scopes.STATUS_READ}")

    response = await remote.call_tool("airport_weather", {"airport": "ESGG"}, token=token)

    assert response.status_code == 403
    assert challenge_of(response)["scope"] == Scopes.WEATHER_READ


async def test_the_composite_challenge_lists_all_it_needs_and_the_body_says_what_is_missing(
    remote: RemoteServer,
) -> None:
    token = KEY.mint(scp=Scopes.WEATHER_READ)

    response = await remote.call_tool("disruption_triage", {"airport": "ESGG"}, token=token)

    assert response.status_code == 403
    assert challenge_of(response)["scope"].split() == [Scopes.TRAFFIC_READ, Scopes.WEATHER_READ]
    details = response.json()["error"]["data"]["error"]["details"]
    assert details["missing_scopes"] == [Scopes.TRAFFIC_READ]


async def test_application_roles_satisfy_scopes_like_delegated_ones(remote: RemoteServer) -> None:
    """A service principal carries `roles`, a user carries `scp`; the check is the same."""
    token = KEY.mint(roles=[Scopes.WEATHER_READ])

    allowed = await remote.call_tool("airport_weather", {"airport": "ESGG"}, token=token)
    refused = await remote.call_tool("upstream_status", token=token)

    assert allowed.status_code == 200
    assert refused.status_code == 403


async def test_listing_tools_needs_no_scope(remote: RemoteServer) -> None:
    response = await remote.rpc("tools/list", token=KEY.mint())

    assert response.status_code == 200
    assert len(response.json()["result"]["tools"]) == len(EXPECTED_TOOLS)


async def test_the_sdk_client_receives_the_refusal_as_an_error_naming_the_scope(
    remote: RemoteServer,
) -> None:
    async with remote.client(KEY.mint(scp=Scopes.REFERENCE_READ)) as client:
        allowed = await call(client, "resolve_airport", {"query": "ESGG"})
        with pytest.raises(MCPError) as excinfo:
            await client.call_tool("upstream_status", {})

    assert allowed["ok"] is True
    error = excinfo.value.error
    assert error.code == INSUFFICIENT_SCOPE_RPC_CODE
    assert error.data["error"]["code"] == "insufficient_scope"
    assert error.data["error"]["details"]["missing_scopes"] == [Scopes.STATUS_READ]


async def test_an_unknown_tool_is_left_to_the_protocol_layer(remote: RemoteServer) -> None:
    response = await remote.call_tool("delete_everything", token=KEY.mint())

    assert response.status_code != 403
    assert '"ok": true' not in response.text and '"ok":true' not in response.text


@pytest.mark.parametrize("body", [b"", b"{not json", b"[]", b'"tools/call"', b'{"method": 7}'])
async def test_a_body_the_challenge_cannot_parse_is_not_served(
    body: bytes, remote: RemoteServer
) -> None:
    response = await remote.raw.post(
        "/mcp",
        content=body,
        headers={
            "Authorization": f"Bearer {KEY.mint()}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        },
    )

    assert 400 <= response.status_code < 500


async def test_a_batch_cannot_carry_a_tool_call_past_the_scope_check(
    remote: RemoteServer, runtime: Runtime
) -> None:
    batch = [
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "airport_live_traffic", "arguments": {"airport": "ESGG"}},
        },
    ]

    response = await remote.raw.post(
        "/mcp",
        json=batch,
        headers={
            "Authorization": f"Bearer {KEY.mint()}",
            "Accept": "application/json, text/event-stream",
        },
    )

    assert '"ok": true' not in response.text and '"ok":true' not in response.text
    assert runtime.opensky.credits_spent_total == 0


# --- the check next to the tool ----------------------------------------------
async def test_the_tool_itself_refuses_when_the_http_challenge_is_absent(
    runtime: Runtime, logs: LogCapture
) -> None:
    """Defence in depth: remove the middleware and the scope still holds."""
    token = KEY.mint(scp=Scopes.WEATHER_READ)
    async with serve(
        runtime.settings, JwksEndpoint(KEY).transport, http_challenge=False
    ) as unguarded:
        response = await unguarded.call_tool(
            "airport_live_traffic", {"airport": "ESGG"}, token=token
        )
        allowed = await unguarded.call_tool("airport_weather", {"airport": "ESGG"}, token=token)

    assert response.status_code == 200, "without the middleware nothing produces a 403"
    payload = tool_payload(response)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "insufficient_scope"
    assert Scopes.TRAFFIC_READ in payload["error"]["message"]
    assert payload["error"]["details"]["missing_scopes"] == [Scopes.TRAFFIC_READ]
    assert runtime.opensky.credits_spent_total == 0
    assert tool_payload(allowed)["ok"] is True
    assert logs.events("tool_refused")[-1]["code"] == "insufficient_scope"


async def test_a_tool_on_the_authenticated_server_fails_closed_without_a_token(
    remote: RemoteServer,
) -> None:
    """Called outside any request there is no verified token: that is a refusal."""
    result = await remote.server.call_tool("resolve_airport", {"query": "ESGG"})

    payload = getattr(result, "structured_content", None)
    assert isinstance(payload, dict)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "authentication_required"


async def test_the_stdio_server_runs_tools_without_a_token(runtime: Runtime) -> None:
    """stdio has no bearer token by design; its tools are not scope-wrapped."""
    result = await srv.mcp.call_tool("resolve_airport", {"query": "ESGG"})

    payload = getattr(result, "structured_content", None)
    assert isinstance(payload, dict) and payload["ok"] is True
