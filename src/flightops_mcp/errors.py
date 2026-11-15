"""Structured errors.

Design rule: an expected failure is *data the model can act on*, never an
unhandled exception. Per the MCP specification's guidance that input-validation
and upstream failures belong to the tool execution error channel (so the model
can self-correct), every tool returns a payload instead of raising.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class ToolFailure(Exception):
    """A failure the calling agent is expected to reason about."""

    code: str
    message: str
    hint: str | None = None
    retry_after_s: float | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:  # pragma: no cover - debugging aid
        return f"{self.code}: {self.message}"

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "ok": False,
            "error": {"code": self.code, "message": self.message},
        }
        if self.hint:
            payload["error"]["hint"] = self.hint
        if self.retry_after_s is not None:
            payload["error"]["retry_after_s"] = round(self.retry_after_s, 3)
        if self.details:
            payload["error"]["details"] = self.details
        return payload


# --- Concrete failures -------------------------------------------------------
def authentication_required(tool: str) -> ToolFailure:
    return ToolFailure(
        code="authentication_required",
        message=f"{tool} was called on the authenticated server without a verified access token.",
        hint="Send a bearer token issued for this server. This is not retryable as is.",
        details={"tool": tool},
    )


def insufficient_scope(tool: str, required: Iterable[str], missing: Iterable[str]) -> ToolFailure:
    lacking = sorted(missing)
    return ToolFailure(
        code="insufficient_scope",
        message=f"The access token lacks the scope {tool} requires: {' '.join(lacking)}.",
        hint="Obtain a token that includes the missing scope, then retry. Retrying with the "
        "same token fails the same way; tell the user which permission is missing.",
        details={"tool": tool, "required_scopes": sorted(required), "missing_scopes": lacking},
    )


def missing_credentials() -> ToolFailure:
    return ToolFailure(
        code="credentials_missing",
        message="OpenSky credentials are not configured, so authenticated upstream calls "
        "cannot be made.",
        hint="Set FLIGHTOPS_OPENSKY_CLIENT_ID and FLIGHTOPS_OPENSKY_CLIENT_SECRET "
        "(free account: https://opensky-network.org). Tools backed by "
        "AviationWeather.gov and the local airport database keep working without them.",
    )


def token_passthrough_blocked(host: str) -> ToolFailure:
    return ToolFailure(
        code="token_passthrough_blocked",
        message=f"The server refused to send the caller's access token to {host}.",
        hint="This is a server defect, not something the caller can fix. Report it; do not "
        "retry. The caller's token was not sent anywhere.",
        details={"upstream_host": host},
    )


def upstream_auth_failed(status: int, upstream: str) -> ToolFailure:
    return ToolFailure(
        code="upstream_auth_failed",
        message=f"{upstream} rejected our credentials (HTTP {status}).",
        hint="The client id or secret is wrong or revoked. Rotate the API client in the "
        "upstream account page; do not retry with the same credentials.",
        details={"upstream": upstream, "status": status},
    )


def upstream_rate_limited(
    upstream: str, retry_after_s: float | None, remaining: str | None
) -> ToolFailure:
    return ToolFailure(
        code="upstream_rate_limited",
        message=f"{upstream} rate limit reached; this request was not served.",
        hint="Retry after the indicated delay, or narrow the query (smaller bounding box, "
        "shorter time window). Do not immediately retry the same call.",
        retry_after_s=retry_after_s,
        details={"upstream": upstream, "credits_remaining": remaining},
    )


def budget_exhausted(spent: int, budget: int) -> ToolFailure:
    return ToolFailure(
        code="local_budget_exhausted",
        message=(
            f"The local credit guard stopped this call ({spent}/{budget} credits used this hour)."
        ),
        hint="This guard exists so an agent loop cannot drain the upstream daily allowance. "
        "Narrow the query, or raise FLIGHTOPS_CREDIT_BUDGET_PER_HOUR deliberately.",
        details={"credits_spent_this_hour": spent, "budget": budget},
    )


def upstream_circuit_open(upstream: str, retry_after_s: float) -> ToolFailure:
    """The breaker is open: fail fast, and say how long the window has left."""
    return ToolFailure(
        code="upstream_circuit_open",
        message=(
            f"{upstream} has failed repeatedly, so calls to it are being refused "
            f"without an attempt."
        ),
        hint="Report the outage instead of retrying: the retry budget is already spent. "
        "Try again after the indicated delay, or use a tool that does not depend on "
        "this upstream.",
        retry_after_s=retry_after_s,
        details={"upstream": upstream},
    )


def upstream_unavailable(upstream: str, detail: str) -> ToolFailure:
    return ToolFailure(
        code="upstream_unavailable",
        message=f"{upstream} could not be reached or returned an unexpected response.",
        hint="Transient upstream failure. Retrying once is reasonable; a repeat means treat "
        "the data as unavailable rather than inferring a result.",
        details={"upstream": upstream, "detail": detail},
    )


def not_found(what: str, value: str) -> ToolFailure:
    return ToolFailure(
        code="not_found",
        message=f"No {what} matched {value!r}.",
        hint="Resolve the identifier first with resolve_airport, then retry with the ICAO "
        "code. Report the miss to the user rather than substituting a similar airport.",
    )


def reference_data_missing(path: str) -> ToolFailure:
    return ToolFailure(
        code="reference_data_missing",
        message=f"The airport reference database was not found at {path}.",
        hint="Run `uv run python scripts/build_airports_db.py` once (downloads the public-domain "
        "OurAirports CSV). Offline tools degrade to explicit lat/lon arguments until then.",
        details={"path": path},
    )


def airport_has_no_coordinates(ident: str) -> ToolFailure:
    return ToolFailure(
        code="airport_has_no_coordinates",
        message=f"Airport {ident} has no published coordinates in the reference data.",
        hint="Call the tool with an explicit bounding box or pick another airport; do not guess "
        "coordinates.",
    )
