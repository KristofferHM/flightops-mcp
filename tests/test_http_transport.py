"""The remote server over Streamable HTTP: authenticated or refused.

These go through the real ASGI app. The bearer middleware, the protocol layer
and the tools are the production objects; only the JWKS fetch and the upstream
APIs are mock transports.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from mcp.server import MCPServer

from flightops_mcp import server as srv
from flightops_mcp.auth import AuthConfigError, Scopes
from flightops_mcp.config import Settings

from .authkit import ISSUER, RESOURCE, JwksEndpoint, signing_key
from .conftest import LogCapture
from .harness import RemoteServer, remote_settings, serve, tool_payload
from .test_server_tools import EXPECTED_TOOLS, call, install

KEY = signing_key("key-1")

AUTH_ENV = ("FLIGHTOPS_AUTH_ISSUER", "FLIGHTOPS_AUTH_JWKS_URL")


@pytest.fixture
async def remote(settings: Settings) -> AsyncIterator[RemoteServer]:
    configured = remote_settings(settings)
    install(configured)
    async with serve(configured, JwksEndpoint(KEY).transport) as server:
        yield server


# --- unauthenticated requests ------------------------------------------------
async def test_a_request_without_a_token_gets_401_and_a_pointer_to_the_metadata(
    remote: RemoteServer,
) -> None:
    response = await remote.rpc("tools/list")

    assert response.status_code == 401
    challenge = response.headers["www-authenticate"]
    assert challenge.startswith("Bearer ")
    assert 'error="invalid_token"' in challenge
    assert (
        'resource_metadata="http://127.0.0.1:8000/.well-known/oauth-protected-resource/mcp"'
        in challenge
    )


@pytest.mark.parametrize(
    "authorization",
    ["Basic dXNlcjpwYXNz", "Bearer", "Bearer not-a-jwt", "bearer  "],
)
async def test_a_request_with_an_unusable_authorization_header_gets_401(
    authorization: str, remote: RemoteServer
) -> None:
    response = await remote.rpc("tools/list", headers={"Authorization": authorization})
    assert response.status_code == 401


async def test_an_expired_token_gets_401_and_the_reason_is_logged(
    remote: RemoteServer, logs: LogCapture
) -> None:
    token = KEY.mint(expires_in=-60)

    response = await remote.rpc("tools/list", token=token)

    assert response.status_code == 401
    assert logs.events("token_rejected")[-1]["reason"] == "expired"
    assert token not in logs.text


async def test_a_token_from_an_untrusted_signer_gets_401(remote: RemoteServer) -> None:
    response = await remote.rpc("tools/list", token=signing_key("key-2").mint())
    assert response.status_code == 401


async def test_no_tool_runs_for_an_unauthenticated_caller(remote: RemoteServer) -> None:
    response = await remote.call_tool("resolve_airport", {"query": "ESGG"})

    assert response.status_code == 401
    assert "matches" not in response.text


# --- authenticated requests --------------------------------------------------
async def test_an_authenticated_client_sees_every_tool(remote: RemoteServer) -> None:
    async with remote.client(KEY.mint()) as client:
        tools = await client.list_tools()

    assert {tool.name for tool in tools.tools} == EXPECTED_TOOLS


async def test_an_authenticated_client_can_call_a_tool(remote: RemoteServer) -> None:
    async with remote.client(KEY.mint(scp=Scopes.REFERENCE_READ)) as client:
        payload = await call(client, "resolve_airport", {"query": "ESGG"})

    assert payload["ok"] is True
    assert payload["matches"][0]["icao"] == "ESGG"


async def test_a_bare_http_post_with_a_token_is_served_statelessly(remote: RemoteServer) -> None:
    """No initialize handshake and no session id: one POST in, one JSON response out."""
    response = await remote.call_tool(
        "resolve_airport", {"query": "GOT"}, token=KEY.mint(scp=Scopes.REFERENCE_READ)
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    assert "mcp-session-id" not in response.headers
    assert tool_payload(response)["matches"][0]["icao"] == "ESGG"


async def test_the_protected_resource_metadata_names_the_issuer(remote: RemoteServer) -> None:
    """RFC 9728: how a client that got the 401 finds out where to get a token."""
    response = await remote.raw.get("/.well-known/oauth-protected-resource/mcp")

    assert response.status_code == 200
    document = response.json()
    assert document["resource"] == RESOURCE
    assert document["authorization_servers"] == [f"{ISSUER}/"]
    assert document["bearer_methods_supported"] == ["header"]


# --- configuration -----------------------------------------------------------
def test_the_http_server_cannot_be_built_without_an_issuer(settings: Settings) -> None:
    with pytest.raises(AuthConfigError) as excinfo:
        srv.create_http_server(settings)

    assert "FLIGHTOPS_AUTH_ISSUER" in str(excinfo.value)
    assert "FLIGHTOPS_AUTH_JWKS_URL" in str(excinfo.value)


def test_the_cli_refuses_to_serve_http_without_authentication(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    for name in AUTH_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)  # no stray .env
    monkeypatch.setattr(MCPServer, "run", _must_not_run)

    with pytest.raises(SystemExit) as excinfo:
        srv.main(["--transport", "streamable-http"])

    assert excinfo.value.code == 2
    assert "requires authentication" in capsys.readouterr().err


def test_the_cli_serves_http_statelessly_once_authentication_is_configured(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("FLIGHTOPS_AUTH_ISSUER", ISSUER)
    monkeypatch.setenv("FLIGHTOPS_AUTH_JWKS_URL", f"{ISSUER}/.well-known/jwks.json")
    monkeypatch.chdir(tmp_path)
    started: dict[str, Any] = {}

    def fake_run(self: MCPServer, transport: str = "stdio", **kwargs: Any) -> None:
        started.update(kwargs, transport=transport, authenticated=self.settings.auth is not None)

    monkeypatch.setattr(MCPServer, "run", fake_run)

    srv.main(["--transport", "streamable-http", "--host", "192.0.2.10", "--port", "8123"])

    assert started == {
        "transport": "streamable-http",
        "host": "192.0.2.10",
        "port": 8123,
        "stateless_http": True,
        "json_response": True,
        "authenticated": True,
    }


def test_the_stdio_server_stays_unauthenticated() -> None:
    """Local stdio has no bearer token to check; the process boundary is the trust boundary."""
    assert srv.mcp.settings.auth is None


def _must_not_run(*_args: Any, **_kwargs: Any) -> None:
    raise AssertionError("the server started without authentication")
