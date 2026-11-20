"""Client for the internal operations API: the upstream that holds passenger data.

Two rules shape this module. The API is called with a token exchanged for it on
behalf of the user, never with the server's own identity, so its audit trail
names the person who asked. And passenger records stop here: `summarise` reduces
a manifest to counts, and nothing identifying is returned, logged or cached.

The same holds for the one write. The API answers a rebooking with the passengers
it moved; `rebook` keeps how many and drops who.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any, Final

import httpx

from .config import Settings
from .errors import ToolFailure, not_found, upstream_unavailable
from .http import SleepFn, UpstreamHttp
from .licences import licensed

# This adapter's terms (licences.py). A source with no entry fails import here.
LICENCE = licensed("ops_api")

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
            raise ops_api_not_configured()
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

    async def rebook(
        self, body: dict[str, Any], *, idempotency_key: str, token: str
    ) -> dict[str, Any]:
        """Execute a rebooking under `idempotency_key`. Returns counts, never passengers.

        This is a POST that the transport layer retries, and that is safe only
        because of the key: if the first attempt wrote and its response was lost,
        the retry is answered with the original result instead of writing again.
        """
        if not self._configured:
            raise ops_api_not_configured()
        try:
            response = await self._http.request_json(
                "POST",
                "/rebookings",
                headers={"Authorization": f"Bearer {token}", "Idempotency-Key": idempotency_key},
                json_body=body,
            )
        except httpx.HTTPError as exc:
            raise upstream_unavailable(UPSTREAM, type(exc).__name__) from exc

        if response.status_code == 401:
            raise DownstreamTokenRejected
        answer = response.json_body if isinstance(response.json_body, dict) else {}
        if response.status_code in {200, 201}:
            moved = answer.get("passengers")
            if answer.get("status") != "confirmed" or not isinstance(moved, list):
                # A 2xx we cannot read is not a confirmation. Say so; do not guess.
                raise upstream_unavailable(UPSTREAM, "the rebooking response was not usable")
            return {
                "rebooking_id": str(answer.get("rebooking_id")),
                "status": "confirmed",
                "from_flight": body["from_flight"],
                "flight_date": body["flight_date"],
                "to_flight": body["to_flight"],
                "cohort": body["cohort"],
                "passengers_rebooked": len(moved),
                "executed_at": answer.get("executed_at"),
                "replayed": response.headers.get("idempotent-replayed") == "true",
            }
        raise _rebooking_refused(response.status_code, answer)


def ops_api_not_configured() -> ToolFailure:
    return ToolFailure(
        code="ops_api_not_configured",
        message="No internal operations API is configured.",
        hint="An operator must set FLIGHTOPS_OPS_API_BASE_URL. Not retryable.",
    )


def _count(answer: dict[str, Any], name: str) -> int | None:
    value = answer.get(name)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _cohort_changed(answer: dict[str, Any]) -> ToolFailure:
    return ToolFailure(
        code="cohort_changed",
        message="Nothing was rebooked: the number of passengers in this cohort is no "
        "longer the number that was approved.",
        hint="Call flight_passenger_impact again and propose with the current count. That "
        "is a new proposal and needs a new approval.",
        details={"approved": _count(answer, "expected"), "current": _count(answer, "actual")},
    )


def _insufficient_seats(answer: dict[str, Any]) -> ToolFailure:
    return ToolFailure(
        code="insufficient_seats",
        message="Nothing was rebooked: the target flight has too few free seats.",
        hint="Propose a different target flight or a smaller cohort. Do not split the "
        "cohort across flights without telling the user.",
        details={"needed": _count(answer, "needed"), "free": _count(answer, "free")},
    )


def _flight_unknown(_answer: dict[str, Any]) -> ToolFailure:
    return ToolFailure(
        code="not_found",
        message=f"The {UPSTREAM} does not know one of these flights, or the target takes "
        "no rebookings.",
        hint="Check both flight designators with the user. Do not substitute a similar flight.",
    )


def _approver_is_requester(_answer: dict[str, Any]) -> ToolFailure:
    return ToolFailure(
        code="approver_is_proposer",
        message=f"Nothing was rebooked: the {UPSTREAM} refused an approval by the requester.",
        hint="A second person must approve. Tell the user who needs to act; do not retry.",
    )


def _rebooking_forbidden(_answer: dict[str, Any]) -> ToolFailure:
    return ToolFailure(
        code="downstream_forbidden",
        message=f"The {UPSTREAM} refused this user permission to rebook.",
        hint="The user lacks the permission downstream. Not retryable; tell the user.",
    )


def _key_reused(_answer: dict[str, Any]) -> ToolFailure:
    return ToolFailure(
        code="idempotency_conflict",
        message="Nothing was rebooked: this idempotency key already belongs to a "
        "different request.",
        hint="This is a server defect, not something the caller can fix. Report it; do not retry.",
    )


# (status, error) -> the refusal. `None` matches any error for that status.
_REFUSALS: Final[dict[tuple[int, str | None], Callable[[dict[str, Any]], ToolFailure]]] = {
    (409, "cohort_changed"): _cohort_changed,
    (409, "insufficient_seats"): _insufficient_seats,
    (404, None): _flight_unknown,
    (403, "approver_is_requester"): _approver_is_requester,
    (403, None): _rebooking_forbidden,
    (422, "idempotency_key_reused"): _key_reused,
}


def _rebooking_refused(status: int, answer: dict[str, Any]) -> ToolFailure:
    """Map the API's refusal to something the model can act on. Counts only."""
    error = answer.get("error")
    build = _REFUSALS.get((status, error if isinstance(error, str) else None))
    build = build or _REFUSALS.get((status, None))
    return build(answer) if build else upstream_unavailable(UPSTREAM, f"HTTP {status}")


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
