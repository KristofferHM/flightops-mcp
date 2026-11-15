"""Runtime container.

One object owns the upstream clients, the reference database and the clock. It
is injected rather than imported-and-constructed inside each tool, which is what
lets the test suite swap in a mock transport and a fake clock and still exercise
the real tool code.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx

from .airports import AirportDB
from .auth.obo import OboExchanger
from .config import Settings
from .opensky import OpenSkyClient
from .ops import OpsApiClient
from .weather import WeatherClient


@dataclass(slots=True)
class Runtime:
    settings: Settings
    opensky: OpenSkyClient
    weather: WeatherClient
    airports: AirportDB
    obo: OboExchanger
    ops: OpsApiClient
    clock: Callable[[], float]

    @classmethod
    def build(
        cls,
        settings: Settings | None = None,
        *,
        opensky_transport: httpx.AsyncBaseTransport | None = None,
        token_transport: httpx.AsyncBaseTransport | None = None,
        weather_transport: httpx.AsyncBaseTransport | None = None,
        idp_transport: httpx.AsyncBaseTransport | None = None,
        ops_transport: httpx.AsyncBaseTransport | None = None,
        sleep: Any = None,
        clock: Callable[[], float] = time.time,
    ) -> Runtime:
        # `time.time`, not `time.monotonic`: movement windows are sent upstream as
        # Unix epoch seconds, and monotonic seconds would ask OpenSky for traffic
        # in 1970. Durations are computed as differences, so wall clock is safe
        # for the credit window too.
        resolved = settings or Settings()
        return cls(
            settings=resolved,
            opensky=OpenSkyClient(
                resolved,
                transport=opensky_transport,
                token_transport=token_transport,
                sleep=sleep,
                clock=clock,
            ),
            weather=WeatherClient(resolved, transport=weather_transport),
            airports=AirportDB(resolved.airports_db),
            obo=OboExchanger(resolved, transport=idp_transport, clock=clock),
            ops=OpsApiClient(resolved, transport=ops_transport, sleep=sleep),
            clock=clock,
        )

    async def aclose(self) -> None:
        await self.opensky.aclose()
        await self.weather.aclose()
        await self.obo.aclose()
        await self.ops.aclose()


class _RuntimeHolder:
    """Module-level singleton, held in a class rather than a bare `global`."""

    instance: Runtime | None = None


_holder = _RuntimeHolder()


def get_runtime() -> Runtime:
    """The process-wide runtime, built on first use."""
    if _holder.instance is None:
        _holder.instance = Runtime.build()
    return _holder.instance


def set_runtime(runtime: Runtime | None) -> None:
    """Install a runtime (tests) or clear it (shutdown)."""
    _holder.instance = runtime
