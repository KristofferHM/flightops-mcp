"""Tool annotations: what each tool does to the world, in the protocol's own words.

A host reads `readOnlyHint`, `destructiveHint`, `idempotentHint` and `openWorldHint`
to decide what needs a confirmation before it runs. Without them `propose_rebooking`
looks like a weather read unless the host parses prose.

Like the scope table, this one is closed: a tool with no entry cannot be registered,
so a forgotten annotation is an import error and not a silent omission.

`openWorldHint` says whether a tool reaches outside this system's own data. The
bundled airport reference and the process's own state are closed; OpenSky, NOAA,
Schiphol and the operations API are open. `idempotentHint` and `destructiveHint`
are left unset on reads: the specification gives them meaning only when
`readOnlyHint` is false.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final

from mcp.types import ToolAnnotations

__all__ = ["TOOL_ANNOTATIONS", "UnannotatedToolError", "annotations_for"]


class UnannotatedToolError(KeyError):
    """A tool with no entry in the annotation table."""


def _read(title: str, *, open_world: bool) -> ToolAnnotations:
    return ToolAnnotations(title=title, read_only_hint=True, open_world_hint=open_world)


TOOL_ANNOTATIONS: Final[Mapping[str, ToolAnnotations]] = {
    # The bundled OurAirports snapshot: nothing outside this process is asked.
    "resolve_airport": _read("Resolve an airport", open_world=False),
    "search_airports": _read("Search the airport reference", open_world=False),
    # Process state: credit ledgers, circuits, cache counters. No upstream is called.
    "upstream_status": _read("Upstream and budget status", open_world=False),
    "airport_weather": _read("Airport weather (METAR/TAF)", open_world=True),
    "weather_overview": _read("Weather at many airports", open_world=True),
    "airport_live_traffic": _read("Live traffic near an airport", open_world=True),
    "airport_movements": _read("Airport arrivals and departures", open_world=True),
    "airspace_snapshot": _read("Regional airspace snapshot", open_world=True),
    "aircraft_track": _read("One aircraft's observed path", open_world=True),
    "disruption_triage": _read("Airport disruption triage", open_world=True),
    "schiphol_flights": _read("Schiphol published schedule", open_world=True),
    "provider_flights": _read("Flights from your own provider", open_world=True),
    "flight_passenger_impact": _read("Passenger impact counts", open_world=True),
    # The only write. Destructive: it moves passengers off a flight they were booked
    # on, which nothing in this server can undo. Idempotent, and honestly so: the
    # proposal's digest is the idempotency key sent to the operations API, which
    # answers a repeat of the same proposal with the original result instead of
    # rebooking twice (`rebooking.py`, `ops.py`). A changed argument is a new proposal.
    "propose_rebooking": ToolAnnotations(
        title="Propose a passenger rebooking",
        read_only_hint=False,
        destructive_hint=True,
        idempotent_hint=True,
        open_world_hint=True,
    ),
}


def annotations_for(tool: str) -> ToolAnnotations:
    """The annotations for `tool`. Unknown tools are an error, as for scopes."""
    try:
        return TOOL_ANNOTATIONS[tool]
    except KeyError:
        raise UnannotatedToolError(tool) from None
