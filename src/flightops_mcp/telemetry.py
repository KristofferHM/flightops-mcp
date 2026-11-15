"""Telemetry: structured logs with redaction, and Prometheus metrics.

The redaction pass is deliberately paranoid. An MCP server sits between a model
and an upstream API, so the two things that must never reach a log line are the
bearer token and any passenger-identifying field. Tests plant a canary value and
assert it appears nowhere in captured logs.
"""

from __future__ import annotations

import logging
import re
import sys
from collections.abc import MutableMapping
from typing import Any

import structlog
from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram

# --- Metrics -----------------------------------------------------------------
REGISTRY = CollectorRegistry()

TOOL_CALLS = Counter(
    "flightops_tool_calls_total",
    "Tool invocations by tool name and outcome.",
    ["tool", "outcome"],
    registry=REGISTRY,
)
UPSTREAM_REQUESTS = Counter(
    "flightops_upstream_requests_total",
    "Upstream HTTP requests by upstream and status class.",
    ["upstream", "status"],
    registry=REGISTRY,
)
UPSTREAM_LATENCY = Histogram(
    "flightops_upstream_latency_seconds",
    "Upstream request latency.",
    ["upstream"],
    registry=REGISTRY,
)
CREDITS_SPENT = Counter(
    "flightops_opensky_credits_spent_total",
    "OpenSky API credits spent by this process.",
    registry=REGISTRY,
)
CREDITS_REMAINING = Gauge(
    "flightops_opensky_credits_remaining",
    "Last reported upstream credit balance (X-Rate-Limit-Remaining).",
    registry=REGISTRY,
)
TOKEN_REFRESHES = Counter(
    "flightops_token_refreshes_total",
    "OAuth2 access-token acquisitions by outcome.",
    ["outcome"],
    registry=REGISTRY,
)
CIRCUIT_STATE = Gauge(
    "flightops_circuit_open",
    "1 when the upstream circuit breaker is open.",
    ["upstream"],
    registry=REGISTRY,
)


# --- Log redaction -----------------------------------------------------------
_SECRET_KEYS = frozenset(
    {
        "access_token",
        "refresh_token",
        "id_token",
        "token",
        "authorization",
        "auth",
        "client_secret",
        "api_key",
        "api_secret",
        "password",
        "secret",
        "cookie",
        "set-cookie",
    }
)
# Passenger-identifying fields. The reference data has none of these; the mock
# internal ops API does, which is what makes the canary test meaningful.
_PII_KEYS = frozenset(
    {
        "passenger_name",
        "passenger_email",
        "passenger_phone",
        "email",
        "phone",
        "pnr",
        "booking_ref",
        "date_of_birth",
        "dob",
        "ssn",
        "passport",
    }
)
_BEARER_RE = re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._\-~+/]{8,}=*")
_JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\b")
_REDACTED = "[REDACTED]"


def _redact_value(value: Any) -> Any:
    if isinstance(value, str):
        value = _BEARER_RE.sub(r"\1" + _REDACTED, value)
        return _JWT_RE.sub(_REDACTED, value)
    if isinstance(value, dict):
        return {k: _redact_keyed(k, v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return type(value)(_redact_value(v) for v in value)
    return value


def _redact_keyed(key: Any, value: Any) -> Any:
    lowered = str(key).lower()
    if lowered in _SECRET_KEYS or lowered in _PII_KEYS:
        return _REDACTED
    return _redact_value(value)


def redaction_processor(
    _logger: Any, _method_name: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    """structlog processor: strip credentials and PII from every log record."""
    return {k: _redact_keyed(k, v) for k, v in event_dict.items()}


def configure_logging(level: str = "INFO") -> None:
    """JSON logs through the stdlib logging tree, with redaction installed.

    Deliberately not structlog's `PrintLogger`: that binds `sys.stdout` at
    construction time, and anything that replaces or closes `sys.stdout` later
    (test harnesses, a supervisor restarting streams) turns every subsequent log
    call into `ValueError: I/O operation on closed file`. Routing through stdlib
    logging means handlers own the stream, and a test can attach its own handler
    without touching global state.
    """
    numeric_level = getattr(logging, level.upper(), logging.INFO)
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=numeric_level,
        force=True,
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            redaction_processor,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(numeric_level),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str) -> Any:
    return structlog.get_logger(name)
