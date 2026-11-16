"""The rebooking ledger of the mock ops API, free of any HTTP framework.

Two properties live here, and both are counted from the outside by the tests:

* **One key, one write.** The first request with an idempotency key writes. Every
  later request with that key and the same body gets the stored response back,
  byte for byte, and writes nothing. The same key with a different body is a
  client bug and is refused, never treated as a replay.
* **`writes` is the truth.** It gains an entry only when passengers actually
  move, so "was there a second write" is answered here, not by a client.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

# This API's own definition of a tight connection. It is not imported from the
# MCP server on purpose: two systems that can disagree are why a rebooking
# carries the passenger count its approver saw.
TIGHT_CONNECTION_MINUTES = 45
COHORTS = ("all", "connecting", "tight_connections", "needing_assistance")
REBOOKING_FIELDS = (
    "from_flight",
    "flight_date",
    "to_flight",
    "cohort",
    "reason_code",
    "expected_passengers",
    "approved_by",
    "proposal_sha256",
)
_KEY_RE = re.compile(r"^[A-Za-z0-9_\-]{16,128}$")


class Problem(Exception):
    """A refused rebooking. `detail` holds counts and field names, never a passenger."""

    def __init__(self, status: int, error: str, **detail: Any) -> None:
        super().__init__(error)
        self.status = status
        self.error = error
        self.detail = detail


@dataclass(frozen=True, slots=True)
class Outcome:
    status: int
    body: bytes
    replayed: bool


def select_cohort(rows: list[dict[str, Any]], cohort: str) -> list[dict[str, Any]]:
    if cohort == "connecting":
        return [row for row in rows if row.get("connecting_flight")]
    if cohort == "tight_connections":
        return [
            row
            for row in rows
            if row.get("connecting_flight")
            and isinstance(row.get("connection_minutes"), int)
            and row["connection_minutes"] < TIGHT_CONNECTION_MINUTES
        ]
    if cohort == "needing_assistance":
        return [row for row in rows if row.get("needs_assistance") is True]
    return list(rows)


class RebookingLedger:
    def __init__(
        self,
        manifests: dict[str, list[dict[str, Any]]],
        seats: dict[str, int],
        clock: Callable[[], float],
    ) -> None:
        self._manifests = manifests
        self._seats = seats
        self._clock = clock
        # Idempotency-Key -> (fingerprint of the request, status, the exact response bytes).
        self._stored: dict[str, tuple[str, int, bytes]] = {}
        self.writes: list[dict[str, Any]] = []

    def submit(self, key: str, body: Any, *, user: str) -> Outcome:
        if not _KEY_RE.fullmatch(key):
            raise Problem(400, "idempotency_key_required")
        field = invalid_field(body)
        if field is not None:
            raise Problem(400, "invalid_request", field=field)

        # The fingerprint is of the operation, not of who signed it off. A replay
        # approved by a second person is still the same rebooking, and the answer
        # to it is what already happened under the first approval.
        operation = {name: value for name, value in body.items() if name != "approved_by"}
        fingerprint = hashlib.sha256(
            json.dumps(operation, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        previous = self._stored.get(key)
        if previous is not None:
            if previous[0] != fingerprint:
                raise Problem(422, "idempotency_key_reused")
            return Outcome(status=previous[1], body=previous[2], replayed=True)

        raw = self._execute(key, body, user=user)
        self._stored[key] = (fingerprint, 201, raw)
        return Outcome(status=201, body=raw, replayed=False)

    def _execute(self, key: str, body: dict[str, Any], *, user: str) -> bytes:
        if body["approved_by"] == user:
            raise Problem(403, "approver_is_requester")
        source, target = body["from_flight"], body["to_flight"]
        if source not in self._manifests or target not in self._seats:
            raise Problem(404, "flight_not_found")
        cohort = select_cohort(self._manifests[source], body["cohort"])
        if len(cohort) != body["expected_passengers"]:
            # The approver signed off on a number. If the manifest moved since, the
            # approval no longer describes what would happen, so nothing happens.
            raise Problem(
                409, "cohort_changed", expected=body["expected_passengers"], actual=len(cohort)
            )
        if len(cohort) > self._seats[target]:
            raise Problem(409, "insufficient_seats", needed=len(cohort), free=self._seats[target])

        moved = {id(row) for row in cohort}
        self._manifests[source] = [row for row in self._manifests[source] if id(row) not in moved]
        self._seats[target] -= len(cohort)
        # Derived from the key, so the id is as repeatable as the request.
        rebooking_id = "RBK-" + hashlib.sha256(key.encode()).hexdigest()[:10].upper()
        self.writes.append(
            {
                "rebooking_id": rebooking_id,
                "idempotency_key": key,
                "from_flight": source,
                "to_flight": target,
                "passengers_moved": len(cohort),
                "user": user,
                "approved_by": body["approved_by"],
            }
        )
        return json.dumps(
            {
                "rebooking_id": rebooking_id,
                "status": "confirmed",
                "from_flight": source,
                "flight_date": body["flight_date"],
                "to_flight": target,
                "cohort": body["cohort"],
                "executed_at": datetime.fromtimestamp(self._clock(), tz=UTC).isoformat(),
                # Identifying on purpose: the caller is expected to reduce this to a count.
                "passengers": [
                    {
                        "passenger_name": row["passenger_name"],
                        "booking_ref": row["booking_ref"],
                        "email": row["email"],
                        "new_flight": target,
                    }
                    for row in cohort
                ],
            },
            separators=(",", ":"),
        ).encode()


def invalid_field(body: Any) -> str | None:
    """The name of the first field that is missing or malformed, or None."""
    if not isinstance(body, dict):
        return "body"
    for name in REBOOKING_FIELDS:
        value = body.get(name)
        if name == "expected_passengers":
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                return name
        elif not isinstance(value, str) or not value:
            return name
    if body["cohort"] not in COHORTS:
        return "cohort"
    try:
        date.fromisoformat(body["flight_date"])
    except ValueError:
        return "flight_date"
    return None
