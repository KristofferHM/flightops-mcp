"""OAuth2 client-credentials token manager for OpenSky Network.

Three behaviours matter here, and each one has a test:

1. **Caching with an expiry skew.** A token is reused until shortly before it
   expires, so a burst of tool calls does not trigger a burst of token requests.
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


class OpenSkyTokenManager:
    """Caches and refreshes an OAuth2 access token for the OpenSky API."""

    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._settings = settings
        self._clock = clock
        self._state: TokenState | None = None
        self._lock = asyncio.Lock()
        self._log = get_logger(__name__)
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
        return bool(self._settings.opensky_client_id and self._settings.opensky_client_secret)

    def cached(self) -> bool:
        return self._state is not None and self._clock() < self._valid_until()

    def invalidate(self) -> None:
        """Drop the cached token; the next call re-acquires one."""
        self._state = None

    async def token(self) -> str:
        if not self.has_credentials():
            raise missing_credentials()

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
        return self._state.expires_at - self._settings.token_expiry_skew_s

    async def _fetch(self) -> TokenState:
        assert self._settings.opensky_client_id is not None
        assert self._settings.opensky_client_secret is not None
        requested_at = self._clock()
        self._log.info("token_request", url=self._settings.opensky_auth_url)
        try:
            response = await self._client.post(
                self._settings.opensky_auth_url,
                data={
                    "grant_type": "client_credentials",
                    "client_id": self._settings.opensky_client_id,
                    "client_secret": self._settings.opensky_client_secret,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        except httpx.HTTPError as exc:
            TOKEN_REFRESHES.labels(outcome="transport_error").inc()
            raise upstream_unavailable("OpenSky auth", f"{type(exc).__name__}: {exc}") from exc

        if response.status_code in {400, 401, 403}:
            TOKEN_REFRESHES.labels(outcome="rejected").inc()
            raise upstream_auth_failed(response.status_code, "OpenSky auth")

        if response.status_code >= 400:
            TOKEN_REFRESHES.labels(outcome="error").inc()
            raise upstream_unavailable(
                "OpenSky auth", f"HTTP {response.status_code} from the token endpoint"
            )

        try:
            payload: dict[str, Any] = response.json()
            access_token = str(payload["access_token"])
        except (ValueError, KeyError, TypeError) as exc:
            TOKEN_REFRESHES.labels(outcome="malformed").inc()
            raise upstream_unavailable(
                "OpenSky auth", f"token response was not usable: {exc}"
            ) from exc

        # `expires_in` is 1800 for OpenSky, but never assume: fall back to five
        # minutes if the field is missing, which is safely inside the real TTL.
        ttl_raw = payload.get("expires_in", 300)
        try:
            ttl = float(ttl_raw)
        except (TypeError, ValueError):
            ttl = 300.0

        TOKEN_REFRESHES.labels(outcome="ok").inc()
        self.refresh_count += 1
        self._log.info("token_acquired", ttl_s=ttl, refresh_count=self.refresh_count)
        return TokenState(access_token=access_token, expires_at=requested_at + ttl)


def is_auth_failure(exc: BaseException) -> bool:
    return isinstance(exc, ToolFailure) and exc.code == "upstream_auth_failed"
