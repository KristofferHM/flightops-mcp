"""On-behalf-of, end to end: MCP server -> local IdP (exchange) -> mock ops API.

All three are the real apps, in process. A user's token goes in at the MCP
server; a different, narrower token arrives at the ops API carrying the same
user; counts come back and nothing that identifies a passenger does.
"""

from __future__ import annotations

import asyncio
import base64
import json
from collections import Counter
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest
from mcp.server.auth.provider import AccessToken

from flightops_mcp import server as srv
from flightops_mcp.auth import Scopes
from flightops_mcp.auth.obo import ACCESS_TOKEN_TYPE, TOKEN_EXCHANGE_GRANT, OboExchanger
from flightops_mcp.config import Settings
from flightops_mcp.errors import ToolFailure
from flightops_mcp.runtime import Runtime, set_runtime
from flightops_mcp.telemetry import redaction_processor
from services.ops_api.app import DEFAULT_MANIFESTS, OpsApiConfig, create_app

from .authkit import ISSUER, JWKS_URL, authenticated_as
from .conftest import FakeClock, LogCapture
from .harness import RemoteServer, remote_settings, serve, tool_payload
from .idpkit import (
    BATCH,
    BYSTANDER,
    CREW_READ,
    OPS_AUDIENCE,
    OTHER_AUDIENCE,
    PASSENGERS_READ,
    SERVER,
    LocalIdp,
    claims_of,
)
from .test_server_tools import install

TOKEN_URL = f"{ISSUER}/token"
OPS_URL = "http://ops.test"

CANARY_ROW = {
    "passenger_name": "CANARY-PAX-Ada Lovelace",
    "email": "CANARY-EMAIL@example.com",
    "phone": "CANARY-PHONE-0701740699",
    "booking_ref": "CANARY-PNR",
    "date_of_birth": "CANARY-DOB-1815-12-10",
    "seat": "1A",
    "checked_in": True,
    "connecting_flight": "SK903",
    "connection_minutes": 20,
    "needs_assistance": True,
}


class Tap(httpx.AsyncBaseTransport):
    """Passes requests through to an app, recording them and optionally refusing some."""

    def __init__(self, inner: httpx.AsyncBaseTransport) -> None:
        self._inner = inner
        self.bearers: list[str] = []
        self.refuse_next = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        # Yield like a real network call, so concurrent callers actually interleave.
        await asyncio.sleep(0)
        self.bearers.append(request.headers.get("authorization", "").removeprefix("Bearer "))
        if self.refuse_next:
            self.refuse_next -= 1
            return httpx.Response(401, json={"error": "invalid_token"})
        return await self._inner.handle_async_request(request)


@dataclass(slots=True)
class Stack:
    idp: LocalIdp
    ops_app: Any
    ops: Tap
    runtime: Runtime
    remote: RemoteServer
    user_tokens: dict[str, str] = field(default_factory=dict)

    @property
    def exchanges(self) -> int:
        return self.idp.provider.issued["token_exchange"]

    @property
    def audit(self) -> list[dict[str, str]]:
        audit: list[dict[str, str]] = self.ops_app.state.audit
        return audit

    async def user(self, sub: str = "user-9", scope: str = PASSENGERS_READ) -> str:
        return await self.idp.user_token(sub, scope=scope)

    async def impact(self, token: str, flight: str = "SK1429") -> dict[str, Any]:
        response = await self.remote.call_tool(
            "flight_passenger_impact", {"flight": flight}, token=token
        )
        assert response.status_code == 200, response.text
        return tool_payload(response)


@pytest.fixture
async def stack(settings: Settings) -> AsyncIterator[Stack]:
    idp = LocalIdp()
    manifests = {**DEFAULT_MANIFESTS, "SK7777": [CANARY_ROW]}
    ops_app = create_app(
        OpsApiConfig(issuer=ISSUER, jwks_url=JWKS_URL, audience=OPS_AUDIENCE),
        jwks_transport=idp.transport,
        manifests=manifests,
    )
    ops = Tap(httpx.ASGITransport(app=ops_app))
    configured = remote_settings(settings).model_copy(
        update={
            "auth_token_url": TOKEN_URL,
            "auth_client_id": SERVER,
            "auth_client_secret": idp.secrets[SERVER],
            "ops_api_base_url": OPS_URL,
            "ops_api_audience": OPS_AUDIENCE,
        }
    )
    install(configured)  # builds the airport database
    runtime = Runtime.build(configured, idp_transport=Tap(idp.transport), ops_transport=ops)
    set_runtime(runtime)
    async with serve(configured, idp.transport) as remote:
        yield Stack(idp=idp, ops_app=ops_app, ops=ops, runtime=runtime, remote=remote)


# --- the path, end to end -----------------------------------------------------
async def test_a_users_call_reaches_the_ops_api_as_that_user(stack: Stack) -> None:
    inbound = await stack.user("user-9")

    payload = await stack.impact(inbound)

    assert payload["ok"] is True
    assert payload["flight"] == "SK1429"
    assert payload["passengers"] == {
        "booked": 6,
        "checked_in": 5,
        "not_checked_in": 1,
        "connecting": 3,
        "tight_connections": 2,
        "needing_assistance": 2,
    }
    assert payload["unknowns"] == []
    assert payload["delegation"]["downstream_audience"] == OPS_AUDIENCE
    assert stack.audit == [{"flight": "SK1429", "user": "user-9", "actor": SERVER}], (
        "the downstream audit trail must name the user, with the server as the actor"
    )
    assert stack.exchanges == 1


async def test_the_token_sent_downstream_is_exchanged_not_forwarded(stack: Stack) -> None:
    inbound = await stack.user("user-9", scope=f"{PASSENGERS_READ} {Scopes.WEATHER_READ}")

    await stack.impact(inbound)

    (downstream,) = stack.ops.bearers
    assert downstream != inbound
    sent = claims_of(downstream)
    assert sent["aud"] == OPS_AUDIENCE
    assert sent["sub"] == "user-9"
    assert sent["scp"] == PASSENGERS_READ, "only the scope this call needs goes downstream"
    assert sent["act"] == {"sub": SERVER}
    assert sent["exp"] <= claims_of(inbound)["exp"]


async def test_no_passenger_identifying_field_leaves_the_server(
    stack: Stack, logs: LogCapture
) -> None:
    """The canary: PII-shaped values planted downstream appear nowhere upstream of it."""
    inbound = await stack.user()

    response = await stack.remote.call_tool(
        "flight_passenger_impact", {"flight": "SK7777"}, token=inbound
    )

    assert tool_payload(response)["passengers"]["booked"] == 1
    assert tool_payload(response)["passengers"]["tight_connections"] == 1
    assert "CANARY" not in response.text
    assert "CANARY" not in logs.text
    assert inbound not in logs.text
    assert stack.ops.bearers[0] not in logs.text, "the exchanged token is a secret too"
    assert logs.events("obo_exchanged"), "the exchange itself must be visible in the log"


async def test_an_empty_manifest_is_reported_as_unknown_not_as_nobody_affected(
    stack: Stack,
) -> None:
    payload = await stack.impact(await stack.user(), flight="SK9001")

    assert payload["ok"] is True
    assert payload["passengers"]["booked"] == 0
    assert len(payload["unknowns"]) == 1
    assert "not evidence" in payload["unknowns"][0]


async def test_an_unknown_flight_is_a_not_found_failure(stack: Stack) -> None:
    payload = await stack.impact(await stack.user(), flight="SK0001")

    assert payload["ok"] is False
    assert payload["error"]["code"] == "not_found"


@pytest.mark.parametrize("flight", ["", "../admin", "SK1429/../../x", "not a flight", "1"])
async def test_a_malformed_flight_never_reaches_the_idp_or_the_api(
    flight: str, stack: Stack
) -> None:
    payload = await stack.impact(await stack.user(), flight=flight)

    assert payload["error"]["code"] == "invalid_argument"
    assert stack.exchanges == 0
    assert stack.ops.bearers == []


async def test_the_flight_designator_is_normalised(stack: Stack) -> None:
    payload = await stack.impact(await stack.user(), flight=" sk 1429 ")
    assert payload["flight"] == "SK1429"


# --- who may use it -----------------------------------------------------------
async def test_a_user_without_the_passenger_scope_is_challenged(stack: Stack) -> None:
    inbound = await stack.user(scope=Scopes.WEATHER_READ)

    response = await stack.remote.call_tool(
        "flight_passenger_impact", {"flight": "SK1429"}, token=inbound
    )

    assert response.status_code == 403
    assert f'scope="{PASSENGERS_READ}"' in response.headers["www-authenticate"]
    assert stack.exchanges == 0


async def test_a_service_principal_cannot_act_on_behalf_of_anyone(stack: Stack) -> None:
    """It holds the permission as an application role, but there is no user to act for."""
    inbound = await stack.idp.service_token(client_id=BATCH)

    payload = await stack.impact(inbound)

    assert payload["ok"] is False
    assert payload["error"]["code"] == "obo_exchange_rejected"
    assert payload["error"]["details"] == {"idp_error": "invalid_grant"}
    assert stack.ops.bearers == [], "nothing was sent downstream"


async def test_the_tool_is_unavailable_over_stdio(stack: Stack) -> None:
    result = await srv.mcp.call_tool("flight_passenger_impact", {"flight": "SK1429"})

    payload = getattr(result, "structured_content", None)
    assert isinstance(payload, dict)
    assert payload["error"]["code"] == "user_token_required"
    assert stack.exchanges == 0


# --- one exchange per identity -------------------------------------------------
async def test_fifty_concurrent_calls_for_one_user_make_exactly_one_exchange(
    stack: Stack,
) -> None:
    inbound = await stack.user("user-9")

    payloads = await asyncio.gather(*(stack.impact(inbound) for _ in range(50)))

    assert all(payload["ok"] for payload in payloads)
    assert stack.exchanges == 1
    assert stack.runtime.obo.exchange_count == 1
    assert len(stack.audit) == 50, "every call still reached the downstream API"
    assert len(set(stack.ops.bearers)) == 1


async def test_fifty_concurrent_calls_make_exactly_one_exchange_per_identity(
    stack: Stack,
) -> None:
    users = [f"user-{index}" for index in range(5)]
    tokens = {user: await stack.user(user) for user in users}
    calls = [stack.impact(tokens[users[index % 5]]) for index in range(50)]

    payloads = await asyncio.gather(*calls)

    assert all(payload["ok"] for payload in payloads)
    assert stack.exchanges == 5
    assert Counter(entry["user"] for entry in stack.audit) == dict.fromkeys(users, 10)
    # Each downstream token belongs to the user whose call carried it.
    assert {claims_of(bearer)["sub"] for bearer in stack.ops.bearers} == set(users)
    assert len(set(stack.ops.bearers)) == 5


async def test_a_refreshed_token_for_the_same_user_reuses_the_exchange(stack: Stack) -> None:
    """Identity, not token, is the cache key: a client refreshing its token costs nothing."""
    first = await stack.user("user-9")
    second = await stack.user("user-9")
    assert first != second

    await stack.impact(first)
    await stack.impact(second)

    assert stack.exchanges == 1


# --- failure paths on demand ---------------------------------------------------
@pytest.mark.parametrize("kill", ["revoke", "expire"])
async def test_a_revoked_or_expired_user_token_cannot_be_exchanged(kill: str, stack: Stack) -> None:
    inbound = await stack.user()
    await getattr(stack.idp, kill)(inbound)

    payload = await stack.impact(inbound)

    assert payload["ok"] is False
    assert payload["error"]["code"] == "obo_exchange_rejected"
    assert payload["error"]["details"] == {"idp_error": "invalid_grant"}
    assert "sign in again" in payload["error"]["hint"]
    assert stack.ops.bearers == []


async def test_a_downstream_401_triggers_exactly_one_re_exchange(stack: Stack) -> None:
    inbound = await stack.user()
    await stack.impact(inbound)
    stack.ops.refuse_next = 1  # the cached token is suddenly not accepted

    payload = await stack.impact(inbound)

    assert payload["ok"] is True
    assert stack.exchanges == 2
    assert len(set(stack.ops.bearers)) == 2, "the retry used a newly exchanged token"


async def test_a_downstream_that_keeps_refusing_is_a_failure_not_a_loop(stack: Stack) -> None:
    stack.ops.refuse_next = 10

    payload = await stack.impact(await stack.user())

    assert payload["ok"] is False
    assert payload["error"]["code"] == "downstream_auth_failed"
    assert stack.exchanges == 2
    assert len(stack.ops.bearers) == 2


async def test_an_unreachable_idp_is_reported_as_unavailable(settings: Settings) -> None:
    def down(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    exchanger = OboExchanger(obo_settings(settings), transport=httpx.MockTransport(down))

    with pytest.raises(ToolFailure) as excinfo:
        await exchanger.token_for(inbound_user("user-1"), audience=OPS_AUDIENCE, scopes=["a"])

    assert excinfo.value.code == "upstream_unavailable"


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(503),
        httpx.Response(200, text="not json"),
        httpx.Response(200, json={"token_type": "Bearer"}),
        httpx.Response(200, json={"access_token": "x", "expires_in": "soon"}),
    ],
)
async def test_an_unusable_exchange_response_is_reported_as_unavailable(
    response: httpx.Response, settings: Settings
) -> None:
    exchanger = OboExchanger(
        obo_settings(settings), transport=httpx.MockTransport(lambda _request: response)
    )

    with pytest.raises(ToolFailure) as excinfo:
        await exchanger.token_for(inbound_user("user-1"), audience=OPS_AUDIENCE, scopes=["a"])

    assert excinfo.value.code == "upstream_unavailable"
    assert exchanger.exchange_count == 0


async def test_obo_without_server_credentials_is_a_configuration_failure(
    settings: Settings,
) -> None:
    exchanger = OboExchanger(settings)

    with pytest.raises(ToolFailure) as excinfo:
        await exchanger.token_for(inbound_user("user-1"), audience=OPS_AUDIENCE, scopes=["a"])

    assert excinfo.value.code == "obo_not_configured"


# --- the exchanger on its own --------------------------------------------------
def obo_settings(settings: Settings) -> Settings:
    return settings.model_copy(
        update={
            "auth_token_url": TOKEN_URL,
            "auth_client_id": SERVER,
            "auth_client_secret": "server-secret",
            "token_expiry_skew_s": 30.0,
        }
    )


INBOUND = "inbound-token"


def inbound_user(sub: str, tid: str = "tenant-1") -> AccessToken:
    return AccessToken(
        token=INBOUND, client_id="agent", scopes=[], subject=sub, claims={"iss": ISSUER, "tid": tid}
    )


class TokenEndpoint:
    def __init__(self, expires_in: int = 300) -> None:
        self.requests: list[httpx.Request] = []
        self.expires_in = expires_in

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        number = len(self.requests)
        await asyncio.sleep(0)  # yield, so a missing lock shows up as extra requests
        return httpx.Response(
            200, json={"access_token": f"down-{number}", "expires_in": self.expires_in}
        )


def exchanger_with(
    settings: Settings, clock: Callable[[], float]
) -> tuple[OboExchanger, TokenEndpoint]:
    endpoint = TokenEndpoint()
    exchanger = OboExchanger(
        obo_settings(settings), transport=httpx.MockTransport(endpoint), clock=clock
    )
    return exchanger, endpoint


async def test_the_exchange_request_presents_the_users_token_and_the_servers_credentials(
    settings: Settings, clock: FakeClock
) -> None:
    exchanger, endpoint = exchanger_with(settings, clock)

    await exchanger.token_for(
        inbound_user("user-1"), audience=OPS_AUDIENCE, scopes=[PASSENGERS_READ]
    )

    (request,) = endpoint.requests
    assert str(request.url) == TOKEN_URL
    form = dict(httpx.QueryParams(request.content.decode()))
    assert form == {
        "grant_type": TOKEN_EXCHANGE_GRANT,
        "subject_token": INBOUND,
        "subject_token_type": ACCESS_TOKEN_TYPE,
        "requested_token_type": ACCESS_TOKEN_TYPE,
        "audience": OPS_AUDIENCE,
        "scope": PASSENGERS_READ,
    }
    credentials = base64.b64decode(request.headers["authorization"].removeprefix("Basic "))
    assert credentials == f"{SERVER}:server-secret".encode(), (
        "the server authenticates as itself, never with the user's token"
    )


async def test_an_exchanged_token_is_reused_until_shortly_before_it_expires(
    settings: Settings, clock: FakeClock
) -> None:
    exchanger, endpoint = exchanger_with(settings, clock)
    user = inbound_user("user-1")

    first = await exchanger.token_for(user, audience=OPS_AUDIENCE, scopes=["a"])
    clock.advance(269)  # 300s lifetime minus the 30s skew
    second = await exchanger.token_for(user, audience=OPS_AUDIENCE, scopes=["a"])
    clock.advance(2)
    third = await exchanger.token_for(user, audience=OPS_AUDIENCE, scopes=["a"])

    assert first == second == "down-1"
    assert third == "down-2"
    assert len(endpoint.requests) == 2


async def test_the_cache_never_crosses_users_tenants_audiences_or_scopes(
    settings: Settings, clock: FakeClock
) -> None:
    exchanger, endpoint = exchanger_with(settings, clock)
    alice = inbound_user("alice")

    tokens = [
        await exchanger.token_for(alice, audience=OPS_AUDIENCE, scopes=["a"]),
        await exchanger.token_for(inbound_user("bob"), audience=OPS_AUDIENCE, scopes=["a"]),
        await exchanger.token_for(
            inbound_user("alice", tid="tenant-2"), audience=OPS_AUDIENCE, scopes=["a"]
        ),
        await exchanger.token_for(alice, audience="api://other", scopes=["a"]),
        await exchanger.token_for(alice, audience=OPS_AUDIENCE, scopes=["a", "b"]),
    ]

    assert len(set(tokens)) == 5, "five distinct identities or targets, five exchanges"
    assert await exchanger.token_for(alice, audience=OPS_AUDIENCE, scopes=["a"]) == tokens[0]
    assert len(endpoint.requests) == 5


async def test_fifty_concurrent_exchanges_for_one_identity_make_one_request(
    settings: Settings, clock: FakeClock
) -> None:
    exchanger, endpoint = exchanger_with(settings, clock)
    user = inbound_user("user-1")

    tokens = await asyncio.gather(
        *(exchanger.token_for(user, audience=OPS_AUDIENCE, scopes=["a"]) for _ in range(50))
    )

    assert set(tokens) == {"down-1"}
    assert len(endpoint.requests) == 1


async def test_expired_entries_are_dropped_from_the_cache(
    settings: Settings, clock: FakeClock
) -> None:
    exchanger, _ = exchanger_with(settings, clock)
    for index in range(20):
        await exchanger.token_for(
            inbound_user(f"user-{index}"), audience=OPS_AUDIENCE, scopes=["a"]
        )
    assert len(exchanger._cache) == 20

    clock.advance(600)
    await exchanger.token_for(inbound_user("late"), audience=OPS_AUDIENCE, scopes=["a"])

    assert len(exchanger._cache) == 1
    assert len(exchanger._locks) == 1


# --- the ops API only accepts exchanged tokens ---------------------------------
async def ops_get(stack: Stack, token: str | None) -> httpx.Response:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=stack.ops_app), base_url=OPS_URL
    ) as client:
        return await client.get("/flights/SK1429/passengers", headers=headers)


async def test_the_ops_api_serves_an_exchanged_token(stack: Stack) -> None:
    exchanged = (await stack.idp.exchange(await stack.user())).json()["access_token"]

    response = await ops_get(stack, exchanged)

    assert response.status_code == 200
    assert response.json()["passengers"][0]["passenger_name"] == "Example Passenger 01"


async def test_the_ops_api_refuses_the_users_original_token(stack: Stack) -> None:
    """Passthrough would fail here even if the MCP server attempted it."""
    response = await ops_get(stack, await stack.user())

    assert response.status_code == 401
    assert "passenger_name" not in response.text


async def test_the_ops_api_refuses_a_token_for_it_that_was_not_exchanged(stack: Stack) -> None:
    direct = await stack.idp.user_token(audience=OPS_AUDIENCE, scope=PASSENGERS_READ)

    response = await ops_get(stack, direct)

    assert response.status_code == 401
    assert response.json()["error_description"] == "only exchanged tokens are accepted"


async def test_the_ops_api_refuses_an_exchanged_token_without_the_passenger_scope(
    stack: Stack,
) -> None:
    subject = await stack.idp.user_token(audience=OTHER_AUDIENCE, scope=CREW_READ)
    exchanged = await stack.idp.exchange(subject, client_id=BYSTANDER, scope=CREW_READ)

    response = await ops_get(stack, exchanged.json()["access_token"])

    assert response.status_code == 403
    assert response.json()["error"] == "insufficient_scope"


async def test_the_ops_api_health_endpoint_needs_no_token_and_exposes_no_data(
    stack: Stack,
) -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=stack.ops_app), base_url=OPS_URL
    ) as client:
        response = await client.get("/healthz")

    assert (response.status_code, response.json()) == (200, {"status": "ok"})


@pytest.mark.parametrize("token", [None, "not-a-jwt"])
async def test_the_ops_api_refuses_a_missing_or_garbage_token(
    token: str | None, stack: Stack
) -> None:
    assert (await ops_get(stack, token)).status_code == 401


async def test_the_ops_api_refuses_a_token_from_another_idp(stack: Stack) -> None:
    impostor = LocalIdp(own_key=True)
    exchanged = (await impostor.exchange(await impostor.user_token())).json()["access_token"]

    assert (await ops_get(stack, exchanged)).status_code == 401


# --- redaction canaries for the new fields -------------------------------------
def test_exchange_fields_are_redacted_if_they_are_ever_logged() -> None:
    event = {
        "event": "obo_debug",
        "subject_token": "CANARY-SUBJECT-TOKEN",
        "assertion": "CANARY-ASSERTION",
        "form": {"subject_token": "CANARY-NESTED", "audience": OPS_AUDIENCE},
    }

    rendered = json.dumps(redaction_processor(None, "info", dict(event)))

    assert "CANARY" not in rendered
    assert OPS_AUDIENCE in rendered


def test_a_manifest_row_is_redacted_if_it_is_ever_logged() -> None:
    rendered = json.dumps(
        redaction_processor(None, "info", {"event": "oops", "rows": [dict(CANARY_ROW)]})
    )

    assert "CANARY" not in rendered
    assert '"seat": "1A"' in rendered, "operational fields survive; identifying ones do not"


async def test_logging_inside_an_authenticated_call_does_not_leak_the_token(
    logs: LogCapture,
) -> None:
    with authenticated_as("CANARY-INBOUND-TOKEN") as access:
        srv._log.info("tool_debug", token=access.token, authorization=f"Bearer {access.token}")

    assert "CANARY" not in logs.text
