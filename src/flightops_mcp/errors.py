"""Structured errors.

Design rule: an expected failure is *data the model can act on*, never an
unhandled exception. Per the MCP specification's guidance that input-validation
and upstream failures belong to the tool execution error channel (so the model
can self-correct), every tool returns a payload instead of raising.
"""

from __future__ import annotations

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
def missing_credentials() -> ToolFailure:
    return ToolFailure(
        code="credentials_missing",
        message="OpenSky credentials are not configured, so authenticated upstream calls "
        "cannot be made.",
        hint="Set FLIGHTOPS_OPENSKY_CLIENT_ID and FLIGHTOPS_OPENSKY_CLIENT_SECRET "
        "(free account: https://opensky-network.org). Tools backed by "
        "AviationWeather.gov and the local airport database keep working without them.",
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
