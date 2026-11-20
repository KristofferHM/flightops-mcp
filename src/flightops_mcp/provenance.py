"""Which sources each tool reads, and what its payload says about them.

The third closed table, after scopes and annotations: a tool with no entry cannot be
registered. An entry is a route, a function of the edition plan, because one tool
can read different sources in different editions: live traffic is OpenSky's in the
demo and adsb.lol's wherever adsb.lol is switched on.

A route answers with the sources the tool reads, or None when it has none it may
read in this plan. That one answer decides three things, so they cannot disagree:

* whether the tool is listed (`available`), which is how the startup decision
  reaches the protocol: a model is not shown a tool this deployment cannot serve;
* whether a call is refused before any upstream is asked (`not_in_edition`);
* which licence records the payload carries (`stamp`).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from typing import Any, Final

from .edition import EditionPlan
from .errors import ToolFailure
from .licences import CLASS_MEANING, source_record

__all__ = [
    "LICENCE_FIELDS",
    "TOOL_SOURCES",
    "UnsourcedToolError",
    "available",
    "may_cache",
    "not_in_edition",
    "route_for",
    "stamp",
]

Route = Callable[[EditionPlan], tuple[str, ...] | None]
# What `stamp` adds to a single-source payload's own `source` block.
LICENCE_FIELDS: Final = ("licence_class", "licence_name", "attribution", "fetched_at")


class UnsourcedToolError(KeyError):
    """A tool with no entry in the source table."""


def _fixed(*sources: str) -> Route:
    return lambda _plan: sources


def _traffic(plan: EditionPlan) -> tuple[str, ...]:
    # With no source the edition allows, the route names the one it would need, so a
    # refusal says which licence stands in the way.
    return (plan.traffic_source or "opensky",)


def _opensky(_plan: EditionPlan) -> tuple[str, ...]:
    return ("opensky",)


def _triage(plan: EditionPlan) -> tuple[str, ...]:
    # Weather always; each traffic leg only where the edition allows its source. A leg
    # without one is reported unavailable by the triage itself, as a missing credential is.
    legs = ("aviationweather", *_traffic(plan), *_opensky(plan))
    return tuple(dict.fromkeys(leg for leg in legs if plan.allows(leg)))


def _providers(plan: EditionPlan) -> tuple[str, ...] | None:
    return tuple(sorted(plan.providers)) or None


TOOL_SOURCES: Final[Mapping[str, Route]] = {
    "resolve_airport": _fixed("ourairports"),
    "search_airports": _fixed("ourairports"),
    "airport_weather": _fixed("aviationweather"),
    "weather_overview": _fixed("aviationweather"),
    "airport_live_traffic": _traffic,
    # adsb.lol answers a circle of 250 nm at most and keeps no history, so a region,
    # a track and a movement list stay OpenSky's: demo only.
    "airspace_snapshot": _opensky,
    "aircraft_track": _opensky,
    "airport_movements": _opensky,
    "disruption_triage": _triage,
    "schiphol_flights": _fixed("schiphol"),
    # Process state only. It reads no data source, so it carries no attribution.
    "upstream_status": _fixed(),
    "flight_passenger_impact": _fixed("ops_api"),
    "propose_rebooking": _fixed("ops_api"),
    "provider_flights": _providers,
}


def route_for(tool: str) -> Route:
    try:
        return TOOL_SOURCES[tool]
    except KeyError:
        raise UnsourcedToolError(tool) from None


def available(tool: str, plan: EditionPlan) -> bool:
    """Whether `tool` may run in `plan`: it has a route, and every source on it is allowed."""
    sources = route_for(tool)(plan)
    return sources is not None and all(plan.allows(source) for source in sources)


def may_cache(tool: str, plan: EditionPlan) -> bool:
    """Whether the tool cache may hold `tool`'s answer in `plan`.

    The demo keeps its caching exactly as it was. Elsewhere, an answer built from a
    source whose terms do not allow holding it (class D, the customer's own) is
    never kept past the request.
    """
    if plan.edition == "demo":
        return True
    return all(plan.licence(source).may_cache for source in route_for(tool)(plan) or ())


def not_in_edition(tool: str, plan: EditionPlan) -> ToolFailure:
    sources = route_for(tool)(plan) or ()
    refused = [plan.licence(source) for source in sources if not plan.allows(source)]
    if refused:
        terms = refused[0]
        why = (
            f"it reads {terms.name}, licence class {terms.licence_class} "
            f"({CLASS_MEANING[terms.licence_class]}; {terms.licence_name})"
        )
    else:
        why = "no source this edition may serve answers it"
    return ToolFailure(
        code="not_in_edition",
        message=f"{tool} is not available in the {plan.edition} edition: {why}.",
        hint="This deployment cannot answer it, and retrying will not change that. Say so; "
        "do not substitute another source's data.",
        details={"edition": plan.edition, "tool": tool},
    )


def stamp(
    payload: dict[str, Any], sources: Iterable[str], plan: EditionPlan, fetched_at: str
) -> None:
    """Add each source's licence record to a successful payload, in place.

    `sources` lists every source the payload was built from. A payload that already
    names its sources (a composite, or a provider answer) is left as it is: it knows
    better which parts actually answered.
    """
    if "sources" in payload:
        return
    records = [source_record(plan.licence(source), fetched_at) for source in sources]
    payload["sources"] = records
    block = payload.get("source")
    if len(records) == 1 and isinstance(block, dict):
        record = records[0]
        # A new dict: a tool may build its block from a shared constant (WEATHER_SOURCE),
        # and stamping that in place would date every later answer with this one's time.
        payload["source"] = {**block, **{name: record[name] for name in LICENCE_FIELDS}}
