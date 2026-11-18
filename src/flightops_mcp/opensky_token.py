"""OAuth2 client-credentials token manager, shared by every upstream that uses the grant.

Written for OpenSky first, and the reason it is not OpenSky's alone is the second
adapter: Schiphol uses the same grant with one more form field (`audience`) and a
different refresh margin. Those became constructor parameters. Nothing else changed,
so both upstreams get the same three behaviours, each one with a test:

1. **Caching with an expiry skew.** A token is reused until shortly before it
   expires, so a burst of tool calls does not trigger a burst of token requests.
   For Schiphol the skew is the vendor's own advice: refresh 60 s before expiry.
2. **Single flight.** Fifty concurrent callers that all find an empty cache must
   produce exactly *one* token request. Without the lock this is the classic
   thundering herd: every coroutine sees `None`, every coroutine posts.
3. **Invalidation on 401.** OpenSky revokes tokens, and a revoked token returns
   401 with internal code 38193. The client drops the cached token and tries
   once more rather than failing the user's request.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx

from .auth.passthrough import refuse_inbound_token
from .config import Settings
from .errors import ToolFailure, missing_credentials, upstream_auth_failed, upstream_unavailable
from .telemetry import TOKEN_REFRESHES, get_logger

SleepFn = Callable[[float], "asyncio.Future[None] | Any"]


@dataclass(slots=True)
class TokenState:
    access_token: str
    expires_at: float


class ClientCredentialsTokenManager:
    """Caches and refreshes one OAuth2 client-credentials access token."""

    def __init__(
        self,
        settings: Settings,
        *,
        upstream: str,
        token_url: str,
        client_id: str | None,
        client_secret: str | None,
        audience: str | None = None,
        skew_s: float | None = None,
        missing: Callable[[], ToolFailure] = missing_credentials,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._settings = settings
        self._upstream = upstream
        self._token_url = token_url
        self._client_id = client_id
        self._client_secret = client_secret
        self._audience = audience
        self._skew_s = settings.token_expiry_skew_s if skew_s is None else skew_s
        self._missing = missing
        self._clock = clock
        self._state: TokenState | None = None
        self._lock = asyncio.Lock()
        self._log = get_logger(__name__).bind(upstream=upstream)
        self._client = httpx.AsyncClient(
            timeout=settings.http_timeout_s,
            headers={"User-Agent": settings.user_agent},
            transport=transport,
            event_hooks={"request": [refuse_inbound_token]},
        )
        self.refresh_count = 0

    async def aclose(self) -> None:
        await self._client.aclose()

    # --- public API ---------------------------------------------------------
    def has_credentials(self) -> bool:
        return bool(self._client_id and self._client_secret)

    def cached(self) -> bool:
        return self._state is not None and self._clock() < self._valid_until()

    def invalidate(self) -> None:
        """Drop the cached token; the next call re-acquires one."""
        self._state = None

    async def token(self) -> str:
        if not self.has_credentials():
            raise self._missing()

        if self._state is not None and self._clock() < self._valid_until():
            return self._state.access_token

        async with self._lock:
            # Double-checked locking: the winner refreshes, everyone else finds
            # a fresh token here and returns without a second network call.
            if self._state is not None and self._clock() < self._valid_until():
                return self._state.access_token
            self._state = await self._fetch()
            return self._state.access_token

    # --- internals ----------------------------------------------------------
    def _valid_until(self) -> float:
        assert self._state is not None
        return self._state.expires_at - self._skew_s

    async def _fetch(self) -> TokenState:
        assert self._client_id is not None
        assert self._client_secret is not None
        label = f"{self._upstream} auth"
        requested_at = self._clock()
        form = {
            "grant_type": "client_credentials",
            "client_id": self._client_id,
            "client_secret": self._client_secret,
        }
        if self._audience is not None:
            # Auth0's form of RFC 8707's `resource`: which API the token is for.
            form["audience"] = self._audience
        self._log.info("token_request", url=self._token_url)
        try:
            response = await self._client.post(
                self._token_url,
                data=form,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        except httpx.HTTPError as exc:
            TOKEN_REFRESHES.labels(outcome="transport_error").inc()
            raise upstream_unavailable(label, f"{type(exc).__name__}: {exc}") from exc

        if response.status_code in {400, 401, 403}:
            TOKEN_REFRESHES.labels(outcome="rejected").inc()
            raise upstream_auth_failed(response.status_code, label)

        if response.status_code >= 400:
            TOKEN_REFRESHES.labels(outcome="error").inc()
            raise upstream_unavailable(
                label, f"HTTP {response.status_code} from the token endpoint"
            )

        try:
            payload: dict[str, Any] = response.json()
            access_token = str(payload["access_token"])
        except (ValueError, KeyError, TypeError) as exc:
            TOKEN_REFRESHES.labels(outcome="malformed").inc()
            raise upstream_unavailable(label, f"token response was not usable: {exc}") from exc

        # `expires_in` is 1800 for both upstreams today, but never assume: fall back
        # to five minutes if the field is missing, which is safely inside the real TTL.
        ttl_raw = payload.get("expires_in", 300)
        try:
            ttl = float(ttl_raw)
        except (TypeError, ValueError):
            ttl = 300.0

        TOKEN_REFRESHES.labels(outcome="ok").inc()
        self.refresh_count += 1
        self._log.info("token_acquired", ttl_s=ttl, refresh_count=self.refresh_count)
        return TokenState(access_token=access_token, expires_at=requested_at + ttl)


class OpenSkyTokenManager(ClientCredentialsTokenManager):
    """The OpenSky configuration of the shared manager: no audience, the default skew."""

    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        super().__init__(
            settings,
            upstream="OpenSky",
            token_url=settings.opensky_auth_url,
            client_id=settings.opensky_client_id,
            client_secret=settings.opensky_client_secret,
            transport=transport,
            clock=clock,
        )


def is_auth_failure(exc: BaseException) -> bool:
    return isinstance(exc, ToolFailure) and exc.code == "upstream_auth_failed"
