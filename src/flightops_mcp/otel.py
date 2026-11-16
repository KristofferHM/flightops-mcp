"""OpenTelemetry: one trace across the client, the tool call and the upstream request.

The 2026-07-28 revision carries W3C trace context **per request** in `_meta`
(`traceparent`, `tracestate`, `baggage`), because the protocol is stateless — there
is no session to attach a trace to. A server that ignores it starts a new trace for
every tool call, and an operator debugging a slow triage cannot tell which client
request, which tool and which upstream hop belong together.

Three seams, in this order:

1. **The SDK's `OpenTelemetryMiddleware`**, which `MCPServer` installs by default
   as its outermost built-in — one SERVER span per inbound MCP request, parented
   to the client's `traceparent` from `_meta`. This project registers no
   request-span middleware of its own: a second one produced two spans per
   request, which is how that default was discovered.
2. `guarded` in `server.py` — a span per tool body, so composite tools show their
   nested calls instead of one opaque block.
3. `UpstreamHttp` in `http.py` — a CLIENT span per upstream attempt, with the
   context re-injected as headers so the far side can join the trace.

Exporter choice is configuration, not code: `none` (default, no overhead),
`console` (development), `otlp` (a collector at `FLIGHTOPS_OTEL_ENDPOINT`), and
`memory` (tests, which assert on the spans directly).
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from typing import Any

from opentelemetry import trace
from opentelemetry.propagate import inject
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    ConsoleSpanExporter,
    SimpleSpanProcessor,
    SpanExporter,
    SpanExportResult,
)
from opentelemetry.sdk.trace.sampling import (
    ParentBased,
    TraceIdRatioBased,
)

from . import __version__
from .config import Settings

TRACER_NAME = "flightops-mcp"

_state: dict[str, Any] = {"provider": None, "exporter": None, "kind": None}


# --- provider ----------------------------------------------------------------
class ListSpanExporter(SpanExporter):
    """Collects finished spans in memory, for tests and for a demo endpoint."""

    def __init__(self) -> None:
        self.spans: list[ReadableSpan] = []

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        self.spans.extend(spans)
        return SpanExportResult.SUCCESS

    def shutdown(self) -> None:
        return None

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        del timeout_millis  # part of the exporter interface, unused here
        return True

    def reset(self) -> None:
        self.spans.clear()


def configure_tracing(
    settings: Settings | None = None,
    *,
    exporter: str | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> SpanExporter | None:
    """Install the tracing pipeline once per process.

    OTel refuses to replace a tracer provider, so a second call returns the
    exporter installed by the first. Tests therefore configure `memory` once and
    reset the collected spans between cases rather than reinstalling a provider.
    """
    del clock  # kept in the signature for symmetry with the other factories
    if _state["provider"] is not None:
        return _state["exporter"]  # type: ignore[no-any-return]

    resolved = settings or Settings()
    kind = (exporter or resolved.otel_exporter or "none").lower()

    if kind == "none":
        # No provider: the API's default no-op is what we want, and installing one
        # that drops spans would only cost a sampler on every request.
        _state.update({"provider": "noop", "kind": kind})
        return None

    target: SpanExporter
    if kind == "memory":
        target = ListSpanExporter()
    elif kind == "console":
        target = ConsoleSpanExporter()
    elif kind == "otlp":
        # Imported lazily: the OTLP exporter pulls in protobuf, and a deployment
        # running with `none` or `console` should not pay for that at import time.
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (  # noqa: PLC0415
            OTLPSpanExporter,
        )

        target = OTLPSpanExporter(endpoint=resolved.otel_endpoint)
    else:
        raise ValueError(
            f"unknown FLIGHTOPS_OTEL_EXPORTER {kind!r}: use none, console, otlp or memory"
        )

    provider = TracerProvider(
        resource=Resource.create(
            {
                "service.name": resolved.otel_service_name,
                "service.version": _version(),
                "deployment.environment": resolved.otel_environment,
            }
        ),
        sampler=ParentBased(TraceIdRatioBased(resolved.otel_sample_ratio)),
    )
    if kind == "memory":
        provider.add_span_processor(SimpleSpanProcessor(target))
    else:
        provider.add_span_processor(BatchSpanProcessor(target))
    trace.set_tracer_provider(provider)

    _state.update({"provider": provider, "exporter": target, "kind": kind})
    return target


def _version() -> str:
    return __version__


def tracer() -> trace.Tracer:
    return trace.get_tracer(TRACER_NAME)


def exported_spans() -> list[ReadableSpan]:
    exporter = _state.get("exporter")
    if isinstance(exporter, ListSpanExporter):
        return exporter.spans
    return []


def reset_spans() -> None:
    exporter = _state.get("exporter")
    if isinstance(exporter, ListSpanExporter):
        exporter.reset()


def tracing_kind() -> str | None:
    kind = _state.get("kind")
    return kind if isinstance(kind, str) else None


def inject_trace_headers(headers: dict[str, str]) -> dict[str, str]:
    """Add W3C trace context to outbound headers so the upstream can join the trace."""
    inject(headers)
    return headers


# --- request middleware ------------------------------------------------------
def sdk_instruments_requests(server: Any) -> bool:
    """True when the server's middleware chain carries the SDK's OTel middleware.

    `MCPServer` registers `OpenTelemetryMiddleware` **by default** as its outermost
    built-in, which is why this project registers no request-span middleware of its
    own: doing so produced two SERVER spans per inbound message, discovered by
    running one triage call and counting. The assertions that depend on request
    spans call this first so an SDK change to that default fails loudly instead of
    silently returning shapeless traces.
    """
    from mcp.server._otel import OpenTelemetryMiddleware  # noqa: PLC0415 - private path, deliberately narrow

    return any(isinstance(entry, OpenTelemetryMiddleware) for entry in server.middleware)
