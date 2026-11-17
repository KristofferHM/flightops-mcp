"""No token passthrough, in both directions.

Inbound: a token minted for another audience is refused, however valid it is.
Outbound: the caller's token is never sent to an upstream, and an attempt to
send it is blocked and logged before the request leaves the process.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import httpx
import pytest
from mcp.server.auth.provider import AccessToken

from flightops_mcp import server as srv
from flightops_mcp.auth import AuthSettings, JwksCache, JwtVerifier, Scopes
from flightops_mcp.config import Settings
from flightops_mcp.errors import ToolFailure
from flightops_mcp.http import UpstreamHttp
from flightops_mcp.opensky_token import OpenSkyTokenManager
from flightops_mcp.runtime import Runtime, set_runtime

from .authkit import (
    ISSUER,
    JWKS_URL,
    RESOURCE,
    JwksEndpoint,
    authenticated_as,
    signing_key,
)
from .conftest import FakeClock, LogCapture
from .harness import BASE_URL, _lifespan, remote_settings, serve, tool_payload
from .idpkit import OPS_AUDIENCE, PASSENGERS_READ, LocalIdp
from .test_server_tools import VFR_METAR, install

KEY = signing_key("key-1")
SRC = Path(__file__).parent.parent / "src" / "flightops_mcp"


def verifier(clock: FakeClock) -> JwtVerifier:
    jwks = JwksCache(JWKS_URL, transport=JwksEndpoint(KEY).transport, clock=clock)
    return JwtVerifier(issuer=ISSUER, audience=RESOURCE, jwks=jwks, clock=clock)


class Recorder:
    """An upstream that records every request it is sent."""

    def __init__(self, respond: Any = None) -> None:
        self.requests: list[httpx.Request] = []
        self._respond = respond or (lambda _request: httpx.Response(200, json={}))

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        response: httpx.Response = self._respond(request)
        return response

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)

    def everything_sent(self) -> str:
        return "\n".join(
            f"{request.url} {dict(request.headers)!r} {request.content!r}"
            for request in self.requests
        )


# --- inbound: audience binding -----------------------------------------------
async def test_a_token_for_another_audience_is_rejected_and_the_reason_is_logged(
    clock: FakeClock, logs: LogCapture
) -> None:
    token = KEY.mint(now=clock(), audience=OPS_AUDIENCE, scp=Scopes.WEATHER_READ)

    assert await verifier(clock).verify_token(token) is None

    rejection = logs.events("token_rejected")[-1]
    assert rejection["reason"] == "wrong_audience"
    assert rejection["aud"] == OPS_AUDIENCE
    assert rejection["expected"] == RESOURCE
    assert token not in logs.text


async def test_a_token_with_no_audience_is_rejected(clock: FakeClock, logs: LogCapture) -> None:
    token = KEY.mint(now=clock(), audience=None)

    assert await verifier(clock).verify_token(token) is None
    assert logs.events("token_rejected")[-1]["reason"] == "wrong_audience"


@pytest.mark.parametrize(
    "audience",
    [
        "http://127.0.0.1:8000",
        "http://127.0.0.1:8000/mcpx",
        "http://127.0.0.1:8000/mcp/admin",
        "http://127.0.0.1:8000/mcp?x=1",
        "https://127.0.0.1:8000/mcp",
        "HTTP://127.0.0.1:8000/MCP",
        "evil.test/http://127.0.0.1:8000/mcp",
        "",
        "*",
    ],
)
async def test_a_lookalike_audience_is_rejected(audience: str, clock: FakeClock) -> None:
    token = KEY.mint(now=clock(), audience=audience)
    assert await verifier(clock).verify_token(token) is None


@pytest.mark.parametrize("audience", [7, True, {"aud": RESOURCE}, [7], [[RESOURCE]], None])
async def test_a_malformed_audience_claim_is_rejected(audience: Any, clock: FakeClock) -> None:
    token = KEY.mint(now=clock(), audience=None, aud=audience)
    assert await verifier(clock).verify_token(token) is None


async def test_this_server_among_several_audiences_is_accepted(clock: FakeClock) -> None:
    """RFC 7519: `aud` may be an array; the server must find itself in it."""
    with_us = KEY.mint(now=clock(), audience=None, aud=[OPS_AUDIENCE, RESOURCE])
    without_us = KEY.mint(now=clock(), audience=None, aud=[OPS_AUDIENCE, "api://reporting"])

    assert await verifier(clock).verify_token(with_us) is not None
    assert await verifier(clock).verify_token(without_us) is None


async def test_a_trailing_slash_does_not_make_a_different_audience(clock: FakeClock) -> None:
    accepted = await verifier(clock).verify_token(KEY.mint(now=clock(), audience=f"{RESOURCE}/"))

    assert accepted is not None
    assert accepted.resource == RESOURCE, "the SDK's own audience check reads this field"


async def test_a_wrong_audience_token_gets_401_and_no_tool_runs(
    settings: Settings, logs: LogCapture
) -> None:
    runtime = install(remote_settings(settings))
    token = KEY.mint(audience=OPS_AUDIENCE, scp=" ".join(sorted(Scopes.all())))

    async with serve(runtime.settings, JwksEndpoint(KEY).transport) as remote:
        response = await remote.call_tool("airport_live_traffic", {"airport": "ESGG"}, token=token)

    assert response.status_code == 401, "wrong audience is unauthenticated, not merely forbidden"
    assert 'error="invalid_token"' in response.headers["www-authenticate"]
    assert runtime.opensky.credits_spent_total == 0
    assert logs.events("token_rejected")[-1]["reason"] == "wrong_audience"


async def test_a_token_exchanged_for_the_downstream_api_cannot_be_replayed_here(
    settings: Settings, logs: LogCapture
) -> None:
    """A real passthrough attempt: a genuine token from our issuer, for another API."""
    idp = LocalIdp()
    runtime = install(remote_settings(settings))
    exchanged = (await idp.exchange(await idp.user_token(), scope=PASSENGERS_READ)).json()
    downstream_token = exchanged["access_token"]

    async with serve(runtime.settings, idp.transport) as remote:
        replayed = await remote.rpc("tools/list", token=downstream_token)
        legitimate = await remote.rpc("tools/list", token=await idp.user_token())

    assert replayed.status_code == 401
    assert legitimate.status_code == 200
    rejection = logs.events("token_rejected")[-1]
    assert rejection["reason"] == "wrong_audience"
    assert rejection["aud"] == OPS_AUDIENCE
    assert downstream_token not in logs.text


async def test_the_sdk_refuses_a_wrong_audience_even_if_the_verifier_forgets_to(
    settings: Settings,
) -> None:
    """The second, independent check: a verifier that accepts everything is not enough."""

    class CarelessVerifier:
        async def verify_token(self, token: str) -> AccessToken | None:
            return AccessToken(
                token=token, client_id="x", scopes=sorted(Scopes.all()), resource=OPS_AUDIENCE
            )

    configured = remote_settings(settings)
    install(configured)
    server = srv.create_server(
        auth=AuthSettings(
            issuer_url=ISSUER,  # type: ignore[arg-type]
            resource_server_url=RESOURCE,  # type: ignore[arg-type]
            validate_token_resource=True,
        ),
        token_verifier=CarelessVerifier(),
    )
    app = server.streamable_http_app(**srv.HTTP_OPTIONS)

    async with (
        _lifespan(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE_URL) as raw,
    ):
        response = await raw.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            headers={"Authorization": "Bearer anything-at-all"},
        )

    assert response.status_code == 401


def test_the_server_is_configured_to_have_the_sdk_check_the_audience(settings: Settings) -> None:
    server = srv.create_http_server(
        remote_settings(settings), jwks_transport=JwksEndpoint(KEY).transport
    )

    assert server.settings.auth is not None
    assert server.settings.auth.validate_token_resource is True


# --- outbound: the caller's token stays here ----------------------------------
async def test_an_authenticated_call_never_sends_the_callers_token_upstream(
    settings: Settings,
) -> None:
    """End to end: every upstream is called, none of them sees the inbound token."""
    upstream = Recorder(
        lambda request: (
            httpx.Response(200, json={"access_token": "servers-own-token", "expires_in": 1800})
            if request.method == "POST"
            else httpx.Response(200, json=VFR_METAR)
            if "/metar" in request.url.path
            else httpx.Response(200, json=[])
            if "/flights/" in request.url.path or "/taf" in request.url.path
            else httpx.Response(200, json={"time": 1_700_000_000, "states": []})
        )
    )
    configured = remote_settings(settings)
    install(configured)  # builds the airport database
    runtime = Runtime.build(
        configured,
        opensky_transport=upstream.transport,
        token_transport=upstream.transport,
        weather_transport=upstream.transport,
    )
    set_runtime(runtime)
    token = KEY.mint(scp=f"{Scopes.WEATHER_READ} {Scopes.TRAFFIC_READ}")

    async with serve(configured, JwksEndpoint(KEY).transport) as remote:
        response = await remote.call_tool("disruption_triage", {"airport": "ESGG"}, token=token)

    assert tool_payload(response)["ok"] is True
    hosts = {request.url.host for request in upstream.requests}
    assert hosts == {"opensky-network.org", "auth.opensky-network.org", "aviationweather.gov"}
    assert token not in upstream.everything_sent()
    opensky_auth = {
        request.headers["authorization"]
        for request in upstream.requests
        if request.url.host == "opensky-network.org"
    }
    assert opensky_auth == {"Bearer servers-own-token"}, "upstream calls use the server's identity"


@pytest.mark.parametrize(
    ("request_kwargs", "found_in"),
    [
        ({"headers": {"Authorization": "Bearer {token}"}}, "header:authorization"),
        ({"headers": {"X-Forwarded-Authorization": "{token}"}}, "header:x-forwarded-authorization"),
        ({"params": {"access_token": "{token}"}}, "url"),
        ({"data": {"assertion": "{token}"}}, "body"),
    ],
)
async def test_an_attempt_to_forward_the_token_is_blocked_and_the_reason_is_logged(
    request_kwargs: dict[str, dict[str, str]],
    found_in: str,
    settings: Settings,
    logs: LogCapture,
) -> None:
    upstream = Recorder()
    client = UpstreamHttp(
        settings, "partner-api", "https://partner.example", transport=upstream.transport
    )
    token = KEY.mint()
    kwargs = {
        name: {key: value.format(token=token) for key, value in values.items()}
        for name, values in request_kwargs.items()
    }

    with authenticated_as(token), pytest.raises(ToolFailure) as excinfo:
        await client.request_json("POST", "/bookings", **kwargs)  # type: ignore[arg-type]

    assert excinfo.value.code == "token_passthrough_blocked"
    assert upstream.requests == [], "the request must not have left the process"
    blocked = logs.events("token_passthrough_blocked")[-1]
    assert blocked["reason"] == "outbound request carried the caller's access token"
    assert blocked["found_in"] == found_in
    assert blocked["upstream_host"] == "partner.example"
    # Which call tried it, so the operator can find the code. Unasserted until mutation
    # testing showed either field could be dropped without a test noticing.
    assert blocked["method"] == "POST"
    assert blocked["path"] == "/bookings"
    assert blocked["level"] == "error"
    assert token not in logs.text, "the log names where the token was, never the token"


async def test_a_tool_that_forwards_the_token_returns_a_failure_instead_of_raising(
    settings: Settings,
) -> None:
    upstream = Recorder()
    client = UpstreamHttp(
        settings, "partner-api", "https://partner.example", transport=upstream.transport
    )
    token = KEY.mint()

    @srv.guarded
    async def leaky_tool() -> dict[str, Any]:
        await client.request_json("GET", "/me", headers={"Authorization": f"Bearer {token}"})
        return {"ok": True}

    with authenticated_as(token):
        payload = await leaky_tool()

    assert payload["ok"] is False
    assert payload["error"]["code"] == "token_passthrough_blocked"
    assert payload["error"]["details"] == {"upstream_host": "partner.example"}
    assert token not in str(payload)
    assert upstream.requests == []


async def test_the_guard_is_inert_when_there_is_no_inbound_token(settings: Settings) -> None:
    """stdio has no caller token, so there is nothing to forward and nothing to block."""
    upstream = Recorder()
    client = UpstreamHttp(
        settings, "partner-api", "https://partner.example", transport=upstream.transport
    )

    response = await client.request_json("GET", "/me", headers={"Authorization": "Bearer other"})

    assert response.status_code == 200
    assert len(upstream.requests) == 1


async def test_a_different_bearer_token_is_allowed_while_authenticated(settings: Settings) -> None:
    """The guard blocks the caller's token, not credentials in general."""
    upstream = Recorder()
    client = UpstreamHttp(
        settings, "partner-api", "https://partner.example", transport=upstream.transport
    )

    with authenticated_as(KEY.mint()):
        response = await client.request_json(
            "GET", "/me", headers={"Authorization": "Bearer servers-own-token"}
        )

    assert response.status_code == 200
    assert upstream.requests[0].headers["authorization"] == "Bearer servers-own-token"


async def test_the_opensky_token_client_is_guarded_too(settings: Settings) -> None:
    """The client-credentials request must never carry the caller's token as its secret."""
    upstream = Recorder()
    token = KEY.mint()
    leaky = settings.model_copy(update={"opensky_client_secret": token})
    manager = OpenSkyTokenManager(leaky, transport=upstream.transport)

    with authenticated_as(token), pytest.raises(ToolFailure) as excinfo:
        await manager.token()

    assert excinfo.value.code == "token_passthrough_blocked"
    assert upstream.requests == []


def test_every_outbound_http_client_installs_the_guard() -> None:
    """A structural check: a new upstream client cannot be added without the hook."""
    # The exchange client presents the caller's token to the issuer that minted
    # it, as the subject of an RFC 8693 exchange. That is its whole job.
    exempt = {SRC / "auth" / "obo.py"}
    unguarded: list[str] = []
    clients = 0
    for path in sorted(set(SRC.rglob("*.py")) - exempt):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not (isinstance(node, ast.Call) and ast.unparse(node.func) == "httpx.AsyncClient"):
                continue
            clients += 1
            hooks = next((kw.value for kw in node.keywords if kw.arg == "event_hooks"), None)
            if hooks is None or "refuse_inbound_token" not in ast.unparse(hooks):
                unguarded.append(f"{path.relative_to(SRC)}:{node.lineno}")

    assert clients >= 3, "the scan found fewer clients than exist; fix the scan"
    assert unguarded == []
