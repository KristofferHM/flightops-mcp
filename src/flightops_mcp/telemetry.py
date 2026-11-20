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
# Every upstream that meters its use, by name. The OpenSky-only counter above is
# kept because the dashboard and the runbook name it; this one is what a second
# metered upstream needed, and it is charged by the same ledger class.
UPSTREAM_CREDITS_SPENT = Counter(
    "flightops_upstream_credits_spent_total",
    "Metered upstream units (credits, requests) charged by the local ledger, by upstream.",
    ["upstream"],
    registry=REGISTRY,
)
CACHE_REQUESTS = Counter(
    "flightops_cache_requests_total",
    "Tool-result cache lookups by tool and outcome (hit, miss, coalesced, bypass).",
    ["tool", "outcome"],
    registry=REGISTRY,
)
CACHE_ENTRIES = Gauge(
    "flightops_cache_entries",
    "Tool results currently held in the cache.",
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
        "subject_token",
        "approval_token",
        "request_state",
        "assertion",
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
# Loggers that write a request's full URL, query string included, at INFO.
_URL_LOGGERS = ("httpx", "httpcore")


class _SecretValues:
    """Credential values known by value, not by key: a BYO provider's API keys.

    A provider may put its key in a query string, which no key-based rule can see,
    and `httpx` logs every request URL. Each value is registered before its first
    request is made, and scrubbed from structlog events and from those loggers.
    Empty until a value is registered, so a process without one logs as before.
    """

    values: frozenset[str] = frozenset()


class _ScrubSecretValues(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if _SecretValues.values:
            message = record.getMessage()
            scrubbed = _scrub(message)
            if scrubbed != message:
                record.msg, record.args = scrubbed, ()
        return True


_SCRUB_FILTER = _ScrubSecretValues()


def register_secret_values(values: Any) -> None:
    """Never log these strings, wherever they appear. Values under 4 characters are ignored.

    A short value would scrub common words out of unrelated log lines.
    """
    fresh = {str(value) for value in values if value and len(str(value)) >= 4}
    if not fresh - _SecretValues.values:
        return
    _SecretValues.values = _SecretValues.values | fresh
    for name in _URL_LOGGERS:
        logger = logging.getLogger(name)
        if _SCRUB_FILTER not in logger.filters:
            logger.addFilter(_SCRUB_FILTER)


def _scrub(text: str) -> str:
    # Longest first, so a value that contains another is removed whole.
    for secret in sorted(_SecretValues.values, key=len, reverse=True):
        text = text.replace(secret, _REDACTED)
    return text


def _redact_value(value: Any) -> Any:
    if isinstance(value, str):
        value = _BEARER_RE.sub(r"\1" + _REDACTED, value)
        value = _JWT_RE.sub(_REDACTED, value)
        return _scrub(value) if _SecretValues.values else value
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


def redact(value: Any) -> Any:
    """The log redaction, for anything else that is written down (agent transcripts)."""
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
