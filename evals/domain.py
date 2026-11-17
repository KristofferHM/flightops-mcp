"""Whether a tool call's arguments are valid: by the schema, and by the domain.

Two different questions, and a call has to pass both:

* **Schema.** The arguments satisfy the JSON schema the server published for the
  tool in that run. A string where a number belongs fails here.
* **Domain.** The values mean something. `airport_weather(airport="Gothenburg")`
  is a perfectly good string and not an airport code; a rebooking dated last
  year is a perfectly good date. The server tolerates some of these (it clamps a
  radius, it answers `not_found`), and they are still a wrong call: the model
  guessed where it was told to resolve.

The domain rules are written here from the tools' documented contracts, not
imported from the server's own validation, so that a server which starts
accepting nonsense does not take the check with it. The two exceptions are the
closed vocabularies of a rebooking, which have one definition by design.
"""

from __future__ import annotations

import csv
import re
from collections.abc import Callable, Mapping
from datetime import date, timedelta
from functools import cache
from typing import Any, Final

from jsonschema import Draft202012Validator

from agent.localstack import SEED_AIRPORTS
from flightops_mcp.rebooking import COHORTS, MAX_PASSENGERS, REASON_CODES

_FLIGHT: Final = re.compile(r"^[A-Z0-9]{2,3}\d{1,4}[A-Z]?$")
_ICAO: Final = re.compile(r"^[A-Z]{4}$")
_DIRECTIONS: Final = frozenset({"arrival", "departure", "both"})
# How far from the scenario's own date a rebooking may be dated.
_DAYS_BACK: Final = 1
_DAYS_AHEAD: Final = 7


@cache
def known_airports() -> frozenset[str]:
    """ICAO codes in the reference data the offline stack is built from."""
    with SEED_AIRPORTS.open(encoding="utf-8", newline="") as handle:
        return frozenset(row["ident"].upper() for row in csv.DictReader(handle))


def schema_problems(schema: Mapping[str, Any], arguments: Mapping[str, Any]) -> list[str]:
    """Violations of the tool's published input schema, one sentence each."""
    validator = Draft202012Validator(dict(schema))
    return sorted(
        f"{'.'.join(str(part) for part in error.absolute_path) or 'arguments'}: {error.message}"
        for error in validator.iter_errors(dict(arguments))
    )


def domain_problems(tool: str, arguments: Mapping[str, Any], *, today: date) -> list[str]:
    """Values that are well-formed and still wrong. `today` is the scenario's date."""
    check = _RULES.get(tool)
    return check(arguments, today) if check else []


def _airport(arguments: Mapping[str, Any]) -> list[str]:
    value = arguments.get("airport")
    if not isinstance(value, str) or not _ICAO.fullmatch(value):
        return [f"airport {value!r} is not an ICAO code; resolve it first"]
    if value not in known_airports():
        return [f"airport {value!r} is not in the reference data"]
    return []


def _within(arguments: Mapping[str, Any], name: str, low: float, high: float) -> list[str]:
    if name not in arguments:
        return []
    value = arguments[name]
    if isinstance(value, bool) or not isinstance(value, int | float) or not low <= value <= high:
        return [f"{name} {value!r} is outside {low:g}-{high:g}"]
    return []


def _flight(arguments: Mapping[str, Any], name: str) -> list[str]:
    value = arguments.get(name)
    if not isinstance(value, str) or not _FLIGHT.fullmatch(value):
        return [f"{name} {value!r} is not a flight designator"]
    return []


def _resolve_airport(arguments: Mapping[str, Any], _today: date) -> list[str]:
    query = arguments.get("query")
    problems = [] if isinstance(query, str) and query.strip() else ["query is empty"]
    return problems + _within(arguments, "limit", 1, 20)


def _weather(arguments: Mapping[str, Any], _today: date) -> list[str]:
    return _airport(arguments)


def _traffic(arguments: Mapping[str, Any], _today: date) -> list[str]:
    return [
        *_airport(arguments),
        *_within(arguments, "radius_nm", 5, 200),
        *_within(arguments, "max_aircraft", 1, 200),
    ]


def _movements(arguments: Mapping[str, Any], _today: date) -> list[str]:
    problems = [*_airport(arguments), *_within(arguments, "window_minutes", 5, 60)]
    direction = arguments.get("direction", "both")
    if direction not in _DIRECTIONS:
        problems.append(f"direction {direction!r} is not arrival, departure or both")
    return problems


def _triage(arguments: Mapping[str, Any], _today: date) -> list[str]:
    return [*_airport(arguments), *_within(arguments, "radius_nm", 5, 200)]


def _impact(arguments: Mapping[str, Any], _today: date) -> list[str]:
    return _flight(arguments, "flight")


def _rebooking(arguments: Mapping[str, Any], today: date) -> list[str]:
    problems = [*_flight(arguments, "from_flight"), *_flight(arguments, "to_flight")]
    if not problems and arguments["from_flight"] == arguments["to_flight"]:
        problems.append("from_flight and to_flight are the same flight")
    try:
        when = date.fromisoformat(str(arguments.get("flight_date")))
    except ValueError:
        problems.append(f"flight_date {arguments.get('flight_date')!r} is not YYYY-MM-DD")
    else:
        earliest = today - timedelta(days=_DAYS_BACK)
        latest = today + timedelta(days=_DAYS_AHEAD)
        if not earliest <= when <= latest:
            problems.append(f"flight_date {when} is outside {earliest} to {latest}")
    if arguments.get("cohort") not in COHORTS:
        problems.append(f"cohort {arguments.get('cohort')!r} is not a cohort")
    if arguments.get("reason_code") not in REASON_CODES:
        problems.append(f"reason_code {arguments.get('reason_code')!r} is not a reason code")
    count = arguments.get("passenger_count")
    if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= MAX_PASSENGERS:
        problems.append(f"passenger_count {count!r} is not a count of passengers")
    return problems


_RULES: Final[dict[str, Callable[[Mapping[str, Any], date], list[str]]]] = {
    "resolve_airport": _resolve_airport,
    "airport_weather": _weather,
    "airport_live_traffic": _traffic,
    "airport_movements": _movements,
    "disruption_triage": _triage,
    "flight_passenger_impact": _impact,
    "propose_rebooking": _rebooking,
}
