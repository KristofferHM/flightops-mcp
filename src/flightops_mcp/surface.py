"""The protocol surface around the tools: the airport reference as resources, two
prompts a host can offer, and completions for airport codes.

None of this reaches an upstream. The resources and completions read the bundled
reference data; the prompts are text. They are installed on both servers by
`create_server`, so the stdio and HTTP servers offer the same surface.

On the authenticated server a resource read or a completion needs
`flightops.reference.read`, the scope `resolve_airport` needs: the same data
must not be free through one door and scoped through another. A refusal is a
protocol error, because neither method has a tool-result channel to carry data.
Prompts carry no data and need no scope.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any, Final

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ResourceError, ResourceNotFoundError
from mcp.server.mcpserver.prompts import Prompt
from mcp.shared.exceptions import MCPError
from mcp.types import (
    INTERNAL_ERROR,
    INVALID_REQUEST,
    Completion,
    CompletionArgument,
    CompletionContext,
    PromptReference,
    ResourceTemplateReference,
)

from .auth import Scopes, current_access_token
from .runtime import get_runtime

__all__ = [
    "AIRPORT_INDEX_URI",
    "AIRPORT_TEMPLATE",
    "COMPLETION_CAP",
    "DIRECTIONS",
    "brief_movements",
    "install_reference_surface",
    "triage_airport",
]

AIRPORT_TEMPLATE: Final = "airport://{icao}"
AIRPORT_INDEX_URI: Final = "airport://index"
JSON_MIME: Final = "application/json"
# The specification caps `completion/complete` at 100 values per response.
COMPLETION_CAP: Final = 100
DIRECTIONS: Final = ("arrival", "departure", "both")
# The prompts whose `airport` argument takes an ICAO code.
AIRPORT_PROMPTS: Final = frozenset({"triage_airport", "brief_movements"})
SOURCE: Final = {"name": "OurAirports", "licence": "Public domain", "kind": "bundled CSV snapshot"}


# --- prompts -------------------------------------------------------------------
def triage_airport(airport: str) -> str:
    """Assess whether one airport is disrupted now, and report what cannot be seen."""
    return f"""\
Assess whether the airport {airport} is disrupted right now.

1. If "{airport}" is not a four-letter ICAO code, resolve it with `resolve_airport` \
first. Never guess a code.
2. Call `disruption_triage` once, with the ICAO code. Do not also call the weather, \
traffic and movements tools: the triage makes those calls itself, and calling them \
again spends OpenSky credits twice.
3. Report the severity exactly as returned: low, elevated, high or unknown. Do not \
raise it or lower it, and say that it is advisory.
4. If `assessment.unknowns` or `degraded` is non-empty, say so first, before any \
reason. "Unknown" means the data could not show it: a source that did not answer, \
a METAR too old to describe now, a credential that is not configured. It is not \
evidence that operations are normal, and it is not evidence of a disruption.
5. Then give each reason, and the `why_not_higher` and `why_not_lower` lines, with \
their numbers.
6. Do not say the airport is open, closed or operating normally. This server reports \
observations; it does not authorise any operational decision."""


def brief_movements(airport: str, direction: str = "both") -> str:
    """Brief recent arrivals and/or departures at one airport, as estimates."""
    asked = {"arrival": "arrivals", "departure": "departures"}.get(
        direction, "arrivals and departures"
    )
    return f"""\
Brief the {asked} at the airport {airport} over the last hour.

1. If "{airport}" is not a four-letter ICAO code, resolve it with `resolve_airport` \
first. Never guess a code.
2. Call `airport_movements` with direction "{direction}". Each direction costs 4 \
OpenSky credits, so ask only for what was requested.
3. Report the count per direction and the window it covers. Say that these are \
estimates from ADS-B: origin and destination are inferred from where an aircraft was \
first and last heard, not taken from a schedule.
4. If `unknowns` is non-empty, lead with it. An empty list is not evidence that \
nothing flew: OpenSky only hears ADS-B-equipped aircraft inside receiver coverage, and \
a direction listed as unavailable was never asked about at all.
5. Do not call a flight delayed, cancelled or on time from these data. They record \
what was heard, not what was planned."""


# --- resources -----------------------------------------------------------------
def _airport_document(record: dict[str, Any]) -> str:
    """One airport's reference record, as JSON. The same fields `resolve_airport` names."""
    return json.dumps(
        {
            "icao": record["ident"],
            "iata": record["iata"],
            "name": record["name"],
            "type": record["type"],
            "municipality": record["municipality"],
            "country": record["iso_country"],
            "region": record["iso_region"],
            "continent": record.get("continent"),
            "latitude": record["latitude"],
            "longitude": record["longitude"],
            "elevation_ft": record["elevation_ft"],
            "scheduled_service": record["scheduled_service"],
            "source": SOURCE,
        },
        indent=2,
    )


def _reference_guard(authenticated: bool) -> Callable[[str], None]:
    """A check that the caller may read reference data. A no-op on the stdio server."""

    def check(method: str) -> None:
        if not authenticated:
            return
        token = current_access_token()
        # Fails closed: no verified token in context is a refusal, as in `scoped`.
        if token is None or Scopes.REFERENCE_READ not in token.scopes:
            raise MCPError(
                code=INVALID_REQUEST,
                message=f"{method} needs the scope {Scopes.REFERENCE_READ}.",
                data={"error": "insufficient_scope", "required_scopes": [Scopes.REFERENCE_READ]},
            )

    return check


def _reference_ready() -> Any:
    airports = get_runtime().airports
    if not airports.exists:
        raise ResourceError(
            f"The airport reference database is missing at {airports.path}; "
            "an operator must build it."
        )
    return airports


def install_reference_surface(server: MCPServer, *, authenticated: bool) -> None:
    """Add the resources, prompts and completion handler to `server`."""
    guard = _reference_guard(authenticated)

    @server.resource(
        AIRPORT_INDEX_URI,
        name="airport-index",
        title="Airport reference: what is available",
        description="How many airports the bundled reference holds, where it comes from, "
        f"and how to read one: {AIRPORT_TEMPLATE} with an ICAO code.",
        mime_type=JSON_MIME,
    )
    def airport_index() -> str:
        guard("resources/read")
        airports = _reference_ready()
        return json.dumps(
            {
                "airports": airports.count(),
                "uri_template": AIRPORT_TEMPLATE,
                "codes": "ICAO idents, e.g. ESGG. Find one with the search_airports or "
                "resolve_airport tools, or ask for completions of the icao argument.",
                "source": SOURCE,
            },
            indent=2,
        )

    @server.resource(
        AIRPORT_TEMPLATE,
        name="airport",
        title="Airport reference record",
        description="One airport from the bundled OurAirports reference: codes, name, "
        "location, elevation. Offline; reading it spends no tool call and no credits.",
        mime_type=JSON_MIME,
    )
    def airport_record(icao: str) -> str:
        guard("resources/read")
        record = _reference_ready().get(icao)
        if record is None:
            # A protocol error, not an empty document: an empty body would read as
            # an airport with no data rather than as no such airport.
            raise ResourceNotFoundError(f"No airport {icao!r} in the reference data.")
        return _airport_document(record)

    server.add_prompt(
        Prompt.from_function(
            triage_airport,
            title="Triage an airport",
            description="Run the disruption triage for one airport and report its severity, "
            "reasons and unknowns without overstating them.",
        )
    )
    server.add_prompt(
        Prompt.from_function(
            brief_movements,
            title="Brief arrivals and departures",
            description="Brief an airport's estimated arrivals and/or departures in the last "
            "hour, saying what ADS-B cannot show.",
        )
    )

    @server.completion()  # type: ignore[no-untyped-call, untyped-decorator]
    async def complete(
        ref: PromptReference | ResourceTemplateReference,
        argument: CompletionArgument,
        _context: CompletionContext | None,
    ) -> Completion | None:
        if _takes_airport(ref, argument.name):
            guard("completion/complete")
            try:
                airports = _reference_ready()
            except ResourceError as exc:
                raise MCPError(code=INTERNAL_ERROR, message=str(exc)) from None
            values, total = airports.idents_with_prefix(argument.value, limit=COMPLETION_CAP)
            return Completion(values=values, total=total, has_more=total > len(values))
        if (
            isinstance(ref, PromptReference)
            and ref.name == "brief_movements"
            and argument.name == "direction"
        ):
            wanted = argument.value.strip().lower()
            values = [d for d in DIRECTIONS if d.startswith(wanted)]
            return Completion(values=values, total=len(values), has_more=False)
        return None


def _takes_airport(ref: PromptReference | ResourceTemplateReference, argument: str) -> bool:
    if isinstance(ref, ResourceTemplateReference):
        return ref.uri == AIRPORT_TEMPLATE and argument == "icao"
    return ref.name in AIRPORT_PROMPTS and argument == "airport"
