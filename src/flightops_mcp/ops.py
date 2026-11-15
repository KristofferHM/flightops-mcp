"""Client for the internal operations API: the upstream that holds passenger data.

Two rules shape this module. The API is called with a token exchanged for it on
behalf of the user, never with the server's own identity, so its audit trail
names the person who asked. And passenger records stop here: `summarise` reduces
a manifest to counts, and nothing identifying is returned, logged or cached.
"""

from __future__ import annotations

import re
from typing import Any

import httpx

from .config import Settings
from .errors import ToolFailure, not_found, upstream_unavailable
from .http import SleepFn, UpstreamHttp

UPSTREAM = "internal operations API"

# IATA or ICAO airline designator, 1-4 digits, optional suffix. Validated before
# it is placed in a URL path.
_FLIGHT_RE = re.compile(r"^[A-Z0-9]{2,3}\d{1,4}[A-Z]?$")

# A connection shorter than this is at risk if the inbound flight is late.
TIGHT_CONNECTION_MINUTES = 45


class DownstreamTokenRejected(Exception):
    """The API refused the exchanged token (401). The caller may re-exchange once."""


def normalise_flight(flight: str) -> str:
    designator = flight.strip().upper().replace(" ", "")
    if not _FLIGHT_RE.fullmatch(designator):
        raise ToolFailure(
            code="invalid_argument",
            message=f"{flight!r} is not a flight designator.",
            hint="Use the airline code and number, for example SK1429.",
        )
    return designator


class OpsApiClient:
    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: SleepFn | None = None,
    ) -> None:
        self._configured = bool(settings.ops_api_base_url)
        kwargs: dict[str, Any] = {} if sleep is None else {"sleep": sleep}
        self._http = UpstreamHttp(
            settings,
            "ops-api",
            settings.ops_api_base_url or "http://ops-api.invalid",
            transport=transport,
            **kwargs,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    def configured(self) -> bool:
        return self._configured

    async def manifest(self, flight: str, token: str) -> list[dict[str, Any]]:
        """Passenger records for `flight`. They hold PII: do not log or return them."""
        if not self._configured:
            raise ToolFailure(
                code="ops_api_not_configured",
                message="No internal operations API is configured.",
                hint="An operator must set FLIGHTOPS_OPS_API_BASE_URL. Not retryable.",
            )
        try:
            response = await self._http.request_json(
                "GET",
                f"/flights/{flight}/passengers",
                headers={"Authorization": f"Bearer {token}"},
            )
        except httpx.HTTPError as exc:
            raise upstream_unavailable(UPSTREAM, type(exc).__name__) from exc

        if response.status_code == 401:
            raise DownstreamTokenRejected
        if response.status_code == 403:
            raise ToolFailure(
                code="downstream_forbidden",
                message=f"The {UPSTREAM} refused this user access to passenger data.",
                hint="The user lacks the permission downstream. Not retryable; tell the user.",
            )
        if response.status_code == 404:
            raise not_found("flight", flight)
        body = response.json_body
        rows = body.get("passengers") if isinstance(body, dict) else None
        if response.status_code != 200 or not isinstance(rows, list):
            raise upstream_unavailable(UPSTREAM, f"HTTP {response.status_code}")
        return [row for row in rows if isinstance(row, dict)]


def summarise(rows: list[dict[str, Any]]) -> dict[str, int]:
    """Reduce a manifest to counts. This is the only form passenger data leaves in."""
    connecting = [row for row in rows if row.get("connecting_flight")]
    return {
        "booked": len(rows),
        "checked_in": sum(1 for row in rows if row.get("checked_in") is True),
        "not_checked_in": sum(1 for row in rows if row.get("checked_in") is not True),
        "connecting": len(connecting),
        "tight_connections": sum(
            1
            for row in connecting
            if isinstance(row.get("connection_minutes"), int)
            and row["connection_minutes"] < TIGHT_CONNECTION_MINUTES
        ),
        "needing_assistance": sum(1 for row in rows if row.get("needs_assistance") is True),
    }
