"""Schiphol Public Flight API (v4): the scheduled flights at Amsterdam Airport Schiphol.

**What this adapter has and has not been run against.** It implements the request
and auth shape Schiphol documents, and it is exercised against a mock of that shape
(`services/mock_upstream/`), in the test suite and in the compose matrix. No live
Schiphol key was used to build or test it, and none is in this repository. If a key
is supplied it arrives through the environment, like every other credential:
`FLIGHTOPS_SCHIPHOL_CLIENT_ID` and `FLIGHTOPS_SCHIPHOL_CLIENT_SECRET`.

The documented shape (developer.schiphol.nl, "API Consumer Migration Guide",
2026-07-20):

* Token: `POST https://api.auth.schiphol.nl/oauth/token`, form-encoded
  `grant_type=client_credentials`, `client_id`, `client_secret` and `audience`,
  which must equal the tier's URL (`https://api.schiphol.nl/public`) or the API
  answers 401/403. `expires_in` is 1800. Refresh 60 s before expiry, not on a 401.
* Call: `GET https://api.schiphol.nl/public/public-flights/v4/flights` with
  `Authorization: Bearer` and `Accept: application/json`. The version is in the
  path; the old `ResourceVersion`, `app_id` and `app_key` headers are retired.

The flight fields read here (`flightName`, `flightDirection`, `scheduleDateTime`,
`publicFlightState.flightStates`, `route.destinations`, the estimated and actual
times) and the query parameters (`flightDirection`, `fromDateTime`, `toDateTime`,
`searchDateTimeField`, `page`) are the v4 flights resource as Schiphol has
published it. The mock serves the same fields; it cannot prove Schiphol still does.

What it shares with OpenSky, by construction rather than by copy: the token
manager (`opensky_token.py`, with an audience), the 401/429 policy and the error
taxonomy (`http.authorized_get`), the retrying HTTP client with its circuit
breaker and passthrough guard (`http.UpstreamHttp`), and the credit ledger
(`credits.py`), charged before each request as OpenSky's is.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Final

import httpx

from .config import Settings
from .credits import CreditLedger
from .errors import missing_credentials, upstream_unavailable
from .http import UpstreamHttp, authorized_get
from .opensky_token import ClientCredentialsTokenManager
from .telemetry import UPSTREAM_CREDITS_SPENT

FLIGHTS_PATH: Final = "/public-flights/v4/flights"
# One request is one unit of the public tier's per-minute limit.
REQUEST_COST: Final = 1
DIRECTIONS: Final = {"arrival": "A", "departure": "D"}
# Flight states the v4 resource documents, by the question they answer.
CANCELLED: Final = frozenset({"CNX"})
DIVERTED: Final = frozenset({"DIV"})
DELAYED: Final = frozenset({"DEL"})
# An estimate this much later than the schedule counts as delayed, state or not.
DELAY_THRESHOLD: Final = timedelta(minutes=15)
_NEXT_LINK = re.compile(r'<([^>]+)>\s*;\s*rel="?next"?')


def amsterdam_offset(instant: datetime) -> timedelta:
    """UTC+1, or UTC+2 in EU summer time. Written out because Windows has no tz data.

    EU rule since 1996: summer time from 01:00 UTC on the last Sunday of March to
    01:00 UTC on the last Sunday of October. The API's `fromDateTime` is local time.
    """
    utc = instant.astimezone(UTC)
    start = _last_sunday(utc.year, 3).replace(hour=1)
    end = _last_sunday(utc.year, 10).replace(hour=1)
    return timedelta(hours=2) if start <= utc < end else timedelta(hours=1)


def _last_sunday(year: int, month: int) -> datetime:
    first_of_next = datetime(year + (month == 12), month % 12 + 1, 1, tzinfo=UTC)
    last = first_of_next - timedelta(days=1)
    return last - timedelta(days=(last.weekday() - 6) % 7)


def local_stamp(epoch: float) -> str:
    """Epoch seconds as Schiphol's local `yyyy-MM-ddTHH:mm:ss`, without an offset."""
    instant = datetime.fromtimestamp(epoch, tz=UTC)
    return (instant + amsterdam_offset(instant)).strftime("%Y-%m-%dT%H:%M:%S")


@dataclass(frozen=True, slots=True)
class ScheduledFlight:
    flight: str
    direction: str
    scheduled: str
    estimated: str | None
    actual: str | None
    states: tuple[str, ...]
    route: tuple[str, ...]

    @property
    def cancelled(self) -> bool:
        return bool(CANCELLED.intersection(self.states))

    @property
    def diverted(self) -> bool:
        return bool(DIVERTED.intersection(self.states))

    @property
    def delayed(self) -> bool:
        if DELAYED.intersection(self.states):
            return True
        scheduled, estimated = _instant(self.scheduled), _instant(self.estimated)
        return bool(scheduled and estimated and estimated - scheduled >= DELAY_THRESHOLD)

    def to_payload(self) -> dict[str, Any]:
        return {
            "flight": self.flight,
            "direction": self.direction,
            "scheduled": self.scheduled,
            "estimated": self.estimated,
            "actual": self.actual,
            "states": list(self.states),
            "route": list(self.route),
        }


@dataclass(slots=True)
class FlightsPage:
    flights: list[ScheduledFlight]
    unreadable: int = 0
    received: int = 0
    next_page: int | None = None
    upstream_time: int | None = None


@dataclass(slots=True)
class Schedule:
    flights: list[ScheduledFlight] = field(default_factory=list)
    unreadable: int = 0
    received: int = 0
    requests: int = 0
    truncated: bool = False
    # Schiphol's clock at the newest page it answered, from the `Date` header.
    upstream_time: int | None = None


class SchipholClient:
    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        token_transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Any] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._settings = settings
        self._sleep = sleep or _default_sleep
        self._http = UpstreamHttp(
            settings,
            "schiphol",
            settings.schiphol_base_url,
            transport=transport,
            sleep=self._sleep,
        )
        self._tokens = ClientCredentialsTokenManager(
            settings,
            upstream="Schiphol",
            token_url=settings.schiphol_auth_url,
            client_id=settings.schiphol_client_id,
            client_secret=settings.schiphol_client_secret,
            audience=settings.schiphol_audience,
            skew_s=settings.schiphol_token_refresh_ahead_s,
            missing=lambda: missing_credentials(
                "Schiphol",
                ("FLIGHTOPS_SCHIPHOL_CLIENT_ID", "FLIGHTOPS_SCHIPHOL_CLIENT_SECRET"),
                "an application on https://developer.schiphol.nl",
            ),
            transport=token_transport,
            clock=clock,
        )
        self._ledger = CreditLedger(settings.schiphol_request_budget_per_hour, clock=clock)

    async def aclose(self) -> None:
        await self._http.aclose()
        await self._tokens.aclose()

    @property
    def configured(self) -> bool:
        return self._tokens.has_credentials()

    @property
    def token_cached(self) -> bool:
        return self._tokens.cached()

    @property
    def requests_this_hour(self) -> int:
        return self._ledger.spent()

    @property
    def requests_total(self) -> int:
        return self._ledger.total

    @property
    def budget_per_hour(self) -> int:
        return self._ledger.budget

    async def schedule(self, direction: str, *, begin: float, end: float) -> Schedule:
        """Every page of one direction's flights scheduled in [begin, end], up to the cap."""
        out = Schedule()
        page: int | None = 0
        while page is not None:
            if out.requests >= self._settings.schiphol_max_pages:
                out.truncated = True
                break
            result = await self._page(direction, begin=begin, end=end, page=page)
            out.requests += 1
            out.flights.extend(result.flights)
            out.unreadable += result.unreadable
            out.received += result.received
            if result.upstream_time is not None:
                out.upstream_time = max(out.upstream_time or 0, result.upstream_time)
            page = result.next_page
        return out

    async def _page(self, direction: str, *, begin: float, end: float, page: int) -> FlightsPage:
        # Charged before the request, as OpenSky's credits are: a request that times
        # out still counts against the upstream's limit.
        self._ledger.charge(REQUEST_COST)
        UPSTREAM_CREDITS_SPENT.labels(upstream="schiphol").inc(REQUEST_COST)
        response = await authorized_get(
            self._http,
            self._tokens,
            FLIGHTS_PATH,
            {
                "flightDirection": DIRECTIONS[direction],
                "fromDateTime": local_stamp(begin),
                "toDateTime": local_stamp(end),
                "searchDateTimeField": "scheduleDateTime",
                "sort": "+scheduleDateTime",
                "page": page,
            },
            upstream="Schiphol",
            settings=self._settings,
            sleep=self._sleep,
            # 204 is how the v4 resource answers a valid query with no flights.
            empty_status=frozenset({204}),
        )
        if response.status_code == 204:
            return FlightsPage([], upstream_time=response.upstream_time_epoch)
        result = decode_flights(response.json_body)
        result.next_page = page + 1 if _has_next(response.headers) else None
        result.upstream_time = response.upstream_time_epoch
        return result


def decode_flights(body: object) -> FlightsPage:
    """The v4 `{"flights": [...]}` body. Unreadable rows are counted, never dropped silently."""
    if not isinstance(body, dict):
        raise upstream_unavailable("Schiphol", "flights response was not a JSON object")
    rows = body.get("flights")
    if rows is None:
        rows = []
    if not isinstance(rows, list):
        raise upstream_unavailable("Schiphol", "flights field was not a list")
    flights = [flight for flight in (_flight(row) for row in rows) if flight is not None]
    return FlightsPage(flights, unreadable=len(rows) - len(flights), received=len(rows))


def _flight(row: object) -> ScheduledFlight | None:
    if not isinstance(row, dict):
        return None
    name, direction, scheduled = (
        row.get("flightName"),
        row.get("flightDirection"),
        row.get("scheduleDateTime"),
    )
    if not (isinstance(name, str) and name and direction in {"A", "D"}):
        return None
    if not isinstance(scheduled, str) or _instant(scheduled) is None:
        return None
    state = row.get("publicFlightState")
    states = state.get("flightStates") if isinstance(state, dict) else None
    route = row.get("route")
    destinations = route.get("destinations") if isinstance(route, dict) else None
    if direction == "A":
        estimated, actual = row.get("estimatedLandingTime"), row.get("actualLandingTime")
    else:
        estimated = row.get("publicEstimatedOffBlockTime")
        actual = row.get("actualOffBlockTime")
    return ScheduledFlight(
        flight=name,
        direction="arrival" if direction == "A" else "departure",
        scheduled=scheduled,
        estimated=estimated if isinstance(estimated, str) else None,
        actual=actual if isinstance(actual, str) else None,
        states=tuple(s for s in states if isinstance(s, str)) if isinstance(states, list) else (),
        route=tuple(d for d in destinations if isinstance(d, str))
        if isinstance(destinations, list)
        else (),
    )


def _instant(value: str | None) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else None


def _has_next(headers: dict[str, str]) -> bool:
    lowered = {name.lower(): value for name, value in headers.items()}
    return bool(_NEXT_LINK.search(lowered.get("link", "")))


async def _default_sleep(seconds: float) -> None:
    await asyncio.sleep(seconds)
