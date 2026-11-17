"""The local credit ledger: a rolling hour of OpenSky spend, and the guard on it.

Its own module, not a method on the client, so the arithmetic can be tested and
mutated without an HTTP stack around it (docs/QUALITY.md). The client charges
before each request; this module decides whether that charge is allowed.

The rules, each one a property test in `tests/test_properties_credits.py`:

* Spend inside the window never exceeds the budget. A charge that would cross
  it is refused whole, never partly applied.
* Spend is never negative and a charge is at least one credit. Upstream costs
  are 1 to 4; anything else is a caller bug, and it raises `ValueError` rather
  than quietly crediting the ledger.
* A refusal is a value, `local_budget_exhausted` with the numbers, which the
  tool layer returns to the model as data.
* An entry leaves the window once it is strictly older than one hour, so a
  charge made exactly an hour ago still counts.
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable

from .errors import ToolFailure, budget_exhausted

WINDOW_S = 3600.0


class CreditLedger:
    """Credits charged in the last hour, against a per-hour budget."""

    def __init__(
        self,
        budget_per_hour: int,
        *,
        clock: Callable[[], float] = time.time,
        window_s: float = WINDOW_S,
    ) -> None:
        if budget_per_hour < 0:
            raise ValueError("budget_per_hour must not be negative")
        self._budget = budget_per_hour
        self._clock = clock
        self._window_s = window_s
        self._spend: deque[tuple[float, int]] = deque()
        self.total = 0

    @property
    def budget(self) -> int:
        return self._budget

    def spent(self) -> int:
        """Credits charged inside the window. Forgets what has left it."""
        cutoff = self._clock() - self._window_s
        while self._spend and self._spend[0][0] < cutoff:
            self._spend.popleft()
        return sum(cost for _, cost in self._spend)

    def remaining(self) -> int:
        return self._budget - self.spent()

    def refusal(self, credits: int) -> ToolFailure | None:
        """Why `credits` cannot be charged now, or None if it can."""
        if credits < 1:
            raise ValueError(f"a charge is at least one credit, not {credits}")
        spent = self.spent()
        if spent + credits > self._budget:
            return budget_exhausted(spent, self._budget)
        return None

    def charge(self, credits: int) -> None:
        """Record `credits` now, or raise the refusal. Nothing is recorded on a refusal."""
        refused = self.refusal(credits)
        if refused is not None:
            raise refused
        self._spend.append((self._clock(), credits))
        self.total += credits
