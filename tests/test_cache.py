"""The tool-result cache, the list hints and the tool order: what they do and what they refuse.

The end-to-end private-scope tests are in `tests/test_obo.py`, where the identity is
a real exchanged user: five users, fifty concurrent calls, one downstream read each.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest
from mcp import Client
from prometheus_client import generate_latest

from flightops_mcp import server as srv
from flightops_mcp.auth import Scopes
from flightops_mcp.cache import (
    LIST_CACHE_HINTS,
    TOOL_CACHE_HINTS,
    CacheHint,
    ToolCache,
    UncachedToolError,
    cached,
    hint_for,
)
from flightops_mcp.config import Settings
from flightops_mcp.errors import ToolFailure
from flightops_mcp.runtime import Runtime
from flightops_mcp.telemetry import REGISTRY

from .authkit import JwksEndpoint, authenticated_as, signing_key
from .conftest import FakeClock
from .harness import remote_settings, serve, tool_payload
from .test_server_tools import ALL_TOOLS, EXPECTED_TOOLS, install

KEY = signing_key("key-cache")
PUBLIC = CacheHint(ttl_ms=10_000, scope="public")


class Calls:
    """A stand-in tool body that counts how often it really ran."""

    def __init__(self, result: Any = None) -> None:
        self.count = 0
        self._result = result

    async def __call__(self) -> Any:
        self.count += 1
        await asyncio.sleep(0)
        if isinstance(self._result, BaseException):
            raise self._result
        return self._result if self._result is not None else {"ok": True, "n": self.count}


def metric(name: str, **labels: str) -> float:
    value = REGISTRY.get_sample_value(name, labels)
    return value or 0.0


# --- the policy table ------------------------------------------------------------
def test_every_tool_has_a_cache_decision_as_it_has_a_scope() -> None:
    assert set(TOOL_CACHE_HINTS) == set(Scopes._BY_TOOL) == ALL_TOOLS  # noqa: SLF001
    with pytest.raises(UncachedToolError):
        hint_for("a_tool_nobody_decided_about")


def test_writes_process_state_and_composites_are_never_cached() -> None:
    for name in ("propose_rebooking", "upstream_status", "disruption_triage"):
        assert hint_for(name).ttl_ms == 0, name


def test_data_read_for_one_user_is_private_and_nothing_else_is() -> None:
    private = {name for name, hint in TOOL_CACHE_HINTS.items() if hint.scope == "private"}
    cacheable = {name for name, hint in TOOL_CACHE_HINTS.items() if hint.ttl_ms > 0}
    assert private & cacheable == {"flight_passenger_impact"}


def test_a_zero_ttl_tool_is_not_even_wrapped() -> None:
    async def upstream_status() -> dict[str, Any]:
        return {"ok": True}

    assert cached(upstream_status, cache=ToolCache) is upstream_status


# --- ttlMs ---------------------------------------------------------------------------
async def test_a_ttl_of_zero_is_not_cached() -> None:
    cache = ToolCache()
    body = Calls()

    first = await cache.fetch("t", "k", CacheHint(ttl_ms=0, scope="public"), body)
    second = await cache.fetch("t", "k", CacheHint(ttl_ms=0, scope="public"), body)

    assert body.count == 2
    assert len(cache) == 0
    assert "cache" not in first and "cache" not in second


async def test_a_result_is_fresh_until_its_ttl_and_not_one_instant_longer(
    clock: FakeClock,
) -> None:
    cache = ToolCache(clock=clock)
    body = Calls()

    await cache.fetch("t", "k", PUBLIC, body)
    clock.advance(9.999)
    hit = await cache.fetch("t", "k", PUBLIC, body)
    clock.advance(0.001)
    expired = await cache.fetch("t", "k", PUBLIC, body)

    assert body.count == 2
    assert hit == {"ok": True, "n": 1, "cache": {"hit": True, "ttl_ms": 10_000, "scope": "public"}}
    assert expired == {"ok": True, "n": 2}


async def test_a_disabled_cache_runs_every_call() -> None:
    cache = ToolCache(enabled=False)
    body = Calls()
    for _ in range(3):
        await cache.fetch("t", "k", PUBLIC, body)
    assert body.count == 3


async def test_the_cache_is_bounded_and_forgets_the_least_recently_used() -> None:
    cache = ToolCache(max_entries=2)
    a, b, c = Calls(), Calls(), Calls()
    await cache.fetch("t", "a", PUBLIC, a)
    await cache.fetch("t", "b", PUBLIC, b)
    await cache.fetch("t", "a", PUBLIC, a)  # a is now the most recent
    await cache.fetch("t", "c", PUBLIC, c)  # evicts b
    await cache.fetch("t", "a", PUBLIC, a)
    await cache.fetch("t", "b", PUBLIC, b)

    assert (a.count, b.count, c.count) == (1, 2, 1)
    assert len(cache) == 2
    with pytest.raises(ValueError, match="at least 1"):
        ToolCache(max_entries=0)


# --- what is never kept ------------------------------------------------------------
async def test_a_failure_payload_is_returned_and_forgotten() -> None:
    cache = ToolCache()
    failing = Calls({"ok": False, "error": {"code": "upstream_unavailable"}})

    await cache.fetch("t", "k", PUBLIC, failing)
    again = await cache.fetch("t", "k", PUBLIC, failing)

    assert failing.count == 2
    assert again["ok"] is False and "cache" not in again
    assert len(cache) == 0


async def test_a_raised_failure_is_raised_and_forgotten() -> None:
    cache = ToolCache()
    failing = Calls(ToolFailure(code="upstream_rate_limited", message="later"))

    for _ in range(2):
        with pytest.raises(ToolFailure):
            await cache.fetch("t", "k", PUBLIC, failing)

    assert failing.count == 2
    assert len(cache) == 0


async def test_a_hit_spends_nothing_and_says_so() -> None:
    cache = ToolCache()
    body = Calls({"ok": True, "bookkeeping": {"credits_spent": 4, "credits_remaining": 10}})

    await cache.fetch("t", "k", PUBLIC, body)
    hit = await cache.fetch("t", "k", PUBLIC, body)

    assert hit["bookkeeping"] == {"credits_spent": 0, "credits_remaining": 10}
    assert hit["cache"]["hit"] is True


async def test_a_caller_cannot_edit_what_the_next_caller_is_served() -> None:
    cache = ToolCache()
    body = Calls({"ok": True, "items": [1, 2]})

    first = await cache.fetch("t", "k", PUBLIC, body)
    first["items"].append("tampered")
    second = await cache.fetch("t", "k", PUBLIC, body)
    second["items"].append("tampered again")
    third = await cache.fetch("t", "k", PUBLIC, body)

    assert third["items"] == [1, 2]


# --- single flight -----------------------------------------------------------------
async def test_fifty_concurrent_identical_calls_run_the_tool_once() -> None:
    cache = ToolCache()
    body = Calls()

    results = await asyncio.gather(*(cache.fetch("t", "k", PUBLIC, body) for _ in range(50)))

    assert body.count == 1
    assert sum(1 for r in results if "cache" in r) == 49
    assert {r["n"] for r in results} == {1}


async def test_followers_of_a_failed_call_see_the_failure_and_nothing_is_kept() -> None:
    cache = ToolCache()
    failing = Calls(ToolFailure(code="upstream_unavailable", message="down"))

    results = await asyncio.gather(
        *(cache.fetch("t", "k", PUBLIC, failing) for _ in range(5)), return_exceptions=True
    )

    assert failing.count == 1
    assert all(isinstance(r, ToolFailure) for r in results)
    assert len(cache) == 0


# --- cacheScope: private -------------------------------------------------------------
async def test_private_results_are_partitioned_by_subject_client_and_scopes() -> None:
    store = ToolCache()
    seen: list[str] = []

    async def flight_passenger_impact(flight: str) -> dict[str, Any]:
        seen.append(flight)
        return {"ok": True, "flight": flight, "n": len(seen)}

    tool = cached(flight_passenger_impact, cache=lambda: store)
    contexts = [
        {"subject": "user-a", "client_id": "agent", "scopes": ("p",)},
        {"subject": "user-b", "client_id": "agent", "scopes": ("p",)},
        {"subject": "user-a", "client_id": "other-agent", "scopes": ("p",)},
        {"subject": "user-a", "client_id": "agent", "scopes": ("p", "q")},
    ]
    firsts = []
    for index, context in enumerate(contexts):
        with authenticated_as(f"token-{index}", **context):  # type: ignore[arg-type]
            firsts.append(await tool("SK1"))
            repeat = await tool(flight="SK1")
        assert repeat["n"] == firsts[-1]["n"], "the same context is served its own entry"
        assert repeat["cache"]["scope"] == "private"

    # Four contexts, four real calls: no context was served another's result.
    assert [first["n"] for first in firsts] == [1, 2, 3, 4]
    assert all("cache" not in first for first in firsts)


async def test_a_private_result_with_no_verified_caller_is_never_stored() -> None:
    store = ToolCache()
    runs: list[int] = []

    async def flight_passenger_impact(flight: str) -> dict[str, Any]:
        runs.append(1)
        return {"ok": True}

    tool = cached(flight_passenger_impact, cache=lambda: store)
    before = metric("flightops_cache_requests_total", tool=tool.__name__, outcome="bypass")
    await tool("SK1")
    await tool("SK1")

    assert len(runs) == 2
    assert len(store) == 0
    after = metric("flightops_cache_requests_total", tool=tool.__name__, outcome="bypass")
    assert after - before == 2


# --- through the server ---------------------------------------------------------------
async def test_arguments_are_one_key_however_they_are_spelled(settings: Settings) -> None:
    runtime = install(settings)
    async with Client(srv.mcp) as client:
        await client.call_tool("resolve_airport", {"query": "ESGG"})
        hit = await client.call_tool("resolve_airport", {"query": "ESGG", "limit": 5})

    assert hit.structured_content is not None
    assert hit.structured_content["cache"]["hit"] is True
    assert len(runtime.cache) == 1


async def test_a_composite_counts_only_credits_it_spent(settings: Settings) -> None:
    """Triage after a traffic call: the traffic part comes from the cache and the
    triage's own spend says so, instead of claiming the credit twice."""
    runtime = install(settings)
    async with Client(srv.mcp) as client:
        traffic = await client.call_tool("airport_live_traffic", {"airport": "ESGG"})
        triage = await client.call_tool("disruption_triage", {"airport": "ESGG"})
        again = await client.call_tool("disruption_triage", {"airport": "ESGG"})

    assert traffic.structured_content is not None and triage.structured_content is not None
    assert again.structured_content is not None
    assert traffic.structured_content["bookkeeping"]["credits_spent"] == 1
    # Movements (2 x 4) were fetched; traffic was not.
    assert triage.structured_content["bookkeeping"]["credits_spent"] == 8
    assert again.structured_content["bookkeeping"]["credits_spent"] == 0
    assert "cache" not in again.structured_content  # the composite itself is never cached
    assert runtime.opensky.credits_spent_total == 9


async def test_the_cache_is_observable_on_metrics(settings: Settings) -> None:
    configured = remote_settings(settings)
    install(configured)
    token = KEY.mint(scp=Scopes.WEATHER_READ)
    hits = metric("flightops_cache_requests_total", tool="airport_weather", outcome="hit")
    misses = metric("flightops_cache_requests_total", tool="airport_weather", outcome="miss")

    async with serve(configured, JwksEndpoint(KEY).transport) as remote:
        for _ in range(3):
            response = await remote.call_tool("airport_weather", {"airport": "ESGG"}, token=token)
            assert tool_payload(response)["ok"] is True
        scraped = (await remote.raw.get("/metrics")).text

    assert metric("flightops_cache_requests_total", tool="airport_weather", outcome="miss") == (
        misses + 1
    )
    assert metric("flightops_cache_requests_total", tool="airport_weather", outcome="hit") == (
        hits + 2
    )
    assert 'flightops_cache_requests_total{outcome="hit",tool="airport_weather"}' in scraped
    assert "flightops_cache_entries" in scraped


async def test_a_caller_without_the_scope_never_reaches_the_cache(settings: Settings) -> None:
    """A public result already cached is still refused to a token that lacks the scope:
    the scope check runs first, every time."""
    configured = remote_settings(settings)
    install(configured)
    allowed = KEY.mint(scp=Scopes.WEATHER_READ)
    refused = KEY.mint(scp=Scopes.REFERENCE_READ)

    async with serve(configured, JwksEndpoint(KEY).transport, http_challenge=False) as remote:
        warmed = await remote.call_tool("airport_weather", {"airport": "ESGG"}, token=allowed)
        assert tool_payload(warmed)["ok"] is True
        before = metric("flightops_cache_requests_total", tool="airport_weather", outcome="hit")
        response = await remote.call_tool("airport_weather", {"airport": "ESGG"}, token=refused)
        after = metric("flightops_cache_requests_total", tool="airport_weather", outcome="hit")

    payload = tool_payload(response)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "insufficient_scope"
    assert after == before


# --- list hints and tool order ---------------------------------------------------------
async def test_the_tool_list_carries_public_ttl_hints(settings: Settings) -> None:
    configured = remote_settings(settings)
    install(configured)
    token = KEY.mint(scp=Scopes.REFERENCE_READ)
    async with (
        serve(configured, JwksEndpoint(KEY).transport) as remote,
        remote.client(token) as client,
    ):
        # The 2026-07-28 client: a pre-2026 connection has no such fields to carry.
        result = await client.list_tools()

    hint = LIST_CACHE_HINTS["tools/list"]
    assert result.ttl_ms == hint.ttl_ms == 300_000
    assert result.cache_scope == hint.scope == "public"


async def test_tool_order_is_by_name_and_byte_identical_across_builds(
    settings: Settings,
) -> None:
    """Prompt caching needs an identical prefix. The tool list is part of it, so its
    order and bytes must not depend on how the server happened to be built."""
    install(settings)

    async def listing(server: Any) -> str:
        async with Client(server) as client:
            tools = await client.list_tools()
        return json.dumps(
            [tool.model_dump(mode="json", exclude_none=True) for tool in tools.tools],
            sort_keys=False,
        )

    first = await listing(srv.create_server())
    second = await listing(srv.create_server())
    # Registration order reversed: the published order must not move.
    original = list(srv._TOOLS)  # noqa: SLF001
    srv._TOOLS.reverse()  # noqa: SLF001
    try:
        reversed_build = await listing(srv.create_server())
    finally:
        srv._TOOLS[:] = original  # noqa: SLF001

    names = [tool["name"] for tool in json.loads(first)]
    assert names == sorted(names)
    assert set(names) == EXPECTED_TOOLS
    assert first == second == reversed_build


async def test_the_authenticated_server_publishes_the_same_order(settings: Settings) -> None:
    configured = remote_settings(settings)
    install(configured)
    token = KEY.mint(scp=Scopes.REFERENCE_READ)
    async with serve(configured, JwksEndpoint(KEY).transport) as remote:
        response = await remote.rpc("tools/list", {}, token=token)

    names = [tool["name"] for tool in response.json()["result"]["tools"]]
    assert names == sorted(EXPECTED_TOOLS)


def test_the_cache_counters_exist_before_any_call() -> None:
    scraped = generate_latest(REGISTRY).decode()
    assert "flightops_cache_requests_total" in scraped
    assert "flightops_cache_entries" in scraped


async def test_the_runtime_builds_a_cache_from_settings(settings: Settings) -> None:
    runtime = Runtime.build(
        settings.model_copy(update={"cache_enabled": False, "cache_max_entries": 3}),
        opensky_transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})),
    )
    try:
        body = Calls()
        await runtime.cache.fetch("t", "k", PUBLIC, body)
        await runtime.cache.fetch("t", "k", PUBLIC, body)
        assert body.count == 2
    finally:
        await runtime.aclose()
