"""The scope model: which permission each tool requires.

This table is the authorization policy. A tool that is not in it cannot be
registered, so "forgot to protect the new tool" is an import error rather than
an open endpoint.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Final


class UnknownToolError(KeyError):
    """A tool name with no entry in the scope table."""


class Scopes:
    """Scope names, and the mapping from tool to the scopes it needs."""

    REFERENCE_READ: Final = "flightops.reference.read"
    WEATHER_READ: Final = "flightops.weather.read"
    TRAFFIC_READ: Final = "flightops.traffic.read"
    STATUS_READ: Final = "flightops.status.read"
    # Published airport schedules (Schiphol). Its own domain: a schedule is what an
    # airport says will happen, traffic is what receivers heard, and an operator may
    # grant one without the other.
    SCHEDULE_READ: Final = "flightops.schedule.read"
    # Also the scope requested downstream when exchanging the user's token.
    PASSENGERS_READ: Final = "flightops.passengers.read"
    # The only write. Its own scope, not an extension of a read one: a token that can
    # count passengers must not thereby be able to move them. Like PASSENGERS_READ it
    # is also the scope requested downstream, so the exchange can only narrow to it.
    REBOOKING_WRITE: Final = "flightops.rebooking.write"
    # Held by the person who approves a rebooking, never required of a caller, so
    # it has no tool entry below. It is checked on the approval grant instead
    # (auth/approval.py), and holding it does not let anyone call anything.
    REBOOKING_APPROVE: Final = "flightops.rebooking.approve"

    # One scope per data domain, not per tool: OpenSky traffic and movements are
    # the same upstream, the same credits and the same sensitivity.
    _BY_TOOL: Final[Mapping[str, frozenset[str]]] = {
        "resolve_airport": frozenset({REFERENCE_READ}),
        "airport_weather": frozenset({WEATHER_READ}),
        "airport_live_traffic": frozenset({TRAFFIC_READ}),
        "airport_movements": frozenset({TRAFFIC_READ}),
        # A composite needs everything its parts need. It calls them directly, so
        # this entry is the only check that stands between a caller and both.
        "disruption_triage": frozenset({WEATHER_READ, TRAFFIC_READ}),
        "schiphol_flights": frozenset({SCHEDULE_READ}),
        "upstream_status": frozenset({STATUS_READ}),
        "flight_passenger_impact": frozenset({PASSENGERS_READ}),
        # Proposing needs the write scope alone. It reads no manifest itself: the
        # caller supplies the count it saw, and the ops API refuses if that is stale.
        "propose_rebooking": frozenset({REBOOKING_WRITE}),
    }

    @classmethod
    def for_tool(cls, name: str) -> frozenset[str]:
        """The scopes a caller must hold to run `name`. Unknown tools are an error."""
        try:
            return cls._BY_TOOL[name]
        except KeyError:
            raise UnknownToolError(name) from None

    @classmethod
    def missing(cls, name: str, granted: Iterable[str]) -> list[str]:
        """What `granted` lacks for `name`, sorted so messages and headers are stable."""
        return sorted(cls.for_tool(name) - set(granted))

    @classmethod
    def all(cls) -> frozenset[str]:
        return frozenset().union(*cls._BY_TOOL.values())
