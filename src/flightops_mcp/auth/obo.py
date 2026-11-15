"""On-behalf-of: trade the caller's token for one a downstream API will accept.

This is the alternative to token passthrough. The caller's token is for this
server and stays here; what goes downstream is a different token, issued by the
IdP for that one API, carrying the user's identity and only the scope the call
needs (RFC 8693 token exchange).

Two properties have tests:

1. **One exchange per identity.** Fifty concurrent calls for the same user,
   audience and scope make one request to the IdP. The cache is keyed by who
   the user is, not by which token they presented, so a client that refreshes
   its token does not trigger a new exchange.
2. **The cache cannot cross users.** The key includes issuer, tenant and
   subject; one user's downstream token is never handed to another's request.

A cached token outlives revocation of the user's own token by at most its
lifetime, which the IdP keeps short. That is the cost of not exchanging on
every call, and it is bounded on purpose.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

import httpx
from mcp.server.auth.provider import AccessToken

from ..config import Settings
from ..errors import ToolFailure, upstream_unavailable
from ..telemetry import get_logger

TOKEN_EXCHANGE_GRANT = "urn:ietf:params:oauth:grant-type:token-exchange"  # noqa: S105 - a URN
ACCESS_TOKEN_TYPE = "urn:ietf:params:oauth:token-type:access_token"  # noqa: S105 - a URN
IDP = "identity provider"

_Key = tuple[str, str, str, str, str]


@dataclass(slots=True)
class _Cached:
    access_token: str
    expires_at: float


class OboExchanger:
    """Exchanges inbound user tokens for downstream tokens, cached per identity."""

    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._settings = settings
        self._clock = clock
        self._cache: dict[_Key, _Cached] = {}
        self._locks: dict[_Key, asyncio.Lock] = {}
        self._log = get_logger(__name__)
        # The one client without the passthrough hook, and the one place the
        # caller's token leaves the process: to its own issuer's token endpoint,
        # as the subject of an exchange. That is presenting a token to the party
        # that minted it, not forwarding it to a third.
        self._client = httpx.AsyncClient(
            timeout=settings.http_timeout_s,
            headers={"User-Agent": settings.user_agent, "Accept": "application/json"},
            transport=transport,
        )
        self.exchange_count = 0

    async def aclose(self) -> None:
        await self._client.aclose()

    def configured(self) -> bool:
        settings = self._settings
        return bool(
            settings.auth_token_url and settings.auth_client_id and settings.auth_client_secret
        )

    async def token_for(self, inbound: AccessToken, *, audience: str, scopes: Sequence[str]) -> str:
        """A token for `audience`, on behalf of the user `inbound` identifies."""
        if not self.configured():
            raise obo_not_configured()
        key = _key(inbound, audience, scopes)
        cached = self._cache.get(key)
        if cached is not None and self._clock() < cached.expires_at:
            return cached.access_token

        async with self._locks.setdefault(key, asyncio.Lock()):
            # Double-checked: the first caller exchanges, the rest find its result.
            cached = self._cache.get(key)
            if cached is not None and self._clock() < cached.expires_at:
                return cached.access_token
            fresh = await self._exchange(inbound, audience, scopes)
            self._prune()
            self._cache[key] = fresh
            return fresh.access_token

    def _prune(self) -> None:
        """Drop expired entries, so the cache holds active users rather than every user ever."""
        now = self._clock()
        for key in [key for key, cached in self._cache.items() if now >= cached.expires_at]:
            del self._cache[key]
            lock = self._locks.get(key)
            if lock is not None and not lock.locked():
                del self._locks[key]

    def invalidate(self, inbound: AccessToken, *, audience: str, scopes: Sequence[str]) -> None:
        """Forget the cached token, after the downstream refused it."""
        self._cache.pop(_key(inbound, audience, scopes), None)

    async def _exchange(
        self, inbound: AccessToken, audience: str, scopes: Sequence[str]
    ) -> _Cached:
        settings = self._settings
        assert settings.auth_token_url is not None
        assert settings.auth_client_id is not None
        assert settings.auth_client_secret is not None
        requested_at = self._clock()
        scope = " ".join(scopes)
        try:
            response = await self._client.post(
                settings.auth_token_url,
                data={
                    "grant_type": TOKEN_EXCHANGE_GRANT,
                    "subject_token": inbound.token,
                    "subject_token_type": ACCESS_TOKEN_TYPE,
                    "requested_token_type": ACCESS_TOKEN_TYPE,
                    "audience": audience,
                    "scope": scope,
                },
                # The server authenticates as itself. Its secret, never the user's token.
                auth=(settings.auth_client_id, settings.auth_client_secret),
            )
        except httpx.HTTPError as exc:
            raise upstream_unavailable(IDP, type(exc).__name__) from exc

        if response.status_code in {400, 401, 403}:
            error = _oauth_error(response)
            self._log.warning(
                "obo_exchange_rejected", audience=audience, scope=scope, idp_error=error
            )
            raise obo_exchange_rejected(error)
        if response.status_code >= 400:
            raise upstream_unavailable(IDP, f"HTTP {response.status_code} from the token endpoint")

        try:
            payload: dict[str, Any] = response.json()
            access_token = str(payload["access_token"])
            ttl = float(payload["expires_in"])
        except (ValueError, KeyError, TypeError) as exc:
            raise upstream_unavailable(IDP, "the exchange response was not usable") from exc

        self.exchange_count += 1
        self._log.info("obo_exchanged", audience=audience, scope=scope, ttl_s=ttl)
        # Stop using the token before it expires, so a call in flight never
        # arrives downstream with a token that died on the way.
        usable_for = max(0.0, ttl - settings.token_expiry_skew_s)
        return _Cached(access_token=access_token, expires_at=requested_at + usable_for)


def _key(inbound: AccessToken, audience: str, scopes: Sequence[str]) -> _Key:
    claims = inbound.claims or {}
    return (
        str(claims.get("iss", "")),
        str(claims.get("tid", "")),
        inbound.subject or inbound.client_id,
        audience,
        " ".join(sorted(scopes)),
    )


def _oauth_error(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return "unknown"
    error = body.get("error") if isinstance(body, dict) else None
    return error if isinstance(error, str) else "unknown"


def obo_not_configured() -> ToolFailure:
    return ToolFailure(
        code="obo_not_configured",
        message="This server is not configured to call downstream APIs on behalf of a user.",
        hint="An operator must set FLIGHTOPS_AUTH_TOKEN_URL, FLIGHTOPS_AUTH_CLIENT_ID and "
        "FLIGHTOPS_AUTH_CLIENT_SECRET. Not retryable; the other tools are unaffected.",
    )


def obo_exchange_rejected(idp_error: str) -> ToolFailure:
    return ToolFailure(
        code="obo_exchange_rejected",
        message="The identity provider refused to issue a downstream token for this user "
        f"({idp_error}).",
        hint="The user's token is expired, revoked, not a user token, or lacks the scope. "
        "Ask the user to sign in again; do not retry with the same token.",
        details={"idp_error": idp_error},
    )
