"""Composes tool answers into what each dashboard endpoint returns."""

from __future__ import annotations

import asyncio
from typing import Any, Final

from flightops_mcp.opensky import AIRSPACE_REGIONS

from . import board
from .sources import ToolSource

# The selected airport's search. 40 nm around any listed airport is one credit.
DETAIL_RADIUS_NM: Final = 40.0
DETAIL_MAX_AIRCRAFT: Final = 40
AIRSPACE_MAX_AIRCRAFT: Final = 5000
# NOAA is free and shared: a board of airports must not arrive as one burst.
WEATHER_CONCURRENCY: Final = 4

Payload = dict[str, Any]


class UnknownBoardItem(KeyError):
    """An airport or region this board does not list."""


class BoardService:
    def __init__(self, source: ToolSource, airports: list[Payload]) -> None:
        self._source = source
        self._airports = {airport["ident"]: airport for airport in airports}

    def _airport(self, icao: str) -> Payload:
        # Only listed airports: a request must not be able to pick what credits buy.
        try:
            return self._airports[icao.upper()]
        except KeyError:
            raise UnknownBoardItem(icao) from None

    async def _weather(self, icao: str) -> Payload:
        return board.weather_view(await self._source.call("airport_weather", {"airport": icao}))

    async def snapshot(
        self, traffic_snapshot: int | None = None, traffic_upstream: int | None = None
    ) -> Payload:
        """Every listed airport with its weather, plus the banner, badge and governance strip.

        `traffic_snapshot` is the time of the traffic layer the page is drawing, and
        `traffic_upstream` OpenSky's clock when it sent it, both as this server gave them.
        They cost nothing: no upstream is asked about traffic here.
        """
        gate = asyncio.Semaphore(WEATHER_CONCURRENCY)

        async def one(icao: str) -> Payload:
            async with gate:
                return await self._weather(icao)

        views = await asyncio.gather(*(one(icao) for icao in self._airports))
        status = await self._source.call("upstream_status", {})
        return {
            "banner": board.banner(views, self._source.recorded_at),
            "last_update": board.last_update(
                views, self._source.recorded_at, traffic_snapshot, traffic_upstream
            ),
            "governance": board.governance(status),
            "regions": list(AIRSPACE_REGIONS),
            "airports": [
                {
                    "icao": record["ident"],
                    "iata": record.get("iata"),
                    "name": record["name"],
                    "latitude": record["latitude"],
                    "longitude": record["longitude"],
                    "weather": view,
                    "marker": board.marker(view),
                }
                for record, view in zip(self._airports.values(), views, strict=True)
            ],
        }

    async def detail(self, icao: str) -> Payload:
        """The selected airport: weather and the aircraft heard around it."""
        record = self._airport(icao)
        ident = record["ident"]
        weather = await self._weather(ident)
        traffic = await self._source.call(
            "airport_live_traffic",
            {
                "airport": ident,
                "radius_nm": DETAIL_RADIUS_NM,
                "max_aircraft": DETAIL_MAX_AIRCRAFT,
            },
        )
        return {
            "icao": ident,
            "name": record["name"],
            "weather": weather,
            "marker": board.marker(weather),
            "traffic": board.traffic_view(traffic),
            "movements": {"arrivals": "not requested", "departures": "not requested"},
        }

    async def triage(self, icao: str) -> Payload:
        ident = self._airport(icao)["ident"]
        payload = await self._source.call("disruption_triage", {"airport": ident})
        return {"icao": ident, **board.triage_view(payload)}

    async def airspace(self, region: str) -> Payload:
        if region not in AIRSPACE_REGIONS:
            raise UnknownBoardItem(region)
        payload = await self._source.call(
            "airspace_snapshot", {"region": region, "max_aircraft": AIRSPACE_MAX_AIRCRAFT}
        )
        return board.airspace_view(payload)
