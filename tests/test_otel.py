"""Tracing across the tool boundary.

The revision this server targets carries trace context **per request** in `_meta`
(stateless: there is no session to attach a trace to). The assertions that matter:
the client's `traceparent` becomes the parent span, one triage call stays in one
trace with its nested lookups visible, and the upstream request carries the context
onward.
"""

from __future__ import annotations

import time
from typing import Any

import httpx
import pytest
from mcp import Client

from flightops_mcp import otel
from flightops_mcp import server as srv
from flightops_mcp.airports import build_from_csv
from flightops_mcp.config import Settings
from flightops_mcp.runtime import Runtime, set_runtime

from .conftest import FIXTURES, SleepRecorder

TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"
CLIENT_SPAN_ID = "00f067aa0ba902b7"
TRACEPARENT = f"00-{TRACE_ID}-{CLIENT_SPAN_ID}-01"
TRACE_ID_INT = int(TRACE_ID, 16)

VFR_METAR = [{"icaoId": "ESGG", "rawOb": "ESGG 142320Z 24008KT 9999 BKN030", "fltCat": "VFR"}]


def install(settings: Settings, *, seen_headers: list[dict[str, str]] | None = None) -> Runtime:
    """A runtime whose upstreams are mocks; optionally records request headers."""
    if not settings.airports_db.is_file():
        build_from_csv(FIXTURES / "airports_sample.csv", settings.airports_db)

    def opensky_handler(request: httpx.Request) -> httpx.Response:
        if seen_headers is not None:
            seen_headers.append(dict(request.headers))
        if "/states/" in request.url.path:
            return httpx.Response(200, json={"time": 1_700_000_000, "states": []})
        return httpx.Response(200, json=[])

    def token_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"access_token": "tok", "expires_in": 1800})

    def weather_handler(request: httpx.Request) -> httpx.Response:
        if seen_headers is not None:
            seen_headers.append(dict(request.headers))
        if "/taf" in request.url.path:
            return httpx.Response(200, json=[{"icaoId": "ESGG", "rawTAF": "TAF ESGG"}])
        return httpx.Response(200, json=VFR_METAR)

    runtime = Runtime.build(
        settings,
        opensky_transport=httpx.MockTransport(opensky_handler),
        token_transport=httpx.MockTransport(token_handler),
        weather_transport=httpx.MockTransport(weather_handler),
        sleep=SleepRecorder(),
        clock=time.time,
    )
    set_runtime(runtime)
    return runtime


@pytest.fixture(autouse=True)
def _clear_runtime() -> Any:
    yield
    set_runtime(None)


def spans_in_client_trace() -> list[Any]:
    return [span for span in otel.exported_spans() if span.context.trace_id == TRACE_ID_INT]


# --- wiring ------------------------------------------------------------------
def test_the_sdk_still_instruments_requests_by_default() -> None:
    """`MCPServer` installs its OTel middleware as an outermost built-in.

    If a future SDK stops doing that, request spans vanish silently — so the
    dependency on that default is asserted rather than assumed.
    """
    assert otel.sdk_instruments_requests(srv.create_server()) is True


def test_configuring_tracing_twice_keeps_the_first_provider() -> None:
    # OTel refuses to replace a provider; the function must return the installed
    # exporter rather than raise or silently do nothing.
    for _ in range(3):
        assert otel.configure_tracing(exporter="memory") is not None
    assert otel.tracing_kind() == "memory"


# --- the trace ---------------------------------------------------------------
async def test_one_triage_call_is_one_trace_with_nested_spans(settings: Settings) -> None:
    install(settings)
    async with Client(srv.mcp) as client:
        await client.call_tool(
            "disruption_triage", {"airport": "ESGG"}, meta={"traceparent": TRACEPARENT}
        )

    in_trace = spans_in_client_trace()
    names = [span.name for span in in_trace]
    assert len(in_trace) >= 4, [span.name for span in otel.exported_spans()]
    assert "tools/call disruption_triage" in names
    assert "tool.disruption_triage" in names
    # The composite tool's own lookups are their own spans, not one opaque block.
    assert "tool.airport_weather" in names
    assert any(name.startswith("HTTP ") for name in names), names

    by_id = {span.context.span_id: span for span in in_trace}
    request_span = next(span for span in in_trace if span.name == "tools/call disruption_triage")
    assert request_span.parent is not None
    assert request_span.parent.span_id == int(CLIENT_SPAN_ID, 16), (
        "the server span must be parented to the client's span, not start a new trace"
    )

    triage = next(span for span in in_trace if span.name == "tool.disruption_triage")
    assert triage.parent is not None
    assert triage.parent.span_id == request_span.context.span_id

    weather = next(span for span in in_trace if span.name == "tool.airport_weather")
    assert weather.parent is not None
    assert weather.parent.span_id == triage.context.span_id, "nested tool call must nest"

    http_span = next(span for span in in_trace if span.name.startswith("HTTP "))
    assert http_span.parent is not None
    assert http_span.parent.span_id in {weather.context.span_id, triage.context.span_id}
    assert http_span.attributes is not None
    assert http_span.attributes["upstream.name"] in {"opensky", "aviationweather"}
    assert "http.response.status_code" in http_span.attributes
    assert by_id[http_span.parent.span_id].name.startswith("tool.")


async def test_exactly_one_server_span_per_request(settings: Settings) -> None:
    """Regression: a second request-span middleware produced two spans per call."""
    install(settings)
    async with Client(srv.mcp) as client:
        await client.call_tool(
            "resolve_airport", {"query": "ESGG"}, meta={"traceparent": TRACEPARENT}
        )

    calls = [span for span in spans_in_client_trace() if span.name.startswith("tools/call")]
    assert len(calls) == 1, [span.name for span in otel.exported_spans()]


async def test_the_request_span_carries_the_semantic_attributes(settings: Settings) -> None:
    install(settings)
    async with Client(srv.mcp) as client:
        await client.call_tool(
            "airport_weather", {"airport": "ESGG"}, meta={"traceparent": TRACEPARENT}
        )

    request_span = next(
        span for span in spans_in_client_trace() if span.name.startswith("tools/call")
    )
    attributes = request_span.attributes or {}
    assert attributes["mcp.method.name"] == "tools/call"
    assert attributes["mcp.protocol.version"] == "2026-07-28"
    assert attributes["gen_ai.tool.name"] == "airport_weather"
    assert attributes["gen_ai.operation.name"] == "execute_tool"
    assert "jsonrpc.request.id" in attributes


async def test_a_request_without_traceparent_starts_its_own_trace(settings: Settings) -> None:
    install(settings)
    async with Client(srv.mcp) as client:
        await client.call_tool("resolve_airport", {"query": "ESGG"})

    calls = [span for span in otel.exported_spans() if span.name.startswith("tools/call")]
    assert len(calls) == 1
    assert calls[0].context.trace_id != TRACE_ID_INT
    assert calls[0].parent is None or not calls[0].parent.is_valid


async def test_handshake_traffic_without_a_traceparent_gets_its_own_trace(
    settings: Settings,
) -> None:
    """`server/discover` and `tools/list` carry no trace context, and must not be
    appended to the client's trace — a fabricated link is worse than a separate one."""
    install(settings)
    async with Client(srv.mcp) as client:
        await client.call_tool(
            "resolve_airport", {"query": "ESGG"}, meta={"traceparent": TRACEPARENT}
        )

    handshake = [
        span for span in otel.exported_spans() if span.name in {"server/discover", "tools/list"}
    ]
    assert handshake, [span.name for span in otel.exported_spans()]
    assert all(span.context.trace_id != TRACE_ID_INT for span in handshake)


# --- propagation and failure -------------------------------------------------
async def test_upstream_requests_carry_the_trace_context_forward(settings: Settings) -> None:
    seen: list[dict[str, str]] = []
    install(settings, seen_headers=seen)
    async with Client(srv.mcp) as client:
        await client.call_tool(
            "airport_weather", {"airport": "ESGG"}, meta={"traceparent": TRACEPARENT}
        )

    forwarded = [headers for headers in seen if "traceparent" in headers]
    assert forwarded, "the upstream call must carry a traceparent header"
    version, trace_id, span_id, _flags = forwarded[0]["traceparent"].split("-")
    assert version == "00"
    assert trace_id == TRACE_ID, "the upstream must see the same trace"
    assert span_id != CLIENT_SPAN_ID, "the server propagates its own span, not the client's"


async def test_tool_failure_is_marked_on_the_tool_span(settings: Settings) -> None:
    install(settings)
    async with Client(srv.mcp) as client:
        await client.call_tool(
            "resolve_airport", {"query": "ZZZZ"}, meta={"traceparent": TRACEPARENT}
        )

    tool_span = next(
        span for span in spans_in_client_trace() if span.name == "tool.resolve_airport"
    )
    assert tool_span.attributes is not None
    assert tool_span.attributes["mcp.tool.outcome"] == "not_found"
    assert tool_span.attributes["mcp.tool.ok"] is False
    assert tool_span.status.status_code.name == "ERROR"


async def test_a_successful_tool_span_is_not_marked_as_an_error(settings: Settings) -> None:
    install(settings)
    async with Client(srv.mcp) as client:
        await client.call_tool(
            "resolve_airport", {"query": "ESGG"}, meta={"traceparent": TRACEPARENT}
        )

    tool_span = next(
        span for span in spans_in_client_trace() if span.name == "tool.resolve_airport"
    )
    assert tool_span.attributes is not None
    assert tool_span.attributes["mcp.tool.outcome"] == "ok"
    assert tool_span.status.status_code.name != "ERROR"
