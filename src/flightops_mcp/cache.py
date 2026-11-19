"""Tool-result caching, governed by the protocol's own `ttlMs` / `cacheScope` vocabulary.

Revision 2026-07-28 lets a server say how long a result stays fresh (`ttlMs`) and
whether it may be shared across authorization contexts (`cacheScope`). The SDK puts
those hints on list and read results. This module applies the same two words to
tool results inside the server, because that is where the money is: a repeated
traffic question inside ten seconds should not spend a second OpenSky credit.

The rules, each one a test in `tests/test_cache.py`:

* **`ttl_ms == 0` is never stored.** Not stored briefly, not stored and then
  expired: the call goes straight through. Writes and process state are 0.
* **`private` is partitioned by identity.** The key carries the caller's subject,
  client and granted scopes, so one user's passenger counts cannot be served to
  another. A private result with no verified caller is not cached at all.
* **Only successes are kept.** An `ok: false` payload or a raised failure is
  returned to the caller and forgotten, so a transient outage is not pinned for
  the TTL.
* **The cache sits inside the scope check.** `tool()` wraps it inside `guarded`,
  and the HTTP server wraps `scoped` around that, so a caller who lacks the scope
  is refused before the cache is consulted.
* **A hit says it is a hit.** The payload keeps its original `upstream_time_epoch`, gains a
  `cache` block, and its `credits_spent` is zero, because nothing was spent.
* **Concurrent identical calls make one upstream call.** The first caller runs
  the tool; the rest wait for its answer. Counted as `coalesced`.

Every tool must have an entry in `TOOL_CACHE_HINTS`, as every tool must have a
scope: caching is a decision made per tool, never a default that applies by
omission.
"""

from __future__ import annotations

import asyncio
import copy
import functools
import inspect
import json
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Final

from mcp.server.caching import CacheableMethod, CacheHint

from .auth import current_access_token
from .telemetry import CACHE_ENTRIES, CACHE_REQUESTS

__all__ = [
    "LIST_CACHE_HINTS",
    "TOOL_CACHE_HINTS",
    "CacheHint",
    "ToolCache",
    "cache_identity",
    "cached",
    "hint_for",
]


class UncachedToolError(KeyError):
    """A tool with no entry in the cache hint table."""


# The tool list is the same for every caller: scopes are enforced when a tool is
# called, not by hiding it. So it is public. Five minutes bounds how long a client
# keeps a list from before a deploy.
LIST_CACHE_HINTS: Final[Mapping[CacheableMethod, CacheHint]] = {
    "tools/list": CacheHint(ttl_ms=300_000, scope="public"),
    "server/discover": CacheHint(ttl_ms=300_000, scope="public"),
}

TOOL_CACHE_HINTS: Final[Mapping[str, CacheHint]] = {
    # A bundled snapshot that changes only with a new image.
    "resolve_airport": CacheHint(ttl_ms=3_600_000, scope="public"),
    # METARs are issued at most every 30 minutes. A minute old is still current.
    "airport_weather": CacheHint(ttl_ms=60_000, scope="public"),
    # OpenSky's authenticated resolution is 5 s. Ten seconds is two buckets, and is
    # what keeps fifty agents asking about one airport at one credit, not fifty.
    "airport_live_traffic": CacheHint(ttl_ms=10_000, scope="public"),
    # A one-hour window whose end is rounded to 5 s: a minute changes almost nothing.
    "airport_movements": CacheHint(ttl_ms=60_000, scope="public"),
    # Schiphol's schedule: published times, updated by the airport as they change.
    "schiphol_flights": CacheHint(ttl_ms=60_000, scope="public"),
    # A wide box costs the top credit tier, so a board and an agent asking within the
    # minute share one call. Aircraft move, but this is an overview, not a position fix.
    "airspace_snapshot": CacheHint(ttl_ms=60_000, scope="public"),
    # Composed from three cached parts. Caching the composite as well would let its
    # severity outlive the evidence it was computed from.
    "disruption_triage": CacheHint(ttl_ms=0),
    # Process state. A cached health report is a wrong health report.
    "upstream_status": CacheHint(ttl_ms=0),
    # Read on behalf of one user. Private: never served to anyone else.
    "flight_passenger_impact": CacheHint(ttl_ms=30_000, scope="private"),
    # The only write. Idempotency is the operations API's job, not a cache's.
    "propose_rebooking": CacheHint(ttl_ms=0),
}


def hint_for(tool: str) -> CacheHint:
    """The cache hint for `tool`. Unknown tools are an error, as for scopes."""
    try:
        return TOOL_CACHE_HINTS[tool]
    except KeyError:
        raise UncachedToolError(tool) from None


def cache_identity() -> str | None:
    """The authorization context a private result belongs to, or None.

    Subject, client and the granted scopes, all three. The same user through a
    different client, or with a narrower token, is a different context: the
    narrower token must not see what the wider one was given.
    """
    token = current_access_token()
    if token is None:
        return None
    return json.dumps([token.subject, token.client_id, sorted(token.scopes)], separators=(",", ":"))


@dataclass(slots=True)
class _Entry:
    value: dict[str, Any]
    expires_at: float


class ToolCache:
    """A bounded LRU of tool results, with single flight per key."""

    def __init__(
        self,
        *,
        max_entries: int = 1024,
        enabled: bool = True,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if max_entries < 1:
            raise ValueError("max_entries must be at least 1")
        self._max = max_entries
        self._enabled = enabled
        self._clock = clock
        self._entries: OrderedDict[str, _Entry] = OrderedDict()
        self._inflight: dict[str, asyncio.Future[Any]] = {}
        self.hits = 0
        self.misses = 0

    def __len__(self) -> int:
        return len(self._entries)

    def clear(self) -> None:
        self._entries.clear()
        CACHE_ENTRIES.set(0)

    async def fetch(
        self,
        tool: str,
        key: str,
        hint: CacheHint,
        call: Callable[[], Awaitable[Any]],
    ) -> Any:
        """Serve `key` from the cache, or run `call` once and keep a success."""
        if not self._enabled or hint.ttl_ms <= 0:
            return await call()

        entry = self._entries.get(key)
        if entry is not None:
            if self._clock() < entry.expires_at:
                self._entries.move_to_end(key)
                CACHE_REQUESTS.labels(tool=tool, outcome="hit").inc()
                self.hits += 1
                return _as_hit(entry.value, hint)
            del self._entries[key]
            CACHE_ENTRIES.set(len(self._entries))

        pending = self._inflight.get(key)
        if pending is not None:
            CACHE_REQUESTS.labels(tool=tool, outcome="coalesced").inc()
            self.hits += 1  # answered without an upstream call of its own
            # `shield`: a follower that is cancelled must not cancel the leader's call.
            result = await asyncio.shield(pending)
            return _as_hit(result, hint) if _keepable(result) else result

        CACHE_REQUESTS.labels(tool=tool, outcome="miss").inc()
        self.misses += 1
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._inflight[key] = future
        try:
            result = await call()
        except asyncio.CancelledError:
            future.cancel()
            raise
        except BaseException as exc:
            future.set_exception(exc)
            # Marks the exception retrieved, so a call with no followers does not
            # log "exception was never retrieved" when the future is collected.
            future.exception()
            raise
        else:
            future.set_result(result)
            if _keepable(result):
                self._store(key, result, hint)
            return result
        finally:
            del self._inflight[key]

    def _store(self, key: str, value: dict[str, Any], hint: CacheHint) -> None:
        self._entries[key] = _Entry(
            value=copy.deepcopy(value), expires_at=self._clock() + hint.ttl_ms / 1000
        )
        self._entries.move_to_end(key)
        while len(self._entries) > self._max:
            self._entries.popitem(last=False)
        CACHE_ENTRIES.set(len(self._entries))


def _keepable(result: Any) -> bool:
    """Only a plain success payload. Failures and approval requests are never kept."""
    return isinstance(result, dict) and result.get("ok") is True


def _as_hit(value: dict[str, Any], hint: CacheHint) -> dict[str, Any]:
    """A copy the caller may mutate, labelled as served from the cache."""
    served = copy.deepcopy(value)
    served["cache"] = {"hit": True, "ttl_ms": hint.ttl_ms, "scope": hint.scope}
    bookkeeping = served.get("bookkeeping")
    if isinstance(bookkeeping, dict) and "credits_spent" in bookkeeping:
        # This answer cost nothing. Reporting the original spend again would make a
        # composite that sums its parts claim credits that were never charged.
        bookkeeping["credits_spent"] = 0
    return served


def cached(
    fn: Callable[..., Awaitable[Any]], *, cache: Callable[[], ToolCache]
) -> Callable[..., Awaitable[Any]]:
    """Serve `fn`'s results through `cache()`, under its hint.

    `cache` is looked up per call, not bound once: the runtime that owns it is
    replaced per test and per eval scenario, and a cache must not outlive its world.

    The key is the tool name and its bound arguments with defaults applied, so
    `airport_weather("ESGG")` and `airport_weather(airport="ESGG")` are one entry.
    """
    name: str = fn.__name__
    hint = hint_for(name)
    if hint.ttl_ms <= 0:
        return fn  # nothing to decide per call
    signature = inspect.signature(fn)

    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        bound = signature.bind(*args, **kwargs)
        bound.apply_defaults()
        parts: list[Any] = [name, sorted(bound.arguments.items())]
        if hint.scope == "private":
            identity = cache_identity()
            if identity is None:
                # No verified caller means no partition to keep it in. Fail closed.
                CACHE_REQUESTS.labels(tool=name, outcome="bypass").inc()
                return await fn(*args, **kwargs)
            parts.append(identity)
        key = json.dumps(parts, separators=(",", ":"), default=str)
        return await cache().fetch(name, key, hint, lambda: fn(*args, **kwargs))

    return wrapper
