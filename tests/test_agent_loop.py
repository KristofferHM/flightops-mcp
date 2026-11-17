"""The loop: plan, tools, reflect, and every way a run can end.

The loop is driven here with a scripted planner and scripted tools, so each limit
and each failure can be hit exactly. The planner and the tools are fakes; the
loop, the ledger and the transcript are the real ones. A run that exceeds its
budget stops, says which limit, and leaves nothing it planned uncounted.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import pytest

from agent.budget import Budget
from agent.loop import classify, run_agent
from agent.provider import Observation, ProviderFailure, Task, ToolCall, ToolSpec, Turn, Usage
from agent.tools import ToolOutcome, ToolsUnavailable
from agent.transcript import Transcript, loads

OK = {"ok": True, "unknowns": []}


class ScriptedPlanner:
    """Plays back a fixed list of turns and remembers what it was shown."""

    name = "offline"
    model = "offline"

    def __init__(self, *turns: Turn | ProviderFailure) -> None:
        self._turns = list(turns)
        self.seen: list[list[Observation]] = []
        self.task: Task | None = None

    async def begin(self, task: Task) -> None:
        self.task = task

    async def turn(self, observations: Sequence[Observation]) -> Turn:
        self.seen.append(list(observations))
        planned = self._turns.pop(0)
        if isinstance(planned, ProviderFailure):
            raise planned
        return planned


class ScriptedTools:
    """Answers each tool with a fixed payload and counts what was actually dispatched."""

    def __init__(self, payloads: dict[str, dict[str, Any]] | None = None) -> None:
        self._payloads = payloads or {}
        self.dispatched: list[ToolCall] = []
        self.unavailable = False

    async def list_tools(self) -> tuple[ToolSpec, ...]:
        if self.unavailable:
            raise ToolsUnavailable("ConnectError")
        return (ToolSpec(name="airport_weather", description="", input_schema={}),)

    async def call(self, call: ToolCall) -> ToolOutcome:
        self.dispatched.append(call)
        return ToolOutcome(self._payloads.get(call.name, OK))


def calls(*names: str, usage: Usage | None = None) -> Turn:
    return Turn(
        tool_calls=tuple(
            ToolCall(id=f"c-{name}-{n}", name=name, arguments={}) for n, name in enumerate(names)
        ),
        usage=usage or Usage(),
        stop="tool_calls",
    )


def answer(text: str = "done", usage: Usage | None = None) -> Turn:
    return Turn(text=text, usage=usage or Usage(), stop="answer")


async def run(planner: ScriptedPlanner, tools: ScriptedTools, budget: Budget) -> Transcript:
    ticks = iter(range(1_000))
    return await run_agent(
        scenario_id="budget-test",
        scenario_sha256="0" * 64,
        question="?",
        provider=planner,
        tools=tools,
        budget=budget,
        clock=lambda: 1_800_000_000.0 + next(ticks),
    )


# --- each limit stops the run, and says so ---------------------------------------
async def test_a_run_inside_its_budget_finishes_and_reports_what_it_spent() -> None:
    planner = ScriptedPlanner(
        calls("airport_weather", usage=Usage(120, 30)), answer("clear", Usage(200, 40))
    )
    tools = ScriptedTools({"airport_weather": {"ok": True, "bookkeeping": {"credits_spent": 0}}})

    transcript = await run(planner, tools, Budget())

    assert transcript.outcome == {
        "status": "completed",
        "reason": "answered",
        "detail": "",
        "answer": "clear",
        "unexecuted_tool_calls": 0,
    }
    assert transcript.budget["spent"] == {
        "turns": 2,
        "tool_calls": 1,
        "input_tokens": 320,
        "output_tokens": 70,
        "upstream_credits": 0,
    }
    assert transcript.budget["exhausted"] is None


async def test_exceeding_the_token_budget_stops_before_the_tools_that_turn_wanted() -> None:
    planner = ScriptedPlanner(
        calls("airport_weather", usage=Usage(400, 100)),
        calls("airport_weather", "airport_movements", usage=Usage(900, 200)),
        answer("never reached"),
    )
    tools = ScriptedTools()

    transcript = await run(planner, tools, Budget(model_tokens=1_000))

    assert transcript.outcome["status"] == "stopped"
    assert transcript.outcome["reason"] == "budget_exhausted"
    assert transcript.outcome["detail"] == (
        "agent budget exhausted (model_tokens): the model has used 1600 tokens, over the "
        "limit of 1000"
    )
    assert transcript.budget["exhausted"] == "model_tokens"
    assert transcript.outcome["unexecuted_tool_calls"] == 2, "the dropped calls are counted"
    assert len(tools.dispatched) == 1, "only the first turn's call ran"
    assert transcript.tool_sequence() == ["airport_weather"]
    # The overshoot is on the record: tokens are only known after they are spent.
    assert (
        transcript.budget["spent"]["input_tokens"] + transcript.budget["spent"]["output_tokens"]
        > transcript.budget["limits"]["model_tokens"]
    )


async def test_an_answer_that_broke_the_token_budget_is_kept_and_the_run_is_still_stopped() -> None:
    planner = ScriptedPlanner(answer("the long answer", Usage(900, 200)))

    transcript = await run(planner, ScriptedTools(), Budget(model_tokens=1_000))

    assert transcript.outcome["status"] == "stopped"
    assert transcript.outcome["reason"] == "budget_exhausted"
    assert transcript.outcome["answer"] == "the long answer", "stopped is not the same as erased"


async def test_exceeding_the_tool_call_budget_stops_before_the_call_not_after() -> None:
    planner = ScriptedPlanner(calls("a", "b", "c", "d"), answer("never reached"))
    tools = ScriptedTools()

    transcript = await run(planner, tools, Budget(tool_calls=2))

    assert [call.name for call in tools.dispatched] == ["a", "b"]
    assert transcript.outcome["reason"] == "budget_exhausted"
    assert transcript.outcome["detail"] == (
        "agent budget exhausted (tool_calls): calling c would be tool call 3, over the limit of 2"
    )
    assert transcript.outcome["unexecuted_tool_calls"] == 2
    assert transcript.budget["spent"]["tool_calls"] == 2
    assert transcript.budget["exhausted"] == "tool_calls"


async def test_a_run_that_never_finishes_is_stopped_by_its_turn_budget() -> None:
    planner = ScriptedPlanner(*[calls("a") for _ in range(10)])
    tools = ScriptedTools()

    transcript = await run(planner, tools, Budget(turns=3, tool_calls=100))

    assert transcript.outcome["reason"] == "budget_exhausted"
    assert "used all 3 model turns without finishing" in transcript.outcome["detail"]
    assert transcript.budget["exhausted"] == "turns"
    assert len(planner.seen) == 3, "the planner is not asked for a fourth turn"
    assert len(tools.dispatched) == 3


async def test_exceeding_the_credit_budget_stops_the_run_and_reports_the_overshoot() -> None:
    """Credits are the server's to price. The agent learns the cost from the result."""
    planner = ScriptedPlanner(calls("airport_movements", "airport_movements", "airport_movements"))
    tools = ScriptedTools({"airport_movements": {"ok": True, "bookkeeping": {"credits_spent": 8}}})

    transcript = await run(planner, tools, Budget(upstream_credits=10))

    assert len(tools.dispatched) == 2, "the third call is never made"
    assert transcript.outcome["reason"] == "budget_exhausted"
    assert transcript.outcome["detail"] == (
        "agent budget exhausted (upstream_credits): this run has caused 16 upstream credits "
        "to be spent, over its limit of 10"
    )
    assert transcript.outcome["unexecuted_tool_calls"] == 1
    assert transcript.budget["spent"]["upstream_credits"] == 16
    assert transcript.budget["exhausted"] == "upstream_credits"


# --- two budgets, never one ------------------------------------------------------
async def test_the_servers_exhausted_credit_ledger_is_a_tool_error_not_the_agents_budget() -> None:
    """The operator's ledger ran dry. That is news from a tool, and this run's budget is intact."""
    refused = {
        "ok": False,
        "error": {
            "code": "local_budget_exhausted",
            "message": "The local credit guard stopped this call (400/400 credits used this hour).",
            "details": {"credits_spent_this_hour": 400, "budget": 400},
        },
    }
    planner = ScriptedPlanner(calls("airport_live_traffic"), answer("traffic is unknown"))
    tools = ScriptedTools({"airport_live_traffic": refused})

    transcript = await run(planner, tools, Budget(upstream_credits=5))

    assert transcript.steps[0]["result"] == {
        "class": "error",
        "code": "local_budget_exhausted",
        "unknowns": 0,
        "replayed": None,
    }
    assert transcript.budget["exhausted"] is None, "the agent's own budget did not run out"
    assert transcript.budget["spent"]["upstream_credits"] == 0
    assert transcript.outcome["status"] == "completed", "the planner got to react to the refusal"
    assert planner.seen[1][0].code == "local_budget_exhausted"


# --- every other way a run ends is also an outcome --------------------------------
async def test_a_planner_that_fails_ends_the_run_with_its_reason() -> None:
    planner = ScriptedPlanner(
        calls("a"), ProviderFailure("rate_limited", "the model API rate limit was reached")
    )

    transcript = await run(planner, ScriptedTools(), Budget())

    assert transcript.outcome["status"] == "stopped"
    assert transcript.outcome["reason"] == "provider_error"
    assert transcript.outcome["detail"] == "rate_limited: the model API rate limit was reached"
    assert transcript.tool_sequence() == ["a"], "what happened before the failure is kept"


@pytest.mark.parametrize(
    ("stop", "reason"), [("refusal", "model_refused"), ("truncated", "model_truncated")]
)
async def test_a_refused_or_truncated_turn_stops_the_run_without_running_its_tools(
    stop: Any, reason: str
) -> None:
    turn = Turn(
        text="partial",
        tool_calls=(ToolCall(id="c1", name="propose_rebooking", arguments={}),),
        stop=stop,
    )
    tools = ScriptedTools()

    transcript = await run(ScriptedPlanner(turn), tools, Budget())

    assert transcript.outcome["reason"] == reason
    assert transcript.outcome["answer"] == "partial"
    assert transcript.outcome["unexecuted_tool_calls"] == 1
    assert tools.dispatched == [], "a call from a cut-off turn may be half an argument list"


async def test_a_server_that_cannot_list_its_tools_ends_the_run_as_data() -> None:
    tools = ScriptedTools()
    tools.unavailable = True
    planner = ScriptedPlanner(answer())

    transcript = await run(planner, tools, Budget())

    assert transcript.outcome["reason"] == "server_unavailable"
    assert planner.seen == [], "nothing is planned against a server that did not answer"
    assert loads(transcript.to_bytes()) == transcript, "a failed run is still a valid transcript"


async def test_the_planner_is_shown_tool_results_and_the_offered_tools() -> None:
    planner = ScriptedPlanner(calls("airport_weather"), answer())
    tools = ScriptedTools({"airport_weather": {"ok": True, "unknowns": ["No METAR returned."]}})

    transcript = await run(planner, tools, Budget())

    assert planner.task is not None
    assert [tool.name for tool in planner.task.tools] == ["airport_weather"]
    assert planner.seen[0] == [], "the first turn is the plan: nothing has been observed yet"
    assert planner.seen[1][0].payload["unknowns"] == ["No METAR returned."]
    assert transcript.steps[0]["result"]["unknowns"] == 1
    assert [step["turn"] for step in transcript.steps] == [1]


# --- result classes ------------------------------------------------------------------
@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"ok": True}, ("ok", None, 0, None)),
        ({"ok": True, "unknowns": ["a", "b"]}, ("ok", None, 2, None)),
        ({"ok": True, "assessment": {"unknowns": ["a"]}}, ("ok", None, 1, None)),
        ({"ok": True, "idempotency": {"key": "rbk_x", "replayed": True}}, ("ok", None, 0, True)),
        ({"ok": False, "error": {"code": "not_found"}}, ("error", "not_found", 0, None)),
        ({"ok": False}, ("error", "unspecified", 0, None)),
        ({"ok": False, "error": "boom"}, ("error", "unspecified", 0, None)),
        ({"ok": "yes"}, ("error", "unspecified", 0, None)),
    ],
)
def test_a_payload_is_reduced_to_how_it_ended(
    payload: dict[str, Any], expected: tuple[Any, ...]
) -> None:
    result = classify(payload)

    assert (result["class"], result["code"], result["unknowns"], result["replayed"]) == expected
