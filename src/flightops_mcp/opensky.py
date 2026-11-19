"""OpenSky Network client: state vectors and airport movements.

Deliberate design points, each one visible in the tests:

* **Credits are charged before the request, not after.** A call that times out
  still consumes upstream budget, so the local ledger must behave the same way
  or the guard under-reports real spend. The cost of `/states/all` is a function
  of bounding-box area, so `BBox.credit_cost` mirrors the documented table
  (<=25 sq deg = 1, 25-100 = 2, 100-400 = 3, >400 = 4).
* **A 429 is a first-class outcome.** If the upstream says "retry in 3 seconds"
  and the configured ceiling is 5 seconds, we wait and retry once. If it says
  "retry in 900 seconds", we do not hold the tool call open: we return a
  structured error carrying that number, which is data the agent can reason
  about. Sleeping inside a tool for fifteen minutes is a failure mode, not
  resilience.
* **1 hour of history is the whole world.** Authenticated state vectors only go
  back an hour at 5-second resolution, so time-window arguments are clamped and
  the clamping is reported back to the caller rather than silently applied.
* **Three credential modes, one code path.** `client_credentials`, `anonymous`
  (opt-in) and `none` differ only in the header sent and in what the payload admits
  about precision. Adding credentials is a change to the environment and a restart.
"""

from __future__ import annotations

import asyncio
import math
import time
from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from typing import Any, Final, Literal

import httpx

from .config import Settings
from .credits import CreditLedger
from .errors import ToolFailure, daily_budget_exhausted, missing_credentials, upstream_unavailable
from .http import UpstreamHttp, authorized_get, credits_remaining_from_headers
from .opensky_token import OpenSkyTokenManager
from .telemetry import CREDITS_REMAINING, CREDITS_SPENT, UPSTREAM_CREDITS_SPENT, get_logger

SleepFn = Callable[[float], Any]

MAX_HISTORY_S = 3600  # authenticated state vectors: 1 hour back
TIME_RESOLUTION_S = 5  # authenticated state vectors: 5-second buckets
# Live state vectors are seconds old. A snapshot five minutes old shows aircraft
# that have since moved several miles, so it is not "around the airport now".
STATES_STALE_AFTER_S = 300

CredentialsMode = Literal["client_credentials", "anonymous", "none"]

ANONYMOUS_TIME_RESOLUTION_S: Final = 10
AUTHENTICATED_DAILY_CREDITS: Final = 4000
ANONYMOUS_DAILY_CREDITS: Final = 400
ANONYMOUS_LIMITATION: Final = (
    "OpenSky anonymous access: positions have 10-second resolution, only the most recent "
    "state vectors are served and any requested time is ignored. This is less precise than "
    "an authenticated answer."
)

DAILY_WINDOW_S: Final = 86_400.0

# Set while the dashboard or a wide snapshot is spending. A context variable, because
# the board reaches the client through tools whose signatures an agent also sees.
_daily_metered: ContextVar[bool] = ContextVar("opensky_daily_metered", default=False)


@contextmanager
def daily_metered() -> Iterator[None]:
    """Charge OpenSky spend made inside this block to the daily ledger as well."""
    token = _daily_metered.set(True)
    try:
        yield
    finally:
        _daily_metered.reset(token)


def credentials_mode(settings: Settings) -> CredentialsMode:
    """How this process reaches OpenSky. Real credentials win over the anonymous flag."""
    if settings.opensky_client_id and settings.opensky_client_secret:
        return "client_credentials"
    return "anonymous" if settings.opensky_anonymous else "none"


@dataclass(frozen=True, slots=True)
class BBox:
    """A WGS-84 bounding box, and the credit cost OpenSky charges for it."""

    lamin: float
    lomin: float
    lamax: float
    lomax: float

    def __post_init__(self) -> None:
        if self.lamin >= self.lamax:
            raise ValueError("lamin must be south of lamax")
        if self.lomin >= self.lomax:
            raise ValueError("lomin must be west of lomax")
        if not (-90 <= self.lamin <= 90 and -90 <= self.lamax <= 90):
            raise ValueError("latitude out of range")
        if not (-180 <= self.lomin <= 180 and -180 <= self.lomax <= 180):
            raise ValueError("longitude out of range")

    @property
    def area_sq_deg(self) -> float:
        return (self.lamax - self.lamin) * (self.lomax - self.lomin)

    @property
    def credit_cost(self) -> int:
        area = self.area_sq_deg
        if area <= 25:
            return 1
        if area <= 100:
            return 2
        if area <= 400:
            return 3
        return 4

    def as_params(self) -> dict[str, float]:
        return {
            "lamin": self.lamin,
            "lomin": self.lomin,
            "lamax": self.lamax,
            "lomax": self.lomax,
        }

    @classmethod
    def around(cls, lat: float, lon: float, radius_nm: float) -> BBox:
        """A box around a point. Deliberately simple: no projection games."""
        # 1 degree of latitude is ~60 nautical miles everywhere; longitude
        # shrinks by cos(latitude). The floor on cos keeps a polar query valid.
        dlat = radius_nm / 60.0
        cos_lat = max(0.05, math.cos(math.radians(lat)))
        dlon = radius_nm / (60.0 * cos_lat)
        return cls(
            lamin=max(-90.0, lat - dlat),
            lomin=max(-180.0, lon - dlon),
            lamax=min(90.0, lat + dlat),
            lomax=min(180.0, lon + dlon),
        )


@dataclass(frozen=True, slots=True)
class StateVector:
    """One aircraft, decoded from OpenSky's positional array format."""

    icao24: str
    callsign: str | None
    origin_country: str | None
    longitude: float | None
    latitude: float | None
    baro_altitude_m: float | None
    on_ground: bool
    velocity_ms: float | None
    true_track_deg: float | None
    vertical_rate_ms: float | None
    last_contact: int | None

    @classmethod
    def from_row(cls, row: list[Any]) -> StateVector:
        def at(index: int) -> Any:
            return row[index] if index < len(row) else None

        # Each field is taken only if it has the documented type, and is unknown
        # otherwise. A string where a number belongs is not a number, and passing it
        # through broke the most-recent-first sort with a TypeError.
        return cls(
            icao24=str(at(0) or ""),
            callsign=_text(at(1), strip=True),
            origin_country=_text(at(2)),
            longitude=_number(at(5)),
            latitude=_number(at(6)),
            baro_altitude_m=_number(at(7)),
            # `bool("false")` is True. Only a JSON true is on the ground.
            on_ground=at(8) is True,
            velocity_ms=_number(at(9)),
            true_track_deg=_number(at(10)),
            vertical_rate_ms=_number(at(11)),
            last_contact=_integer(at(4)),
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "icao24": self.icao24,
            "callsign": self.callsign,
            "origin_country": self.origin_country,
            "latitude": self.latitude,
            "longitude": self.longitude,
            "on_ground": self.on_ground,
            "baro_altitude_m": self.baro_altitude_m,
            "velocity_ms": self.velocity_ms,
            "true_track_deg": self.true_track_deg,
            "vertical_rate_ms": self.vertical_rate_ms,
            "last_contact": self.last_contact,
        }


@dataclass(slots=True)
class StatesSnapshot:
    time: int | None
    vectors: list[StateVector]
    credits_spent: int
    credits_remaining: int | None
    truncated: bool = False
    notes: list[str] = field(default_factory=list)
    # Rows in the answer that were not a state vector. Counted, never dropped
    # silently: a count over the readable rows is not the count.
    unreadable: int = 0
    received: int = 0
    # OpenSky's clock when it answered, from the response's `Date` header. The
    # snapshot's age is `upstream_time - time`; this machine's clock plays no part.
    upstream_time: int | None = None


@dataclass(frozen=True, slots=True)
class Movements:
    movements: list[Movement]
    # Rows that were not a flight object. Counted for the same reason as above.
    unreadable: int = 0
    received: int = 0
    upstream_time: int | None = None


@dataclass(frozen=True, slots=True)
class Movement:
    icao24: str
    callsign: str | None
    first_seen: int | None
    last_seen: int | None
    est_departure_airport: str | None
    est_arrival_airport: str | None

    def to_payload(self) -> dict[str, Any]:
        return {
            "icao24": self.icao24,
            "callsign": self.callsign,
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
            "est_departure_airport": self.est_departure_airport,
            "est_arrival_airport": self.est_arrival_airport,
        }


# Fixed, so a caller cannot ask for an antimeridian box or a surprise cost. Every one is
# over 400 square degrees, which is OpenSky's top tier: 4 credits, however wide.
AIRSPACE_REGIONS: Final[dict[str, BBox]] = {
    "europe": BBox(lamin=34.0, lomin=-25.0, lamax=72.0, lomax=45.0),
    "north_america": BBox(lamin=15.0, lomin=-170.0, lamax=72.0, lomax=-50.0),
    "world": BBox(lamin=-90.0, lomin=-180.0, lamax=90.0, lomax=180.0),
}


class OpenSkyClient:
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
        self._sleep = sleep
        self._http = UpstreamHttp(
            settings,
            "opensky",
            settings.opensky_base_url,
            transport=transport,
            sleep=sleep or _default_sleep,
        )
        self._tokens = OpenSkyTokenManager(settings, transport=token_transport, clock=clock)
        self._ledger = CreditLedger(settings.credit_budget_per_hour, clock=clock)
        # In memory, like the hourly ledger: it does NOT survive a restart, so a restart
        # forgets the last 24 hours of spend. OpenSky's own count does not reset with it,
        # which is why `upstream_remaining_last_seen` is reported next to this one.
        self._daily = CreditLedger(
            settings.dashboard_daily_credit_budget, clock=clock, window_s=DAILY_WINDOW_S
        )
        self._log = get_logger(__name__)
        self.credentials_mode: CredentialsMode = credentials_mode(settings)
        self.credits_spent_by_mode: Counter[str] = Counter()
        self.last_credits_remaining: int | None = None

    async def aclose(self) -> None:
        await self._http.aclose()
        await self._tokens.aclose()

    async def __aenter__(self) -> OpenSkyClient:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    # --- budget -------------------------------------------------------------
    def _require_access(self) -> None:
        """Refuse before anything is charged: a call never sent spends nothing."""
        if self.credentials_mode == "none":
            raise missing_credentials()

    def _charge(self, credits: int) -> None:
        metered = _daily_metered.get()
        # Both limits are checked before either records, so a refusal charges neither.
        if metered and self._daily.spent() + credits > self._daily.budget:
            raise daily_budget_exhausted(self._daily.spent(), self._daily.budget)
        self._ledger.charge(credits)
        if metered:
            self._daily.charge(credits)
        self.credits_spent_by_mode[self.credentials_mode] += credits
        CREDITS_SPENT.inc(credits)
        UPSTREAM_CREDITS_SPENT.labels(upstream="opensky").inc(credits)

    @property
    def credits_spent_this_hour(self) -> int:
        return self._ledger.spent()

    @property
    def credits_spent_total(self) -> int:
        return self._ledger.total

    @property
    def credits_spent_24h(self) -> int:
        """Spend by the dashboard and airspace snapshots in the rolling 24 hours."""
        return self._daily.spent()

    @property
    def daily_budget(self) -> int:
        return self._daily.budget

    @property
    def token_cached(self) -> bool:
        return self._tokens.cached()

    def freshness(self) -> dict[str, Any]:
        """What this credential mode can promise about a traffic answer."""
        anonymous = self.credentials_mode == "anonymous"
        return {
            "credentials": self.credentials_mode,
            "time_resolution_s": ANONYMOUS_TIME_RESOLUTION_S if anonymous else TIME_RESOLUTION_S,
            "most_recent_only": anonymous,
            "time_parameter_honoured": not anonymous,
            "upstream_daily_credits": (
                ANONYMOUS_DAILY_CREDITS if anonymous else AUTHENTICATED_DAILY_CREDITS
            ),
        }

    # --- requests -----------------------------------------------------------
    async def _authorized_get(self, path: str, params: dict[str, Any]) -> tuple[Any, int | None]:
        """GET under the shared 401/429 policy (http.py), as this credential mode.

        Returns the body and the upstream's `Date`, which is None when the answer had none.
        """
        response = await authorized_get(
            self._http,
            self._tokens if self.credentials_mode == "client_credentials" else None,
            path,
            params,
            upstream="OpenSky",
            settings=self._settings,
            sleep=self._sleep or _default_sleep,
            # 404 is OpenSky's documented answer for "no flights in this interval".
            empty_status=frozenset({404}),
        )
        remaining = credits_remaining_from_headers(response.headers)
        if remaining is not None:
            self.last_credits_remaining = remaining
            CREDITS_REMAINING.set(float(remaining))
        return response.json_body, response.upstream_time_epoch

    async def states_in_bbox(self, bbox: BBox, *, max_vectors: int = 200) -> StatesSnapshot:
        self._require_access()
        cost = bbox.credit_cost
        self._charge(cost)
        body, upstream_time = await self._authorized_get("/states/all", bbox.as_params())
        snapshot = decode_states(body, max_vectors=max_vectors)
        snapshot.upstream_time = upstream_time
        snapshot.credits_spent = cost
        snapshot.credits_remaining = self.last_credits_remaining
        return snapshot

    async def movements(
        self,
        airport_icao: str,
        direction: str,
        *,
        begin: int,
        end: int,
    ) -> Movements:
        if direction not in {"arrival", "departure"}:
            raise ToolFailure(
                code="invalid_argument",
                message="direction must be 'arrival' or 'departure'",
                hint="Use 'arrival' for inbound and 'departure' for outbound flights.",
            )
        # Anonymous mode does not call flights, on purpose. Measured 2026-11-18: OpenSky
        # answered the last hour anonymously but debited 18 and 30 credits for one
        # direction each, not the 4 charged here, out of 400 a day; older windows were 403.
        if self.credentials_mode != "client_credentials":
            raise missing_credentials()
        cost = 4  # live / < 24h window
        self._charge(cost)
        body, upstream_time = await self._authorized_get(
            f"/flights/{direction}",
            {"airport": airport_icao, "begin": begin, "end": end},
        )
        return replace(decode_movements(body), upstream_time=upstream_time)


def decode_states(body: object, *, max_vectors: int) -> StatesSnapshot:
    """`/states/all` as a snapshot. Credits are the caller's to fill in.

    Rows that are not a state vector are counted in `unreadable`, never dropped
    silently, so `received == readable + unreadable` always holds and a count
    over the readable rows is known to be a lower bound.
    """
    if not isinstance(body, dict):
        raise upstream_unavailable("OpenSky", "states response was not a JSON object")

    # `null` is how OpenSky says "no aircraft". Any other non-list is not an empty sky.
    rows = body.get("states")
    if rows is None:
        rows = []
    if not isinstance(rows, list):
        raise upstream_unavailable("OpenSky", "states field was not a list")
    readable = [row for row in rows if _is_state_row(row)]
    vectors = [StateVector.from_row(row) for row in readable]

    notes: list[str] = []
    truncated = len(vectors) > max_vectors
    if truncated:
        # Sorting by last contact keeps the most recently heard aircraft,
        # which is what an operations question actually wants.
        vectors.sort(key=lambda v: v.last_contact or 0, reverse=True)
        notes.append(
            f"{len(vectors)} aircraft seen; returning the {max_vectors} most recently heard."
        )
        vectors = vectors[:max_vectors]

    return StatesSnapshot(
        time=_integer(body.get("time")),
        vectors=vectors,
        credits_spent=0,
        credits_remaining=None,
        truncated=truncated,
        notes=notes,
        unreadable=len(rows) - len(readable),
        received=len(rows),
    )


def decode_movements(body: object) -> Movements:
    """`/flights/{arrival,departure}` as movements, unreadable rows counted."""
    # 404 is OpenSky's documented answer for "no flights in this interval".
    if body is None:
        return Movements([])
    # Documented as a JSON array of flight objects. Anything else is not an
    # empty window, it is an answer this client cannot read.
    if not isinstance(body, list):
        raise upstream_unavailable("OpenSky", "movements response was not a JSON array")
    out: list[Movement] = []
    for row in body:
        if not isinstance(row, dict):
            continue
        out.append(
            Movement(
                icao24=str(row.get("icao24") or ""),
                callsign=_text(row.get("callsign"), strip=True),
                first_seen=_integer(row.get("firstSeen")),
                last_seen=_integer(row.get("lastSeen")),
                est_departure_airport=_text(row.get("estDepartureAirport")),
                est_arrival_airport=_text(row.get("estArrivalAirport")),
            )
        )
    return Movements(out, unreadable=len(body) - len(out), received=len(body))


def _number(value: object) -> float | None:
    """A JSON number, or None. `bool` is an int in Python and not a number in JSON."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return value if math.isfinite(value) else None


def _integer(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _text(value: object, *, strip: bool = False) -> str | None:
    if not isinstance(value, str):
        return None
    return value.strip() if strip else value


def _is_state_row(row: object) -> bool:
    """A state vector is an array whose first field is the transponder address."""
    return isinstance(row, list) and bool(row) and isinstance(row[0], str) and bool(row[0])


async def _default_sleep(seconds: float) -> None:
    await asyncio.sleep(seconds)
