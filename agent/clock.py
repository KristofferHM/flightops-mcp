"""The run's clock. Injected, so an offline run reads no wall clock at all."""

from __future__ import annotations


class LogicalClock:
    """Starts where the scenario says and moves one second each time it is read.

    Time in an offline run is a count of the things that happened, not a
    measurement. Two runs of one scenario read the clock the same number of
    times and therefore record the same instants.
    """

    def __init__(self, start: float, *, tick_s: float = 1.0) -> None:
        self._now = start
        self._tick_s = tick_s

    def __call__(self) -> float:
        now = self._now
        self._now += self._tick_s
        return now
