"""The agent's budget ledger: what one run may spend, and that it borrows nothing.

The ledger is tested on its own here. How the loop behaves when a limit is
reached is in `test_agent_loop.py`, and end to end in `test_agent_offline.py`.
"""

from __future__ import annotations

from typing import Any

import pytest

from agent.budget import Budget, BudgetLedger, credits_reported

OK = {"ok": True, "unknowns": []}


# --- each limit, at the ledger ------------------------------------------------------
def test_a_fresh_ledger_allows_work_and_reports_nothing_spent() -> None:
    ledger = BudgetLedger(Budget())

    assert ledger.before_turn() is None
    assert ledger.before_tool_call("airport_weather") is None
    assert ledger.exhausted is None
    assert ledger.snapshot() == {
        "limits": {"turns": 8, "tool_calls": 12, "model_tokens": 60_000, "upstream_credits": 40},
        "spent": {
            "turns": 0,
            "tool_calls": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "upstream_credits": 0,
        },
        "exhausted": None,
    }


def test_the_turn_limit_refuses_the_turn_after_the_last_one() -> None:
    ledger = BudgetLedger(Budget(turns=2))

    assert ledger.before_turn() is None
    assert ledger.after_turn(10, 5) is None
    assert ledger.before_turn() is None
    assert ledger.after_turn(10, 5) is None
    stopped = ledger.before_turn()

    assert stopped is not None
    assert stopped.dimension == "turns"
    assert stopped.detail == "the run used all 2 model turns without finishing"
    assert ledger.snapshot()["exhausted"] == "turns"


def test_the_token_limit_is_inclusive_and_an_overshoot_is_reported_not_hidden() -> None:
    ledger = BudgetLedger(Budget(model_tokens=100))

    assert ledger.after_turn(60, 40) is None, "exactly at the limit is inside it"
    stopped = ledger.after_turn(1, 0)

    assert stopped is not None
    assert stopped.dimension == "model_tokens"
    assert stopped.detail == "the model has used 101 tokens, over the limit of 100"
    assert ledger.snapshot()["spent"]["input_tokens"] == 61


def test_the_tool_call_limit_stops_before_the_call_and_names_it() -> None:
    ledger = BudgetLedger(Budget(tool_calls=1))

    assert ledger.before_tool_call("flight_passenger_impact") is None
    assert ledger.after_tool_call(OK) is None
    stopped = ledger.before_tool_call("propose_rebooking")

    assert stopped is not None
    assert stopped.dimension == "tool_calls"
    assert stopped.detail == ("calling propose_rebooking would be tool call 2, over the limit of 1")
    assert ledger.tool_calls == 1, "the refused call was not counted as made"


def test_the_credit_limit_counts_what_each_call_reports() -> None:
    ledger = BudgetLedger(Budget(upstream_credits=5))
    movements = {"ok": True, "bookkeeping": {"credits_spent": 4}}

    assert ledger.after_tool_call(movements) is None
    stopped = ledger.after_tool_call(movements)

    assert stopped is not None
    assert stopped.dimension == "upstream_credits"
    assert stopped.detail == (
        "this run has caused 8 upstream credits to be spent, over its limit of 5"
    )


# --- two budgets, never one ---------------------------------------------------------
def test_the_agent_counts_only_what_one_call_spent_never_the_servers_ledger() -> None:
    """`upstream_status` reports the server's totals. They are not this run's spend."""
    status = {
        "ok": True,
        "budget": {
            "credits_spent_this_hour": 399,
            "credits_spent_this_process": 5_000,
            "local_budget_per_hour": 400,
            "upstream_remaining_last_seen": 3_601,
        },
    }
    traffic = {"ok": True, "bookkeeping": {"credits_spent": 4, "credits_remaining_upstream": 9}}

    assert credits_reported(status) == 0
    assert credits_reported(traffic) == 4


@pytest.mark.parametrize("spent", [None, "4", -3, True, 2.5, {"n": 1}])
def test_a_credit_count_that_is_not_a_count_is_not_spent(spent: Any) -> None:
    assert credits_reported({"ok": True, "bookkeeping": {"credits_spent": spent}}) == 0
    assert credits_reported({"ok": True, "bookkeeping": spent}) == 0


def test_spending_only_goes_one_way() -> None:
    """A negative report from a planner or a tool does not refund anything."""
    ledger = BudgetLedger(Budget(tool_calls=2, model_tokens=100, upstream_credits=5))

    assert ledger.after_turn(60, 30) is None
    assert ledger.after_turn(-50, -50) is None
    assert ledger.model_tokens == 90, "a negative usage report is not a credit"
    assert ledger.after_tool_call({"ok": True, "bookkeeping": {"credits_spent": 4}}) is None
    assert ledger.after_tool_call({"ok": True, "bookkeeping": {"credits_spent": -4}}) is None
    assert ledger.upstream_credits == 4
    stopped = ledger.before_tool_call("b")
    assert stopped is not None and stopped.dimension == "tool_calls"
    assert ledger.exhausted is stopped
