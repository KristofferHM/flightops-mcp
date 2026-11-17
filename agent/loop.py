"""The loop: plan, call tools, reflect, and write down what happened.

    plan      the provider's first turn, from the question alone
    tools     each call it asked for, dispatched to the server in order
    reflect   the provider's next turn, from what the tools answered

It repeats until the provider answers without asking for a tool, or the run's
budget says stop. Every way out produces a transcript: a run that could not
finish is a result, with a reason, and never an exception at the caller.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Final

from .budget import Budget, BudgetLedger, Exhausted
from .provider import Observation, Provider, ProviderFailure, Task, ToolCall, Turn
from .tools import ApprovalNote, Tools, ToolsUnavailable
from .transcript import Transcript, digest

SYSTEM: Final = """\
You are an airline operations assistant working through the tools of a governed server.

Work in three moves: plan which tools answer the question, call them, then reflect on \
what came back before you answer or call anything else.

Rules the server will hold you to:

1. Report what the tools returned. If a result lists `unknowns`, or a tool failed, say \
so. Missing data is never evidence that operations are normal.
2. A failed call returns `ok: false` with a `code` and a `hint`. Follow the hint. Do not \
repeat a call that was refused for a reason you cannot change.
3. `propose_rebooking` needs a human approval that you cannot give, see or influence. \
Call it once with the count `flight_passenger_impact` reported. If it comes back \
refused, declined or unapproved, tell the user and stop.
4. Never state that passengers were rebooked unless `propose_rebooking` returned \
`ok: true`.
"""


async def run_agent(
    *,
    scenario_id: str,
    scenario_sha256: str,
    question: str,
    provider: Provider,
    tools: Tools,
    budget: Budget,
    clock: Callable[[], float],
    system: str = SYSTEM,
) -> Transcript:
    """Run one scenario to its end and return the transcript. Does not raise on failure."""
    run = _Run(provider=provider, tools=tools, ledger=BudgetLedger(budget), clock=clock)
    started_at = _iso(clock())
    outcome = await run.drive(system, question)
    return Transcript.seal(
        {
            "scenario": {"id": scenario_id, "sha256": scenario_sha256},
            "backend": provider.name,
            "model": provider.model,
            "started_at": started_at,
            "steps": run.steps,
            "approvals": run.approvals,
            "budget": run.ledger.snapshot(),
            "outcome": outcome,
        }
    )


class _Run:
    def __init__(
        self, *, provider: Provider, tools: Tools, ledger: BudgetLedger, clock: Callable[[], float]
    ) -> None:
        self.provider = provider
        self.tools = tools
        self.ledger = ledger
        self.clock = clock
        self.steps: list[dict[str, Any]] = []
        self.approvals: list[dict[str, Any]] = []

    async def drive(self, system: str, question: str) -> dict[str, Any]:
        try:
            offered = await self.tools.list_tools()
        except ToolsUnavailable as exc:
            return _stopped("server_unavailable", f"the server's tools could not be listed: {exc}")
        await self.provider.begin(Task(system=system, question=question, tools=offered))

        observations: list[Observation] = []
        while True:
            planned = await self._plan(observations)
            if isinstance(planned, dict):
                return planned
            observations = []
            stopped = await self._act(planned, observations)
            if stopped is not None:
                return stopped

    async def _plan(self, observations: list[Observation]) -> Turn | dict[str, Any]:
        """Ask for the next turn. Returns it, or the outcome if the run ends here."""
        over = self.ledger.before_turn()
        if over:
            return _over_budget(over)
        try:
            turn = await self.provider.turn(observations)
        except ProviderFailure as exc:
            return _stopped("provider_error", f"{exc.code}: {exc.message}")
        over = self.ledger.after_turn(turn.usage.input_tokens, turn.usage.output_tokens)
        if over:
            # What the model said on the turn that broke the budget is kept: the
            # run is stopped, and nothing it produced is thrown away unseen.
            return _over_budget(over, unexecuted=len(turn.tool_calls), said=turn.text)
        return _ended(turn) or turn

    async def _act(self, turn: Turn, observations: list[Observation]) -> dict[str, Any] | None:
        """Make the turn's tool calls in order. Returns an outcome only if the budget ends it."""
        for position, call in enumerate(turn.tool_calls):
            # The calls a turn still wanted when the budget ran out are counted
            # in the outcome, not quietly dropped.
            remaining = len(turn.tool_calls) - position
            over = self.ledger.before_tool_call(call.name)
            if over:
                return _over_budget(over, unexecuted=remaining)
            over = await self._execute(call, observations)
            if over:
                return _over_budget(over, unexecuted=remaining - 1)
        return None

    async def _execute(self, call: ToolCall, observations: list[Observation]) -> Exhausted | None:
        """Dispatch one call, record the step, and report if it broke the budget."""
        outcome = await self.tools.call(call)
        payload = outcome.payload
        index = len(self.steps)
        self.steps.append(
            {
                "index": index,
                "turn": self.ledger.turns,
                "tool": call.name,
                "arguments": call.arguments,
                "result": classify(payload),
            }
        )
        if outcome.approval is not None:
            self.approvals.append(_approval_record(index, outcome.approval, payload))
        observations.append(Observation(call=call, payload=payload))
        return self.ledger.after_tool_call(payload)


def classify(payload: dict[str, Any]) -> dict[str, Any]:
    """The result class of a tool payload: how it ended, never what it contained."""
    ok = payload.get("ok", True) is True
    error = payload.get("error")
    code = error.get("code") if isinstance(error, dict) else None
    idempotency = payload.get("idempotency")
    replayed = idempotency.get("replayed") if isinstance(idempotency, dict) else None
    return {
        "class": "ok" if ok else "error",
        "code": None if ok else (code if isinstance(code, str) and code else "unspecified"),
        "unknowns": _unknowns(payload),
        "replayed": replayed if isinstance(replayed, bool) else None,
    }


def _unknowns(payload: dict[str, Any]) -> int:
    """How many things the tool said it does not know. Triage nests its list."""
    listed = payload.get("unknowns")
    if not isinstance(listed, list):
        assessment = payload.get("assessment")
        listed = assessment.get("unknowns") if isinstance(assessment, dict) else None
    return len(listed) if isinstance(listed, list) else 0


def _approval_record(step: int, note: ApprovalNote, payload: dict[str, Any]) -> dict[str, Any]:
    """Who decided, when, on which payload, and whether the server acted on it."""
    used = note.grant_presented and payload.get("ok") is True
    audit = payload.get("audit")
    confirmed = audit.get("approved_by") if used and isinstance(audit, dict) else None
    return {
        "step": step,
        "tool": note.tool,
        "proposal": note.proposal,
        "proposal_sha256": digest(note.proposal),
        "action": note.action,
        "approver": note.approver,
        # The run's clock, not the server's: when the answer came back to the loop.
        "decided_at": _iso(note.decided_at),
        "grant_presented": note.grant_presented,
        "used": used,
        # Who the server says approved, from its verification of the grant.
        "confirmed_approver": confirmed if isinstance(confirmed, str) else None,
    }


def _ended(turn: Turn) -> dict[str, Any] | None:
    """The outcome a turn ends the run with, or None if it asked for tools."""
    if turn.stop == "refusal":
        return _stopped("model_refused", "the model declined to continue", turn)
    if turn.stop == "truncated":
        return _stopped("model_truncated", "the model's answer was cut off", turn)
    if turn.tool_calls:
        return None
    return {
        "status": "completed",
        "reason": "answered",
        "detail": "",
        "answer": turn.text,
        "unexecuted_tool_calls": 0,
    }


def _over_budget(over: Exhausted, *, unexecuted: int = 0, said: str = "") -> dict[str, Any]:
    return {
        "status": "stopped",
        "reason": "budget_exhausted",
        "detail": f"agent budget exhausted ({over.dimension}): {over.detail}",
        "answer": said,
        "unexecuted_tool_calls": unexecuted,
    }


def _stopped(reason: str, detail: str, turn: Turn | None = None) -> dict[str, Any]:
    return {
        "status": "stopped",
        "reason": reason,
        "detail": detail,
        "answer": turn.text if turn else "",
        "unexecuted_tool_calls": len(turn.tool_calls) if turn else 0,
    }


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=UTC).isoformat()
