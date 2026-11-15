"""JWKS cache: the public keys this server trusts to have signed a token.

Three behaviours matter, and each one has a test:

1. **Single flight.** A cold cache hit by fifty concurrent requests fetches the
   key set once, not fifty times.
2. **Rotation without restart.** A token signed by a key id the cache has not
   seen triggers one refetch, which is how a rotated signing key is picked up.
3. **A refetch floor.** That same path is reachable by anyone who can send a
   forged `kid`, so refetches are rate-limited. Without the floor an attacker
   turns this server into a request amplifier aimed at the identity provider.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import Any

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicKey

from ..telemetry import get_logger
from .passthrough import refuse_inbound_token


class JwksCache:
    """Fetches and caches the RSA signing keys published at one JWKS URL."""

    def __init__(
        self,
        url: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
        ttl_s: float = 3600.0,
        min_refresh_interval_s: float = 30.0,
        timeout_s: float = 5.0,
        user_agent: str = "flightops-mcp",
    ) -> None:
        self._url = url
        self._clock = clock
        self._ttl_s = ttl_s
        self._min_refresh_interval_s = min_refresh_interval_s
        self._keys: dict[str, RSAPublicKey] = {}
        self._fetched_at: float | None = None
        self._attempted_at: float | None = None
        self._lock = asyncio.Lock()
        self._log = get_logger(__name__)
        self._client = httpx.AsyncClient(
            timeout=timeout_s,
            headers={"User-Agent": user_agent, "Accept": "application/json"},
            transport=transport,
            event_hooks={"request": [refuse_inbound_token]},
        )
        self.fetch_count = 0

    async def aclose(self) -> None:
        await self._client.aclose()

    async def key_for(self, kid: str) -> RSAPublicKey | None:
        """The public key for `kid`, or None if the issuer does not publish it."""
        if self._fresh() and kid in self._keys:
            return self._keys[kid]

        async with self._lock:
            # Double-checked: the first waiter refetched, the rest find the key here.
            if self._fresh() and kid in self._keys:
                return self._keys[kid]
            if self._may_refresh():
                await self._refresh()
            # A stale-but-known key is still served when the issuer is unreachable.
            # Failing every request because the IdP blipped would turn its outage
            # into ours; a key that was valid an hour ago is the smaller risk.
            return self._keys.get(kid)

    # --- internals ----------------------------------------------------------
    def _fresh(self) -> bool:
        return self._fetched_at is not None and self._clock() - self._fetched_at < self._ttl_s

    def _may_refresh(self) -> bool:
        if self._attempted_at is None:
            return True
        return self._clock() - self._attempted_at >= self._min_refresh_interval_s

    async def _refresh(self) -> None:
        self._attempted_at = self._clock()
        self.fetch_count += 1
        try:
            response = await self._client.get(self._url)
            response.raise_for_status()
            document: Any = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            self._log.warning("jwks_fetch_failed", url=self._url, error_class=type(exc).__name__)
            return

        keys = _parse_keys(document)
        if not keys:
            self._log.warning("jwks_has_no_usable_keys", url=self._url)
            return
        self._keys = keys
        self._fetched_at = self._attempted_at
        self._log.info("jwks_refreshed", url=self._url, key_ids=sorted(keys))


def _parse_keys(document: Any) -> dict[str, RSAPublicKey]:
    """Keep RSA signature keys only; skip everything else rather than fail the set."""
    if not isinstance(document, dict) or not isinstance(document.get("keys"), list):
        return {}
    keys: dict[str, RSAPublicKey] = {}
    for entry in document["keys"]:
        if not isinstance(entry, dict):
            continue
        kid = entry.get("kid")
        if not isinstance(kid, str) or not kid:
            continue
        if entry.get("kty") != "RSA" or entry.get("use", "sig") != "sig":
            continue
        if entry.get("alg", "RS256") != "RS256":
            continue
        try:
            key = jwt.PyJWK(entry, algorithm="RS256").key
        except jwt.PyJWTError:
            continue
        if isinstance(key, RSAPublicKey):
            keys[kid] = key
    return keys
