"""Progress notifications that count upstream requests, not seconds.

A tool opens a `tracked` block with the request's context; the code that actually
talks to an upstream calls `upstream_call` after each request it made, and
`expect` when it learns how many it is about to make. Nothing here reads a clock.

The block is a context variable rather than a parameter threaded through every
client, because the clients are shared with the dashboard and with composite tools
that have no request of their own. With no block open, or no progress token on the
request, every call here is a no-op: no token, no notifications, no error.

A composite that calls a tracked tool directly does not pass a context, so the
inner tool opens no block of its own and its requests are not double-counted.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Protocol

from .telemetry import get_logger

_log = get_logger(__name__)


class Reporter(Protocol):
    """The part of the SDK's request context this module uses."""

    async def report_progress(
        self, progress: float, total: float | None = None, message: str | None = None
    ) -> None: ...


@dataclass(slots=True)
class _Tracker:
    reporter: Reporter
    total: int
    done: int = 0


_ACTIVE: ContextVar[_Tracker | None] = ContextVar("flightops_progress", default=None)


@asynccontextmanager
async def tracked(reporter: Reporter | Any | None, total: int = 0) -> AsyncIterator[None]:
    """Count upstream requests made inside the block and report them on `reporter`."""
    if reporter is None or _ACTIVE.get() is not None:
        yield
        return
    token = _ACTIVE.set(_Tracker(reporter=reporter, total=max(0, total)))
    try:
        yield
    finally:
        _ACTIVE.reset(token)


def expect(requests: int) -> None:
    """Add `requests` to the planned total, or take them off when a planned one is skipped."""
    tracker = _ACTIVE.get()
    if tracker is not None:
        tracker.total = max(tracker.done, tracker.total + requests)


async def upstream_call(message: str) -> None:
    """One upstream request has completed, successfully or not."""
    tracker = _ACTIVE.get()
    if tracker is None:
        return
    tracker.done += 1
    # A total is a promise; never report having done more than it says.
    tracker.total = max(tracker.total, tracker.done)
    try:
        await tracker.reporter.report_progress(tracker.done, tracker.total, message)
    except Exception as exc:  # noqa: BLE001 - a lost notification must not fail the tool
        _log.warning("progress_not_sent", error_class=type(exc).__name__)
