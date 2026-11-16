"""The agent's own budget: what one run may spend, and why it stopped.

There are two budgets in this system and they are not the same money:

* **The server's credit ledger** (`credit_budget_per_hour`, in `opensky.py`) is
  the operator's. Every caller draws on it and it protects the upstream daily
  allowance. When it is empty a tool answers `local_budget_exhausted`, which
  reaches this loop as an ordinary tool error.
* **This budget** is one run's allowance: model turns, tool calls, model tokens,
  and the upstream credits the run is willing to be the cause of.

They are kept apart on purpose. This ledger counts only what it observed in this
run. It never reads the server's ledger to decide whether it may continue, and it
has no way to top itself up from it. A run that reaches a limit stops and says
which limit, with the numbers; it does not drop the calls it could not afford
and carry on as if they had never been planned.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class Budget:
    """Limits for one run. Each is a hard stop, not a target."""

    turns: int = 8
    tool_calls: int = 12
    model_tokens: int = 60_000
    upstream_credits: int = 40

    def as_dict(self) -> dict[str, int]:
        return {
            "turns": self.turns,
            "tool_calls": self.tool_calls,
            "model_tokens": self.model_tokens,
            "upstream_credits": self.upstream_credits,
        }


@dataclass(frozen=True, slots=True)
class Exhausted:
    """Why a run stopped. `dimension` is stable; `detail` is for a person."""

    dimension: str
    detail: str


class BudgetLedger:
    def __init__(self, budget: Budget) -> None:
        self.budget = budget
        self.turns = 0
        self.tool_calls = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.upstream_credits = 0
        self.exhausted: Exhausted | None = None

    @property
    def model_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def before_turn(self) -> Exhausted | None:
        """Refuse a model turn the run cannot afford. Checked before the model is asked."""
        if self.turns >= self.budget.turns:
            return self._stop(
                "turns", f"the run used all {self.budget.turns} model turns without finishing"
            )
        return None

    def after_turn(self, input_tokens: int, output_tokens: int) -> Exhausted | None:
        """Account for a turn. Tokens are only known afterwards, so this can overshoot.

        The overshoot is reported, not hidden: `spent` exceeds `limits` in the
        transcript, and the tool calls that turn asked for are not executed.
        """
        self.turns += 1
        self.input_tokens += max(0, input_tokens)
        self.output_tokens += max(0, output_tokens)
        if self.model_tokens > self.budget.model_tokens:
            return self._stop(
                "model_tokens",
                f"the model has used {self.model_tokens} tokens, over the limit of "
                f"{self.budget.model_tokens}",
            )
        return None

    def before_tool_call(self, tool: str) -> Exhausted | None:
        if self.tool_calls >= self.budget.tool_calls:
            return self._stop(
                "tool_calls",
                f"calling {tool} would be tool call {self.tool_calls + 1}, over the limit of "
                f"{self.budget.tool_calls}",
            )
        return None

    def after_tool_call(self, payload: dict[str, Any]) -> Exhausted | None:
        """Count the call and the upstream credits the tool says this call spent."""
        self.tool_calls += 1
        self.upstream_credits += credits_reported(payload)
        if self.upstream_credits > self.budget.upstream_credits:
            return self._stop(
                "upstream_credits",
                f"this run has caused {self.upstream_credits} upstream credits to be spent, "
                f"over its limit of {self.budget.upstream_credits}",
            )
        return None

    def snapshot(self) -> dict[str, Any]:
        return {
            "limits": self.budget.as_dict(),
            "spent": {
                "turns": self.turns,
                "tool_calls": self.tool_calls,
                "input_tokens": self.input_tokens,
                "output_tokens": self.output_tokens,
                "upstream_credits": self.upstream_credits,
            },
            "exhausted": self.exhausted.dimension if self.exhausted else None,
        }

    def _stop(self, dimension: str, detail: str) -> Exhausted:
        self.exhausted = Exhausted(dimension=dimension, detail=detail)
        return self.exhausted


def credits_reported(payload: dict[str, Any]) -> int:
    """Upstream credits a tool result says this one call spent. Absent means none.

    `bookkeeping.credits_spent` is per call. The fields beside it that describe
    the server's ledger (`credits_remaining_upstream`, the hourly totals in
    `upstream_status`) are deliberately not read: they are not this run's.
    """
    bookkeeping = payload.get("bookkeeping")
    spent = bookkeeping.get("credits_spent") if isinstance(bookkeeping, dict) else None
    return spent if isinstance(spent, int) and not isinstance(spent, bool) and spent > 0 else 0
