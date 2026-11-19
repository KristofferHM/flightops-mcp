"""Composes tool answers into what each dashboard endpoint returns."""

from __future__ import annotations

import asyncio
import math
import time
from collections import deque
from collections.abc import Callable, Mapping
from typing import Any, Final

from flightops_mcp.airports import CONTINENTS
from flightops_mcp.opensky import AIRSPACE_REGIONS
from flightops_mcp.server import WEATHER_OVERVIEW_MAX

from . import board
from .sources import ToolSource

# The selected airport's search. 40 nm around any listed airport is one credit.
DETAIL_RADIUS_NM: Final = 40.0
DETAIL_MAX_AIRCRAFT: Final = 40
AIRSPACE_MAX_AIRCRAFT: Final = 5000
# NOAA is free and shared: a continent of airports must not arrive as one burst.
WEATHER_CONCURRENCY: Final = 4
# Every region is in OpenSky's top tier (over 400 square degrees).
TRAFFIC_COST: Final = 4
# The fastest the layer refreshes, whatever the ceiling allows.
TRAFFIC_MIN_INTERVAL_S: Final = 120
# Answers that mean "not now" from a guard, rather than a failure: auto-refresh stops.
REFUSAL_CODES: Final = frozenset({"local_budget_exhausted", "upstream_rate_limited"})
# An airport the overview did not answer for, which only a stale reference file causes.
NOT_ANSWERED: Final = {
    "ok": False,
    "error": {"code": "not_answered", "message": "The weather overview held no entry for it."},
}
LIST_PAGE: Final = 25
LIST_PAGE_MAX: Final = 50

Payload = dict[str, Any]

# The board's regions, in the order the control lists them. Each draws the airports of
# one OurAirports continent (None is all of them) and asks OpenSky for one fixed box.
REGIONS: Final[Mapping[str, tuple[str, str | None]]] = {
    "europe": (CONTINENTS["EU"], "EU"),
    "north_america": (CONTINENTS["NA"], "NA"),
    "south_america": (CONTINENTS["SA"], "SA"),
    "africa": (CONTINENTS["AF"], "AF"),
    "asia": (CONTINENTS["AS"], "AS"),
    "oceania": (CONTINENTS["OC"], "OC"),
    "world": ("World", None),
}
DEFAULT_REGION: Final = "europe"
COVERAGE_NOTE: Final = (
    "Airport data is worldwide; live traffic is not. OpenSky's receivers are dense over "
    "Europe and North America and sparse over Africa, South America and much of Asia, so "
    "an empty patch of sky means no receiver heard anything there, not that no aircraft "
    "are flying."
)


class UnknownBoardItem(KeyError):
    """An airport or region this board does not list."""


class TrafficSpend:
    """What the traffic layer has spent: since the board started, and in the last hour.

    Its own ceiling, under the tools' hourly and daily guards, so a layer left on in a
    forgotten tab stops by itself and says why instead of spending until a guard refuses.
    The clock is this machine's: it measures this board's own spending, not upstream data.
    """

    def __init__(
        self, ceiling_per_hour: int, *, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self.ceiling = ceiling_per_hour
        self.session_total = 0
        self._clock = clock
        self._spend: deque[tuple[float, int]] = deque()

    def spent_last_hour(self) -> int:
        cutoff = self._clock() - 3600.0
        while self._spend and self._spend[0][0] <= cutoff:
            self._spend.popleft()
        return sum(credits for _, credits in self._spend)

    def record(self, credits: int) -> None:
        if credits > 0:
            self._spend.append((self._clock(), credits))
            self.session_total += credits

    def refusal(self) -> str | None:
        """Why a refresh may not spend now, or None when it may."""
        spent = self.spent_last_hour()
        if spent + TRAFFIC_COST <= self.ceiling:
            return None
        if not self._spend or self.ceiling < TRAFFIC_COST:
            return f"the hourly traffic ceiling is {self.ceiling} credits, under one refresh"
        # The refresh fits once enough of the oldest spend has left the hour.
        freed, wait = 0, 0.0
        for at, credits in self._spend:
            freed += credits
            wait = at + 3600.0 - self._clock()
            if spent - freed + TRAFFIC_COST <= self.ceiling:
                break
        return (
            f"{spent} of {self.ceiling} traffic credits used in the last hour; "
            f"the next refresh fits in {board.age_text(max(60.0, wait))}"
        )

    def interval_s(self) -> int:
        """Seconds between refreshes that keep a layer left on under the ceiling."""
        if self.ceiling < TRAFFIC_COST:
            return TRAFFIC_MIN_INTERVAL_S
        return max(TRAFFIC_MIN_INTERVAL_S, math.ceil(3600 * TRAFFIC_COST / self.ceiling))

    def view(self) -> Payload:
        return {
            "per_refresh": TRAFFIC_COST,
            "session_total": self.session_total,
            "hour_spent": self.spent_last_hour(),
            "hour_ceiling": self.ceiling,
            "interval_s": self.interval_s(),
        }


def traffic_default(credentials: str | None) -> Payload:
    """Whether the layer starts on, and the sentence that says why."""
    if credentials == "client_credentials":
        return {
            "on": True,
            "available": True,
            "reason": "OpenSky credentials are configured, so traffic starts on.",
        }
    if credentials == "anonymous":
        return {
            "on": False,
            "available": True,
            "reason": "No OpenSky credentials: traffic starts off. Turning it on spends "
            "from the anonymous allowance, at 10 s resolution.",
        }
    return {
        "on": False,
        "available": False,
        "reason": "No OpenSky credentials are configured, so there is no traffic to show. "
        "Set FLIGHTOPS_OPENSKY_CLIENT_ID and FLIGHTOPS_OPENSKY_CLIENT_SECRET.",
    }


def _matches(airport: Payload, needle: str) -> bool:
    fields = (airport["ident"], airport.get("iata"), airport["name"], airport.get("municipality"))
    return any(needle in str(value).casefold() for value in fields if value)


class BoardService:
    def __init__(
        self,
        source: ToolSource,
        airports: list[Payload],
        *,
        traffic_ceiling_per_hour: int = 60,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._source = source
        self._airports = {airport["ident"]: airport for airport in airports}
        self._views: dict[str, Payload] = {}
        self.traffic = TrafficSpend(traffic_ceiling_per_hour, clock=clock)
        # The newest refusal of a traffic refresh, shown on the badge until one succeeds.
        self._traffic_refused: str | None = None

    def _airport(self, icao: str) -> Payload:
        # Only listed airports: a request must not be able to pick what credits buy.
        try:
            return self._airports[icao.upper()]
        except KeyError:
            raise UnknownBoardItem(icao) from None

    def _region(self, region: str) -> list[Payload]:
        if region not in REGIONS:
            raise UnknownBoardItem(region)
        continent = REGIONS[region][1]
        return [
            airport
            for airport in self._airports.values()
            if continent is None or airport.get("continent") == continent
        ]

    def regions(self) -> list[Payload]:
        """The region control's choices, each with how many airports it will draw."""
        return [
            {"id": key, "label": label, "airports": len(self._region(key))}
            for key, (label, _) in REGIONS.items()
        ]

    async def _weather(self, icao: str) -> Payload:
        return board.weather_view(await self._source.call("airport_weather", {"airport": icao}))

    async def _overview(self, idents: list[str]) -> dict[str, Payload]:
        """Weather views for many airports, one `weather_overview` call per hundred."""
        gate = asyncio.Semaphore(WEATHER_CONCURRENCY)
        chunks = [
            idents[start : start + WEATHER_OVERVIEW_MAX]
            for start in range(0, len(idents), WEATHER_OVERVIEW_MAX)
        ]

        async def one(chunk: list[str]) -> Payload:
            async with gate:
                return await self._source.call("weather_overview", {"airports": chunk})

        answers = await asyncio.gather(*(one(chunk) for chunk in chunks))
        views: dict[str, Payload] = {}
        for chunk, answer in zip(chunks, answers, strict=True):
            entries = (answer.get("airports") or {}) if answer.get("ok") else {}
            for icao in chunk:
                entry = entries.get(icao) if answer.get("ok") else answer
                views[icao] = board.weather_view(entry or NOT_ANSWERED)
        return views

    async def snapshot(
        self,
        region: str = DEFAULT_REGION,
        traffic_snapshot: int | None = None,
        traffic_upstream: int | None = None,
    ) -> Payload:
        """The region's airports with their weather, plus the banner, badge and strip.

        `traffic_snapshot` is the time of the traffic layer the page is drawing, and
        `traffic_upstream` OpenSky's clock when it sent it, both as this server gave them.
        They cost nothing: no upstream is asked about traffic here.
        """
        listed = self._region(region)
        views = await self._overview([airport["ident"] for airport in listed])
        self._views.update(views)
        status = await self._source.call("upstream_status", {})
        governance = board.governance(status)
        weather = list(views.values())
        return {
            "banner": board.banner(weather, self._source.recorded_at),
            "last_update": board.last_update(
                weather,
                self._source.recorded_at,
                traffic_snapshot,
                traffic_upstream,
                self._traffic_refused,
            ),
            "governance": governance,
            "traffic": {
                **traffic_default(governance["credentials"]),
                **self.traffic.view(),
                "refused": self._traffic_refused,
            },
            "region": region,
            "regions": self.regions(),
            "coverage": COVERAGE_NOTE,
            "airports": [
                {
                    "icao": record["ident"],
                    "iata": record.get("iata"),
                    "name": record["name"],
                    "type": record.get("type"),
                    "latitude": record["latitude"],
                    "longitude": record["longitude"],
                    "weather": {"summary": view["summary"]},
                    "marker": board.marker(view),
                }
                for record, view in ((r, views[r["ident"]]) for r in listed)
            ],
        }

    def airport_list(
        self, region: str, query: str = "", offset: int = 0, limit: int = LIST_PAGE
    ) -> Payload:
        """One page of the region's airports, filtered here so the page never holds them all."""
        listed = self._region(region)
        needle = query.strip().casefold()
        found = [airport for airport in listed if not needle or _matches(airport, needle)]
        limit = max(1, min(LIST_PAGE_MAX, limit))
        offset = max(0, min(offset, max(0, len(found) - 1)))
        page = found[offset : offset + limit]
        unseen = {"state": "unknown", "summary": board.NO_DATA_YET}
        items = []
        for airport in page:
            view = self._views.get(airport["ident"], unseen)
            items.append(
                {
                    "icao": airport["ident"],
                    "iata": airport.get("iata"),
                    "name": airport["name"],
                    "municipality": airport.get("municipality"),
                    "country": airport.get("iso_country"),
                    "type": airport.get("type"),
                    "weather": {"summary": view["summary"]},
                    "marker": board.marker(view),
                }
            )
        return {
            "region": region,
            "query": query,
            "region_total": len(listed),
            "total": len(found),
            "offset": offset,
            "limit": limit,
            "items": items,
        }

    async def detail(self, icao: str) -> Payload:
        """The selected airport: weather and the aircraft heard around it."""
        record = self._airport(icao)
        ident = record["ident"]
        weather = await self._weather(ident)
        self._views[ident] = weather
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
            "type": record.get("type"),
            "latitude": record["latitude"],
            "longitude": record["longitude"],
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
        """The region's traffic layer, if the layer's ceiling allows one more refresh."""
        if region not in AIRSPACE_REGIONS or region not in REGIONS:
            raise UnknownBoardItem(region)
        refused = self.traffic.refusal()
        if refused is not None:
            self._traffic_refused = f"Auto-refresh stopped: {refused}."
            return {**board.refused_view(self._traffic_refused), "cost": self.traffic.view()}
        payload = await self._source.call(
            "airspace_snapshot", {"region": region, "max_aircraft": AIRSPACE_MAX_AIRCRAFT}
        )
        spent = (payload.get("bookkeeping") or {}).get("credits_spent")
        self.traffic.record(spent if isinstance(spent, int) else 0)
        view = board.airspace_view(payload)
        error = payload.get("error") or {}
        if payload.get("ok"):
            self._traffic_refused = None
        elif error.get("code") in REFUSAL_CODES:
            # A guard said no. Auto-refresh stops, and the badge says which guard.
            self._traffic_refused = f"Refresh refused: {error.get('message', error['code'])}"
            view["refused"] = self._traffic_refused
        return {**view, "cost": self.traffic.view()}
