"""The offline backend: a scripted policy, no model, no network, no chance.

This is the backend CI runs. It is a planner like the other two, behind the same
interface, so everything after the plan is the real thing: real tool dispatch
over MCP, the real approval gate, the real transcript.

A policy is a generator. It yields the tool calls for a turn, is sent what they
came back with, and returns the final answer. What it does next depends on what
the tools said, which is what makes it a policy and not a recording: the count
in a proposal is the count the server reported, and a refusal ends the run.

Two things a policy must never do, because either would break determinism: read
a clock, and repeat a timestamp from a tool result in its answer. The server
runs on real time. The policy reports counts, codes and identifiers.

A policy is told which plan to follow by its scenario, and a model is not. That
is the limit of what an offline run can show: it exercises the server's contract
and the policy's reaction to it, and says nothing about how a model would plan
(docs/EVALS.md).
"""

from __future__ import annotations

from collections.abc import Callable, Generator, Mapping, Sequence
from typing import Any, Final

from .provider import Observation, ProviderFailure, Task, ToolCall, Turn

# What a policy yields: (tool name, arguments) for each call of one turn.
Calls = list[tuple[str, dict[str, Any]]]
Policy = Callable[[Mapping[str, Any]], Generator[Calls, Sequence[Observation], str]]

# `flight_passenger_impact` reports a count per cohort under these names.
_COUNT_OF: Final = {
    "all": "booked",
    "connecting": "connecting",
    "tight_connections": "tight_connections",
    "needing_assistance": "needing_assistance",
}


def rebook_cohort(params: Mapping[str, Any]) -> Generator[Calls, Sequence[Observation], str]:
    """Count who a flight affects, then propose moving that cohort. One approval, no retry.

    `submissions` above 1 submits the same proposal again after it succeeded, as a
    client would after a timeout. The operations API must answer the repeat with
    the original result.
    """
    flight, target, cohort = params["from_flight"], params["to_flight"], params["cohort"]
    (impact,) = yield [("flight_passenger_impact", {"flight": flight})]
    if not impact.ok:
        return (
            f"Nothing was rebooked. Passenger impact for {flight} could not be read "
            f"({impact.code}), so there is no count to propose."
        )
    counts = impact.payload.get("passengers")
    count = counts.get(_COUNT_OF.get(cohort, cohort)) if isinstance(counts, dict) else None
    if not isinstance(count, int) or isinstance(count, bool) or count < 1:
        unknowns = len(impact.payload.get("unknowns") or [])
        return (
            f"Nothing was rebooked. {flight} has no passengers on file in cohort {cohort} "
            f"({unknowns} unknown(s) reported). That is not evidence nobody is affected."
        )

    proposal = {
        "from_flight": flight,
        "flight_date": params["flight_date"],
        "to_flight": target,
        "cohort": cohort,
        "reason_code": params["reason_code"],
        "passenger_count": count,
    }
    submissions = int(params.get("submissions", 1))
    replays = 0
    result = impact
    for _ in range(submissions):
        (result,) = yield [("propose_rebooking", proposal)]
        if not result.ok:
            # The server's hint for every approval refusal is the same: do not retry.
            return (
                f"Nothing was rebooked. The proposal to move {count} passenger(s) "
                f"({cohort}) from {flight} to {target} was refused: {result.code}."
            )
        replays += 1 if (result.payload.get("idempotency") or {}).get("replayed") else 0

    executed = result.payload.get("result") or {}
    audit = result.payload.get("audit") or {}
    answer = (
        f"Rebooked {executed.get('passengers_rebooked')} passenger(s) ({cohort}) from {flight} "
        f"to {target} as {executed.get('rebooking_id')}, approved by {audit.get('approved_by')}."
    )
    if replays:
        answer += f" {replays} of {submissions} submissions were answered with the original result."
    return answer


def _miss(query: str, resolved: Observation) -> str:
    """The sentence that ends a run whose airport did not resolve."""
    if resolved.ok or resolved.code == "not_found":
        return f"No airport matched {query!r} ({resolved.code}). Nothing was looked up."
    # A refusal is not a miss: the airport may well exist.
    return f"{query!r} could not be resolved ({resolved.code}). Nothing was looked up."


def _resolve(query: str) -> Generator[Calls, Sequence[Observation], tuple[str | None, str]]:
    """Resolve an airport. Gives its ICAO code, or None and the sentence that ends the run."""
    (resolved,) = yield [("resolve_airport", {"query": query, "limit": 1})]
    matches = resolved.payload.get("matches") if resolved.ok else None
    if not matches:
        return None, _miss(query, resolved)
    return str(matches[0]["icao"]), ""


def airport_brief(params: Mapping[str, Any]) -> Generator[Calls, Sequence[Observation], str]:
    """Resolve an airport, read its weather, and say what is not known."""
    icao, miss = yield from _resolve(params["airport"])
    if icao is None:
        return miss
    (weather,) = yield [("airport_weather", {"airport": icao})]
    if not weather.ok:
        return f"Weather for {icao} is unavailable ({weather.code}). Conditions are unknown."
    unknowns = weather.payload.get("unknowns") or []
    flags = weather.payload.get("impact_flags") or []
    if unknowns:
        return (
            f"Conditions at {icao} are unknown: {len(unknowns)} gap(s) in the weather data. "
            "That is not evidence that conditions are normal."
        )
    return f"{icao}: {len(flags)} weather impact flag(s) reported."


def airport_lookup(params: Mapping[str, Any]) -> Generator[Calls, Sequence[Observation], str]:
    """Resolve an airport and name it. The reference data is the whole answer."""
    query = params["airport"]
    (resolved,) = yield [("resolve_airport", {"query": query, "limit": 1})]
    matches = resolved.payload.get("matches") if resolved.ok else None
    if not matches:
        return _miss(query, resolved)
    found = matches[0]
    return f"{found['icao']} (IATA {found['iata'] or 'none'}): {found['name']}."


def airport_traffic(params: Mapping[str, Any]) -> Generator[Calls, Sequence[Observation], str]:
    """Resolve an airport and count the aircraft heard around it."""
    icao, miss = yield from _resolve(params["airport"])
    if icao is None:
        return miss
    arguments: dict[str, Any] = {"airport": icao}
    if "radius_nm" in params:
        arguments["radius_nm"] = params["radius_nm"]
    (traffic,) = yield [("airport_live_traffic", arguments)]
    if not traffic.ok:
        return (
            f"Live traffic around {icao} is unavailable ({traffic.code}). "
            "What is flying there is unknown."
        )
    unknowns = traffic.payload.get("unknowns") or []
    if unknowns:
        return (
            f"Traffic around {icao} is unknown: {len(unknowns)} gap(s) in the traffic data. "
            "That is not evidence that the airspace is empty or the airport closed."
        )
    counts = traffic.payload.get("counts") or {}
    return (
        f"{icao}: {counts.get('returned')} aircraft heard, {counts.get('airborne')} airborne "
        f"and {counts.get('on_ground')} on the ground."
    )


def airport_movements(params: Mapping[str, Any]) -> Generator[Calls, Sequence[Observation], str]:
    """Resolve an airport and count its estimated arrivals and departures."""
    icao, miss = yield from _resolve(params["airport"])
    if icao is None:
        return miss
    arguments: dict[str, Any] = {"airport": icao, "direction": params.get("direction", "both")}
    if "window_minutes" in params:
        arguments["window_minutes"] = params["window_minutes"]
    (movements,) = yield [("airport_movements", arguments)]
    if not movements.ok:
        return (
            f"Movements at {icao} are unavailable ({movements.code}). "
            "Arrivals and departures are unknown."
        )
    unknowns = movements.payload.get("unknowns") or []
    if unknowns:
        return (
            f"Movements at {icao} are unknown: {len(unknowns)} gap(s) in the movement data. "
            "That is not evidence that nothing is flying."
        )
    counts = movements.payload.get("counts") or {}
    counted = ", ".join(f"{count} {which}(s)" for which, count in sorted(counts.items()))
    return f"{icao}: {counted} estimated from ADS-B. These are estimates, not a schedule."


def airport_triage(params: Mapping[str, Any]) -> Generator[Calls, Sequence[Observation], str]:
    """Resolve an airport and run the composite triage. Severity is quoted, never upgraded."""
    icao, miss = yield from _resolve(params["airport"])
    if icao is None:
        return miss
    arguments: dict[str, Any] = {"airport": icao}
    if "radius_nm" in params:
        arguments["radius_nm"] = params["radius_nm"]
    (triage,) = yield [("disruption_triage", arguments)]
    if not triage.ok:
        return (
            f"Triage for {icao} is unavailable ({triage.code}). "
            "Whether the airport is disrupted is unknown."
        )
    assessment = triage.payload.get("assessment") or {}
    severity = assessment.get("severity")
    reasons = assessment.get("reasons") or []
    unknowns = assessment.get("unknowns") or []
    degraded = triage.payload.get("degraded") or []
    if unknowns or degraded:
        return (
            f"{icao}: severity {severity}, with {len(reasons)} reason(s), {len(unknowns)} "
            f"unknown(s) and {len(degraded)} data source(s) unavailable. That is not evidence "
            "that operations are normal."
        )
    return (
        f"{icao}: severity {severity} on {len(reasons)} reason(s), with no gaps reported in "
        "the data behind it. Advisory only."
    )


def passenger_impact(params: Mapping[str, Any]) -> Generator[Calls, Sequence[Observation], str]:
    """Count who a flight affects. Counts only; nobody is named and nothing is proposed."""
    flight = params["flight"]
    (impact,) = yield [("flight_passenger_impact", {"flight": flight})]
    if not impact.ok:
        return (
            f"Passenger impact for {flight} could not be read ({impact.code}). "
            "Who is affected is unknown."
        )
    unknowns = impact.payload.get("unknowns") or []
    if unknowns:
        return (
            f"{flight} has no passengers on file ({len(unknowns)} unknown(s) reported). "
            "That is not evidence that nobody is affected."
        )
    counts = impact.payload.get("passengers") or {}
    return (
        f"{flight}: {counts.get('booked')} booked, {counts.get('connecting')} connecting, "
        f"{counts.get('tight_connections')} with tight connections, "
        f"{counts.get('needing_assistance')} needing assistance."
    )


def upstream_check(_params: Mapping[str, Any]) -> Generator[Calls, Sequence[Observation], str]:
    """Ask the server what state its upstreams and credentials are in."""
    (status,) = yield [("upstream_status", {})]
    if not status.ok:
        return (
            f"Upstream status is unavailable ({status.code}). "
            "The state of the upstreams is unknown."
        )
    budget = status.payload.get("budget") or {}
    configured = (status.payload.get("credentials") or {}).get("opensky_configured")
    open_circuits = sum(
        1 for circuit in status.payload.get("circuits") or [] if circuit.get("state") == "open"
    )
    return (
        f"OpenSky client {'configured' if configured else 'absent'}; "
        f"{budget.get('credits_spent_this_hour')} of {budget.get('local_budget_per_hour')} "
        f"credits spent this hour; {open_circuits} circuit(s) open."
    )


def decline(params: Mapping[str, Any]) -> Generator[Calls, Sequence[Observation], str]:
    """Call nothing. For a request no tool on this server may serve."""
    # No calls are yielded: the refusal is the whole plan.
    yield from ()
    return (
        f"That is not something this server can do: {params['reason']} "
        "No tool was called and nothing was changed."
    )


POLICIES: Final[dict[str, Policy]] = {
    "rebook_cohort": rebook_cohort,
    "airport_brief": airport_brief,
    "airport_lookup": airport_lookup,
    "airport_traffic": airport_traffic,
    "airport_movements": airport_movements,
    "airport_triage": airport_triage,
    "passenger_impact": passenger_impact,
    "upstream_check": upstream_check,
    "decline": decline,
}


class OfflineProvider:
    name = "offline"
    model = "offline"

    def __init__(self, policy: str, params: Mapping[str, Any]) -> None:
        if policy not in POLICIES:
            raise ValueError(
                f"unknown offline policy {policy!r}; have {', '.join(sorted(POLICIES))}"
            )
        self._policy = POLICIES[policy]
        self._params = params
        self._script: Generator[Calls, Sequence[Observation], str] | None = None
        self._started = False
        self._calls = 0
        self._offered: frozenset[str] = frozenset()

    async def begin(self, task: Task) -> None:
        self._offered = frozenset(tool.name for tool in task.tools)
        self._script = self._policy(self._params)
        self._started = False
        self._calls = 0

    async def turn(self, observations: Sequence[Observation]) -> Turn:
        if self._script is None:
            raise ProviderFailure("not_started", "turn() was called before begin()")
        try:
            wanted = self._script.send(observations) if self._started else next(self._script)
        except StopIteration as done:
            return Turn(text=str(done.value), stop="answer")
        self._started = True
        calls = []
        for name, arguments in wanted:
            if name not in self._offered:
                # A script that asks for a tool the server does not offer is a
                # broken script. Say so; do not send a call that cannot land.
                raise ProviderFailure("tool_not_offered", f"the policy asked for {name}")
            self._calls += 1
            # Ids are a count, so they are the same in every run.
            calls.append(ToolCall(id=f"call-{self._calls}", name=name, arguments=dict(arguments)))
        return Turn(tool_calls=tuple(calls), stop="tool_calls")
