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


def airport_brief(params: Mapping[str, Any]) -> Generator[Calls, Sequence[Observation], str]:
    """Resolve an airport, read its weather, and say what is not known."""
    query = params["airport"]
    (resolved,) = yield [("resolve_airport", {"query": query, "limit": 1})]
    matches = resolved.payload.get("matches") if resolved.ok else None
    if not matches:
        return f"No airport matched {query!r} ({resolved.code}). Nothing was looked up."
    icao = matches[0]["icao"]
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


POLICIES: Final[dict[str, Policy]] = {
    "rebook_cohort": rebook_cohort,
    "airport_brief": airport_brief,
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
