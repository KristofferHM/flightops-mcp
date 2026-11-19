"""The `Date` header a real upstream sends, for fixtures that answer in-process.

uvicorn, NOAA, OpenSky and Schiphol all stamp every response with `Date`, and the
server measures data ages against it. `httpx.MockTransport` stamps nothing, so a
fixture world wraps its handlers in this to send the clock of the moment it
describes. A handler that sets its own `Date` keeps it; a test of a response with
no `Date` leaves its handler unwrapped.
"""

from __future__ import annotations

from collections.abc import Callable
from email.utils import formatdate

import httpx

Handler = Callable[[httpx.Request], httpx.Response]


def http_date(epoch: float) -> str:
    """`epoch` as an RFC 9110 HTTP-date, the form a `Date` header takes."""
    return formatdate(epoch, usegmt=True)


def dated(handler: Handler, clock: Callable[[], float]) -> Handler:
    """`handler`, with a `Date` of `clock()` on every response that has none."""

    def stamp(request: httpx.Request) -> httpx.Response:
        response = handler(request)
        response.headers.setdefault("Date", http_date(clock()))
        return response

    return stamp


def dated_transport(handler: Handler, clock: Callable[[], float]) -> httpx.MockTransport:
    return httpx.MockTransport(dated(handler, clock))
