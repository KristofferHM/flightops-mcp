"""A circuit breaker per upstream.

Why this exists: the retry policy in `http.py` is deliberately generous (three
attempts with backoff), which is right for a blip and wrong for an outage. When a
provider is down, every request spends the full retry budget in sleeps while the
agent waits — and the triage tool fans out to three upstreams, so the waits
multiply. A breaker turns "down" into "fail in microseconds, and say so".

The contract with the caller is the same as everywhere else in this project: an
open circuit raises a `ToolFailure` whose `code` and `retry_after_s` are data the
model can reason about, not a hang.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal

from .errors import ToolFailure, upstream_circuit_open
from .telemetry import CIRCUIT_STATE, get_logger

BreakerState = Literal["closed", "open", "half_open"]


@dataclass
class CircuitBreaker:
    """Consecutive-failure breaker with a single half-open probe.

    `closed`      — calls pass, failures are counted.
    `open`        — calls fail fast with `retry_after_s`; nothing reaches upstream.
    `half_open`   — one probe is allowed through after the recovery window; its
                    result decides between `closed` and `open`.
    """

    name: str
    failure_threshold: int = 5
    recovery_timeout_s: float = 30.0
    clock: Callable[[], float] = time.monotonic
    state: BreakerState = "closed"
    consecutive_failures: int = 0
    opened_at: float | None = None
    _log: object = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self._log is None:
            self._log = get_logger(__name__)
        self._publish()

    # --- caller-facing ------------------------------------------------------
    def before_call(self) -> None:
        """Raise when the circuit is open; allow a probe when the window has passed."""
        if self.state == "open":
            assert self.opened_at is not None
            elapsed = self.clock() - self.opened_at
            if elapsed < self.recovery_timeout_s:
                raise upstream_circuit_open(
                    self.name, retry_after_s=self.recovery_timeout_s - elapsed
                )
            self._transition("half_open")

    def record_success(self) -> None:
        if self.state != "closed" or self.consecutive_failures:
            self.consecutive_failures = 0
            self._transition("closed")

    def record_failure(self) -> None:
        self.consecutive_failures += 1
        if self.state == "half_open":
            self._transition("open")
            return
        if self.state == "closed" and self.consecutive_failures >= self.failure_threshold:
            self._transition("open")

    # --- observability ------------------------------------------------------
    def snapshot(self) -> dict[str, object]:
        retry_after: float | None = None
        if self.state == "open" and self.opened_at is not None:
            remaining = self.recovery_timeout_s - (self.clock() - self.opened_at)
            retry_after = round(max(0.0, remaining), 3)
        return {
            "name": self.name,
            "state": self.state,
            "consecutive_failures": self.consecutive_failures,
            "retry_after_s": retry_after,
        }

    def reset(self) -> None:
        self.consecutive_failures = 0
        self.state = "closed"
        self.opened_at = None
        self._publish()

    def _transition(self, new_state: BreakerState) -> None:
        previous = self.state
        self.state = new_state
        if new_state == "open":
            self.opened_at = self.clock()
        elif new_state == "closed":
            self.opened_at = None
        self._publish()
        if previous != new_state:
            log = self._log
            log.info(  # type: ignore[attr-defined]
                "circuit_state_changed",
                breaker=self.name,
                previous=previous,
                state=new_state,
                consecutive_failures=self.consecutive_failures,
            )

    def _publish(self) -> None:
        CIRCUIT_STATE.labels(upstream=self.name).set(1.0 if self.state == "open" else 0.0)


class BreakerRegistry:
    """One breaker per upstream name, shared by every client instance in the process."""

    def __init__(self, *, failure_threshold: int, recovery_timeout_s: float) -> None:
        self.failure_threshold = failure_threshold
        self.recovery_timeout_s = recovery_timeout_s
        self._breakers: dict[str, CircuitBreaker] = {}

    def get(self, name: str, *, clock: Callable[[], float] = time.monotonic) -> CircuitBreaker:
        existing = self._breakers.get(name)
        if existing is not None:
            return existing
        created = CircuitBreaker(
            name=name,
            failure_threshold=self.failure_threshold,
            recovery_timeout_s=self.recovery_timeout_s,
            clock=clock,
        )
        self._breakers[name] = created
        return created

    def snapshots(self) -> list[dict[str, object]]:
        return [breaker.snapshot() for breaker in self._breakers.values()]

    def reset(self) -> None:
        self._breakers.clear()


class _RegistryHolder:
    """Registry held in a class rather than a module-level `global` statement."""

    registry: BreakerRegistry


_holder = _RegistryHolder()


def configure_breakers(*, failure_threshold: int, recovery_timeout_s: float) -> None:
    _holder.registry = BreakerRegistry(
        failure_threshold=failure_threshold, recovery_timeout_s=recovery_timeout_s
    )


def breakers() -> BreakerRegistry:
    if not hasattr(_holder, "registry"):
        _holder.registry = BreakerRegistry(failure_threshold=5, recovery_timeout_s=30.0)
    return _holder.registry


def reset_breakers() -> None:
    breakers().reset()


def is_circuit_open(exc: BaseException) -> bool:
    return isinstance(exc, ToolFailure) and exc.code == "upstream_circuit_open"
