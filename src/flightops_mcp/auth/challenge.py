"""The scope challenge: HTTP 403 with `WWW-Authenticate` naming the scope needed.

The MCP authorization spec asks for this at the HTTP layer so a client can step
up: read the `scope` attribute, obtain a token that has it, and retry. That
means deciding before the protocol layer runs, which is why this middleware
reads the JSON-RPC body itself.

It is the first of two checks. The tool wrapper in `server.py` repeats the
decision next to the tool, so a request shape this parser does not recognise is
passed through here and still refused there.
"""

from __future__ import annotations

import json
from typing import Any

from mcp.server import MCPServer
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.auth.routes import build_resource_metadata_url
from mcp.server.transport_security import DEFAULT_MAX_REQUEST_BODY_SIZE
from pydantic import AnyHttpUrl
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from ..errors import ToolFailure, insufficient_scope
from ..telemetry import get_logger
from .scopes import Scopes, UnknownToolError

# JSON-RPC reserves -32000..-32099 for the server. The SDK client surfaces a
# JSON-RPC error found in a non-2xx body, so the caller sees this, not a bare 403.
INSUFFICIENT_SCOPE_RPC_CODE = -32003


class ScopeChallengeMiddleware:
    """Refuses a `tools/call` whose token lacks the tool's scope, before it runs."""

    def __init__(self, app: ASGIApp, *, path: str, resource_metadata_url: str) -> None:
        self.app = app
        self._path = path
        self._resource_metadata_url = resource_metadata_url
        self._log = get_logger(__name__)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        user = scope.get("user")
        if (
            scope["type"] != "http"
            or scope["method"] != "POST"
            or scope["path"] != self._path
            # Unauthenticated requests are not ours to answer: the SDK's own
            # middleware, further in, turns them into the 401.
            or not isinstance(user, AuthenticatedUser)
        ):
            await self.app(scope, receive, send)
            return

        consumed, body = await _read_body(receive)
        refusal = _refusal(body, user.scopes) if body is not None else None
        if refusal is None:
            await self.app(scope, _replay(consumed, receive), send)
            return

        request_id, failure = refusal
        self._log.warning(
            "scope_challenge",
            tool=failure.details["tool"],
            missing_scopes=failure.details["missing_scopes"],
            client_id=user.access_token.client_id,
        )
        await self._challenge(send, request_id, failure)

    async def _challenge(self, send: Send, request_id: Any, failure: ToolFailure) -> None:
        # Scope names come from the table in scopes.py, never from the request,
        # so nothing caller-controlled is written into this header.
        challenge = (
            'Bearer error="insufficient_scope", '
            f'scope="{" ".join(failure.details["required_scopes"])}", '
            f'resource_metadata="{self._resource_metadata_url}", '
            'error_description="The access token lacks a scope this tool requires"'
        )
        body = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {
                    "code": INSUFFICIENT_SCOPE_RPC_CODE,
                    "message": failure.message,
                    "data": failure.to_payload(),
                },
            }
        ).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 403,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                    (b"www-authenticate", challenge.encode()),
                    (b"cache-control", b"no-store"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


class ScopeChallengingServer(MCPServer):
    """An MCP server whose HTTP app answers a missing scope with the challenge.

    Overriding the app builder, rather than wrapping its result at the call
    site, keeps `server.run(transport="streamable-http")` as the way to serve.
    """

    def streamable_http_app(self, **kwargs: Any) -> Starlette:
        app = super().streamable_http_app(**kwargs)
        auth = self.settings.auth
        if auth is None or auth.resource_server_url is None:
            raise RuntimeError("the scope challenge needs a resource server URL")
        install_scope_challenge(
            app,
            path=kwargs.get("streamable_http_path", "/mcp"),
            resource_server_url=auth.resource_server_url,
        )
        return app


def install_scope_challenge(app: Starlette, *, path: str, resource_server_url: AnyHttpUrl) -> None:
    """Put the challenge inside the SDK's authentication middleware.

    Appended, not `add_middleware`: Starlette's helper inserts outermost, which
    would run this before the bearer token has been verified.
    """
    app.user_middleware.append(
        Middleware(
            ScopeChallengeMiddleware,
            path=path,
            resource_metadata_url=str(build_resource_metadata_url(resource_server_url)),
        )
    )


def _refusal(body: bytes, granted: list[str]) -> tuple[Any, ToolFailure] | None:
    """The request id and failure for a `tools/call` the token cannot make, else None."""
    try:
        message = json.loads(body)
    except ValueError:
        return None
    if not isinstance(message, dict) or message.get("method") != "tools/call":
        return None
    params = message.get("params")
    name = params.get("name") if isinstance(params, dict) else None
    if not isinstance(name, str):
        return None
    try:
        missing = Scopes.missing(name, granted)
    except UnknownToolError:
        return None  # not a tool; the protocol layer has the right error for that
    if not missing:
        return None
    request_id = message.get("id")
    if isinstance(request_id, bool) or not isinstance(request_id, str | int):
        request_id = None
    return request_id, insufficient_scope(name, Scopes.for_tool(name), missing)


async def _read_body(receive: Receive) -> tuple[list[Message], bytes | None]:
    """Drain the request body, keeping the messages so they can be replayed.

    Returns no body when it is larger than the transport accepts or the client
    went away; the request is then passed on uninspected and the SDK rejects it.
    """
    consumed: list[Message] = []
    chunks: list[bytes] = []
    size = 0
    while True:
        message = await receive()
        consumed.append(message)
        if message["type"] != "http.request":
            return consumed, None
        chunk = message.get("body", b"")
        size += len(chunk)
        if size > DEFAULT_MAX_REQUEST_BODY_SIZE:
            return consumed, None
        chunks.append(chunk)
        if not message.get("more_body", False):
            return consumed, b"".join(chunks)


def _replay(consumed: list[Message], receive: Receive) -> Receive:
    """A `receive` that yields what was already read, then continues from the socket."""
    pending = list(consumed)

    async def replayed() -> Message:
        if pending:
            return pending.pop(0)
        return await receive()

    return replayed
