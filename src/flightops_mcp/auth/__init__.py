"""Inbound authentication: this server as an OAuth 2.1 resource server.

Everything that touches the MCP SDK's auth surface lives in this package, so an
SDK bump that moves those APIs is a change to one place.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

import httpx
from mcp.server.auth.middleware.auth_context import get_access_token as current_access_token
from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.settings import AuthSettings
from pydantic import AnyHttpUrl

from ..config import Settings
from .challenge import ScopeChallengingServer
from .jwks import JwksCache
from .passthrough import refuse_inbound_token
from .scopes import Scopes, UnknownToolError
from .verifier import JwtVerifier, TokenRejected

__all__ = [
    "AccessToken",
    "AuthConfigError",
    "AuthSettings",
    "JwksCache",
    "JwtVerifier",
    "ResourceServerAuth",
    "ScopeChallengingServer",
    "Scopes",
    "TokenRejected",
    "TokenVerifier",
    "UnknownToolError",
    "build_resource_server_auth",
    "current_access_token",
    "refuse_inbound_token",
]


class AuthConfigError(Exception):
    """The HTTP transport was asked to start without a complete auth configuration."""


@dataclass(slots=True)
class ResourceServerAuth:
    """What the MCP server needs to authenticate requests over HTTP."""

    settings: AuthSettings
    verifier: JwtVerifier
    jwks: JwksCache


def build_resource_server_auth(
    settings: Settings,
    *,
    jwks_transport: httpx.AsyncBaseTransport | None = None,
    clock: Callable[[], float] = time.time,
) -> ResourceServerAuth:
    """Build the verifier and SDK settings, or refuse.

    There is no unauthenticated HTTP mode. A remote server with a missing issuer
    must fail to start rather than serve everyone; stdio is the local option.
    """
    missing = [
        name
        for name, value in (
            ("FLIGHTOPS_AUTH_ISSUER", settings.auth_issuer),
            ("FLIGHTOPS_AUTH_JWKS_URL", settings.auth_jwks_url),
        )
        if not value
    ]
    if missing:
        raise AuthConfigError(
            "the streamable-http transport requires authentication; set " + " and ".join(missing)
        )
    assert settings.auth_issuer is not None
    assert settings.auth_jwks_url is not None

    jwks = JwksCache(
        settings.auth_jwks_url,
        transport=jwks_transport,
        ttl_s=settings.jwks_cache_ttl_s,
        min_refresh_interval_s=settings.jwks_min_refresh_interval_s,
        timeout_s=settings.http_timeout_s,
        user_agent=settings.user_agent,
    )
    verifier = JwtVerifier(
        issuer=settings.auth_issuer,
        audience=settings.resource_server_url,
        jwks=jwks,
        clock=clock,
        clock_skew_s=settings.auth_clock_skew_s,
    )
    auth = AuthSettings(
        issuer_url=AnyHttpUrl(settings.auth_issuer),
        resource_server_url=AnyHttpUrl(settings.resource_server_url),
        # The verifier already checks `aud`. This makes the SDK check it again
        # from the verifier's report, so removing one check does not remove both.
        validate_token_resource=True,
    )
    return ResourceServerAuth(settings=auth, verifier=verifier, jwks=jwks)
