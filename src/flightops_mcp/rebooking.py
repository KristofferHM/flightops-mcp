"""A rebooking proposal: what the model asks for, in the one form everything binds to.

A proposal is a closed vocabulary on purpose. It names a cohort and a reason
code, never a person and never free text, so it can be logged, shown to an
approver and written into an agent transcript without a redaction pass standing
between it and a leak.

Three things are derived from the same canonical bytes, and that is the design:

* the **digest** the approver signs,
* the **idempotency key** the write is sent under,
* the **audit record** of what was proposed.

Change any field and all three change together. There is no way to approve one
payload and execute another, and no way to send the same payload twice under two
keys.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import date
from typing import Any, Final

from .errors import ToolFailure
from .ops import normalise_flight

COHORTS: Final = ("tight_connections", "connecting", "needing_assistance", "all")
REASON_CODES: Final = ("weather", "technical", "crew", "atc", "late_inbound", "other")
MAX_PASSENGERS: Final = 500
_CANONICAL_VERSION: Final = 1


@dataclass(frozen=True, slots=True)
class Proposal:
    from_flight: str
    flight_date: str
    to_flight: str
    cohort: str
    reason_code: str
    passenger_count: int

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    def canonical(self) -> bytes:
        """The bytes that are hashed, and that an approver is shown. Versioned."""
        return _canonical({"v": _CANONICAL_VERSION, **self.as_dict()})

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.canonical()).hexdigest()

    @property
    def idempotency_key(self) -> str:
        # From the content, not a counter or a clock: a retry of the same proposal
        # produces the same key on any replica, after any restart. The date is part
        # of the content, so tomorrow's identical-looking rebooking is another key.
        return "rbk_" + self.sha256[:32]


def _canonical(value: Any) -> bytes:
    """Sorted keys, no whitespace, ASCII: one byte string per value."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode(
        "ascii"
    )


def approved_digest(detail: dict[str, Any]) -> str | None:
    """The digest of the proposal an approval grant describes, or None if it describes none.

    The grant carries the proposal itself, as its approver was shown it, not a
    hash someone computed for them. Hashing those fields here is what makes "the
    approver saw X" and "X is what runs" the same statement. Nothing is
    normalised: a grant for `2.0` passengers is not a grant for `2`.
    """
    described = detail.get("proposal")
    if not isinstance(described, dict) or not described:
        return None
    try:
        return hashlib.sha256(_canonical(described)).hexdigest()
    except (TypeError, ValueError):
        return None


def build_proposal(
    *,
    from_flight: str,
    flight_date: str,
    to_flight: str,
    cohort: str,
    reason_code: str,
    passenger_count: int,
) -> Proposal:
    """Validate and normalise the model's arguments. Raises `ToolFailure` on a bad one."""
    source = normalise_flight(from_flight)
    target = normalise_flight(to_flight)
    if source == target:
        raise _invalid(
            "from_flight and to_flight are the same flight.",
            "Name the flight the passengers should move to.",
        )
    try:
        day = date.fromisoformat(flight_date.strip())
    except ValueError:
        raise _invalid(
            f"{flight_date!r} is not a calendar date.",
            "Use the departure date of from_flight as YYYY-MM-DD.",
        ) from None
    group = cohort.strip().lower()
    if group not in COHORTS:
        raise _invalid(
            f"{cohort!r} is not a cohort.",
            f"Use one of: {', '.join(COHORTS)}. A rebooking names a group, never a passenger.",
        )
    reason = reason_code.strip().lower()
    if reason not in REASON_CODES:
        raise _invalid(
            f"{reason_code!r} is not a reason code.",
            f"Use one of: {', '.join(REASON_CODES)}. Free text is not accepted.",
        )
    if isinstance(passenger_count, bool) or not 1 <= passenger_count <= MAX_PASSENGERS:
        raise _invalid(
            f"passenger_count must be between 1 and {MAX_PASSENGERS}.",
            "Use the count flight_passenger_impact returned for this cohort. If it is 0 "
            "there is nobody to rebook.",
        )
    return Proposal(
        from_flight=source,
        flight_date=day.isoformat(),
        to_flight=target,
        cohort=group,
        reason_code=reason,
        passenger_count=passenger_count,
    )


def _invalid(message: str, hint: str) -> ToolFailure:
    return ToolFailure(code="invalid_argument", message=message, hint=hint)
