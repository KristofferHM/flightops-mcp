"""The authenticated HTTP server, in process.

Requests go through the real Starlette app (authentication middleware, protocol
handling, tool code) over an ASGI transport. No socket is opened, so the suite
stays hermetic, but nothing between the HTTP request and the tool is mocked.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import httpx
import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.server import MCPServer
from starlette.applications import Starlette

from flightops_mcp import server as srv
from flightops_mcp.config import Settings

from .authkit import ISSUER, JWKS_URL, RESOURCE

BASE_URL = "http://127.0.0.1:8000"


def remote_settings(settings: Settings) -> Settings:
    """The suite's settings, plus the configuration the HTTP transport demands."""
    return settings.model_copy(
        update={
            "auth_issuer": ISSUER,
            "auth_jwks_url": JWKS_URL,
            "resource_server_url": RESOURCE,
        }
    )


@dataclass(slots=True)
class RemoteServer:
    server: MCPServer
    app: Starlette
    raw: httpx.AsyncClient

    async def rpc(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        token: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        """One JSON-RPC request as a bare HTTP POST, for asserting on the HTTP layer."""
        body: dict[str, Any] = {"jsonrpc": "2.0", "id": 1, "method": method}
        if params is not None:
            body["params"] = params
        sent = {"Accept": "application/json, text/event-stream", **(headers or {})}
        if token is not None:
            sent["Authorization"] = f"Bearer {token}"
        return await self.raw.post("/mcp", json=body, headers=sent)

    async def call_tool(
        self, name: str, arguments: dict[str, Any] | None = None, *, token: str | None = None
    ) -> httpx.Response:
        return await self.rpc(
            "tools/call", {"name": name, "arguments": arguments or {}}, token=token
        )

    @asynccontextmanager
    async def client(self, token: str) -> AsyncIterator[Client]:
        """The SDK's own client, authenticated, speaking the real wire protocol."""
        async with (
            httpx2.AsyncClient(
                transport=httpx2.ASGITransport(app=self.app),
                headers={"Authorization": f"Bearer {token}"},
            ) as http_client,
            Client(streamable_http_client(f"{BASE_URL}/mcp", http_client=http_client)) as client,
        ):
            yield client


@asynccontextmanager
async def serve(
    settings: Settings,
    jwks_transport: httpx.AsyncBaseTransport,
    *,
    http_challenge: bool = True,
) -> AsyncIterator[RemoteServer]:
    """Run the remote server for the duration of a test.

    `http_challenge=False` builds the SDK's app without the scope-challenge
    middleware, to prove the check next to the tool holds on its own.
    """
    server = srv.create_http_server(settings, jwks_transport=jwks_transport)
    if http_challenge:
        app = server.streamable_http_app(**srv.HTTP_OPTIONS)
    else:
        app = MCPServer.streamable_http_app(server, **srv.HTTP_OPTIONS)
    async with (
        _lifespan(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE_URL) as raw,
    ):
        yield RemoteServer(server=server, app=app, raw=raw)


@asynccontextmanager
async def _lifespan(app: Starlette) -> AsyncIterator[None]:
    """Hold the app's lifespan open from a task of its own.

    The SDK's session manager owns a task group, which must be exited by the task
    that entered it. pytest-asyncio runs fixture setup and teardown in different
    tasks, so the lifespan cannot be entered directly from a fixture.
    """
    started = asyncio.Event()
    stop = asyncio.Event()

    async def hold() -> None:
        try:
            async with app.router.lifespan_context(app):
                started.set()
                await stop.wait()
        finally:
            started.set()  # never leave the caller waiting on a failed startup

    task = asyncio.create_task(hold())
    await started.wait()
    try:
        if task.done():
            task.result()  # re-raise a startup failure
        yield
    finally:
        stop.set()
        await task


def tool_payload(response: httpx.Response) -> dict[str, Any]:
    """The tool's structured payload out of a raw `tools/call` response."""
    result = response.json()["result"]
    structured = result.get("structuredContent")
    assert isinstance(structured, dict), f"no structured payload in {result!r}"
    return structured
