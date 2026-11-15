"""No token passthrough: the caller's token never leaves for an upstream API.

The MCP authorization spec forbids forwarding the token a client presented, and
for good reason. That token was issued for *this* server. Sent upstream it either
fails, or worse, works: the upstream then acts on the caller's authority for a
request this server composed, and its audit log shows the wrong principal. That
is the confused deputy. Upstream calls use this server's own credentials, or a
token obtained for that upstream by exchange.

Nothing in the codebase forwards the token today. This hook exists so that a
future change which does so fails loudly on its first request instead of
shipping. It is installed on every upstream HTTP client.
"""

from __future__ import annotations

import httpx
from mcp.server.auth.middleware.auth_context import get_access_token

from ..errors import token_passthrough_blocked
from ..telemetry import get_logger

_log = get_logger(__name__)


async def refuse_inbound_token(request: httpx.Request) -> None:
    """httpx request hook: abort any outbound request that carries the inbound token."""
    inbound = get_access_token()
    if inbound is None:
        return  # stdio, or a background call outside any request: nothing to leak
    where = _carried_in(request, inbound.token)
    if where is None:
        return
    host = request.url.host
    # The token itself is never logged; where it was found is enough to fix the bug.
    _log.error(
        "token_passthrough_blocked",
        reason="outbound request carried the caller's access token",
        found_in=where,
        upstream_host=host,
        method=request.method,
        path=request.url.path,
    )
    raise token_passthrough_blocked(host)


def _carried_in(request: httpx.Request, token: str) -> str | None:
    """Where the token appears in the request, or None. Any header counts, not only
    `Authorization`: a token smuggled in `X-Forwarded-Authorization` is still forwarded."""
    for name, value in request.headers.items():
        if token in value:
            return f"header:{name.lower()}"
    if token in str(request.url):
        return "url"
    try:
        body = request.content
    except httpx.RequestNotRead:
        return None  # a streaming body; no upstream call in this server has one
    if token.encode() in body:
        return "body"
    return None
