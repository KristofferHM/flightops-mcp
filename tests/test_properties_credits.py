"""Properties of the credit ledger and of what a bounding box costs.

The ledger is checked against a model: a plain list of (time, credits) that
says what the spend in the window must be. Every step of a random schedule of
charges and clock moves is compared with it, so a mistake in the window edge,
the refusal rule or the bookkeeping shows up as a disagreement, not as a number
somebody has to recognise as wrong.
"""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from flightops_mcp.credits import WINDOW_S, CreditLedger
from flightops_mcp.errors import ToolFailure
from flightops_mcp.opensky import BBox

from .conftest import FakeClock

# A step either moves the clock forward (up to two windows) or asks for a charge of
# 1 to 6 credits; upstream costs are 1 to 4, and 5 and 6 probe "bigger than asked".
steps = st.lists(
    st.one_of(
        st.tuples(st.just("advance"), st.floats(0, 2 * WINDOW_S, allow_nan=False)),
        st.tuples(st.just("charge"), st.integers(1, 6)),
    ),
    max_size=60,
)


def model_spent(entries: list[tuple[float, int]], now: float) -> int:
    return sum(credits for at, credits in entries if at >= now - WINDOW_S)


@given(budget=st.integers(0, 40), schedule=steps)
def test_the_ledger_agrees_with_a_model_of_the_window(
    budget: int, schedule: list[tuple[str, float]]
) -> None:
    clock = FakeClock()
    ledger = CreditLedger(budget, clock=clock)
    entries: list[tuple[float, int]] = []
    total = 0

    for action, value in schedule:
        if action == "advance":
            clock.advance(value)
        else:
            credits = int(value)
            before = model_spent(entries, clock())
            expected_refusal = before + credits > budget
            refusal = ledger.refusal(credits)
            assert (refusal is not None) == expected_refusal
            if expected_refusal:
                with pytest.raises(ToolFailure):
                    ledger.charge(credits)
            else:
                ledger.charge(credits)
                entries.append((clock(), credits))
                total += credits

        spent = ledger.spent()
        assert spent == model_spent(entries, clock())
        assert 0 <= spent <= budget
        assert ledger.remaining() == budget - spent
        assert ledger.remaining() >= 0
        assert ledger.total == total


@given(budget=st.integers(0, 40), spent=st.integers(0, 40), credits=st.integers(1, 10))
def test_a_refusal_is_a_stated_reason_with_the_numbers(
    budget: int, spent: int, credits: int
) -> None:
    clock = FakeClock()
    ledger = CreditLedger(budget, clock=clock)
    for _ in range(min(spent, budget)):
        ledger.charge(1)
    already = ledger.spent()

    refusal = ledger.refusal(credits)

    if already + credits <= budget:
        assert refusal is None
        return
    assert refusal is not None
    assert refusal.code == "local_budget_exhausted"
    assert refusal.details == {"credits_spent_this_hour": already, "budget": budget}
    payload = refusal.to_payload()
    assert payload["ok"] is False
    assert payload["error"]["hint"]
    # Asking again changes nothing: a refusal is not a charge.
    assert ledger.spent() == already
    assert ledger.total == already


@given(credits=st.integers(-1_000, 0))
def test_a_charge_of_less_than_one_credit_is_a_caller_bug(credits: int) -> None:
    ledger = CreditLedger(10, clock=FakeClock())
    with pytest.raises(ValueError, match="at least one credit"):
        ledger.charge(credits)
    assert (ledger.spent(), ledger.total) == (0, 0)


@given(budget=st.integers(-1_000, -1))
def test_a_negative_budget_is_refused_at_construction(budget: int) -> None:
    with pytest.raises(ValueError, match="must not be negative"):
        CreditLedger(budget, clock=FakeClock())


@given(credits=st.integers(1, 4), past=st.floats(0, WINDOW_S, allow_nan=False))
def test_a_charge_counts_for_exactly_one_window(credits: int, past: float) -> None:
    """Inside the window, including its edge, it counts; a moment later it does not."""
    clock = FakeClock()
    ledger = CreditLedger(4, clock=clock)
    ledger.charge(credits)

    clock.advance(past)
    assert ledger.spent() == credits

    clock.value = FakeClock().value + WINDOW_S + 0.001
    assert ledger.spent() == 0
    assert ledger.total == credits


# --- what a box costs ---------------------------------------------------------
latitudes = st.floats(-89.0, 89.0, allow_nan=False)
longitudes = st.floats(-179.0, 179.0, allow_nan=False)
radii = st.floats(0.1, 600.0, allow_nan=False)


@given(lat=latitudes, lon=longitudes, radius=radii)
def test_a_box_around_any_point_is_valid_and_costs_one_to_four(
    lat: float, lon: float, radius: float
) -> None:
    box = BBox.around(lat, lon, radius)
    assert box.lamin < box.lamax
    assert box.lomin < box.lomax
    assert 1 <= box.credit_cost <= 4


@given(lat=latitudes, lon=longitudes, small=radii, large=radii)
def test_a_bigger_radius_never_costs_less(
    lat: float, lon: float, small: float, large: float
) -> None:
    small, large = sorted((small, large))
    assert BBox.around(lat, lon, small).credit_cost <= BBox.around(lat, lon, large).credit_cost


@given(
    lamin=st.floats(-90, 89, allow_nan=False),
    lomin=st.floats(-180, 179, allow_nan=False),
    height=st.floats(0.001, 90, allow_nan=False),
    width=st.floats(0.001, 180, allow_nan=False),
)
def test_the_cost_follows_the_documented_table(
    lamin: float, lomin: float, height: float, width: float
) -> None:
    box = BBox(lamin, lomin, min(90.0, lamin + height), min(180.0, lomin + width))
    area = box.area_sq_deg
    expected = 1 if area <= 25 else 2 if area <= 100 else 3 if area <= 400 else 4
    assert box.credit_cost == expected
