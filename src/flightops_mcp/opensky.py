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
"""

from __future__ import annotations

import asyncio
import math
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx

from .config import Settings
from .errors import (
    ToolFailure,
    budget_exhausted,
    upstream_auth_failed,
    upstream_rate_limited,
    upstream_unavailable,
)
from .http import UpstreamHttp
from .opensky_token import OpenSkyTokenManager
from .telemetry import CREDITS_REMAINING, CREDITS_SPENT, get_logger

SleepFn = Callable[[float], Any]

MAX_HISTORY_S = 3600  # authenticated state vectors: 1 hour back
TIME_RESOLUTION_S = 5  # authenticated state vectors: 5-second buckets
# Live state vectors are seconds old. A snapshot five minutes old shows aircraft
# that have since moved several miles, so it is not "around the airport now".
STATES_STALE_AFTER_S = 300


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

        callsign = at(1)
        return cls(
            icao24=str(at(0) or ""),
            callsign=callsign.strip() if isinstance(callsign, str) else None,
            origin_country=at(2),
            longitude=at(5),
            latitude=at(6),
            baro_altitude_m=at(7),
            on_ground=bool(at(8)),
            velocity_ms=at(9),
            true_track_deg=at(10),
            vertical_rate_ms=at(11),
            last_contact=at(4),
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


@dataclass(frozen=True, slots=True)
class Movements:
    movements: list[Movement]
    # Rows that were not a flight object. Counted for the same reason as above.
    unreadable: int = 0
    received: int = 0


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
        self._clock = clock
        self._sleep = sleep
        self._http = UpstreamHttp(
            settings,
            "opensky",
            settings.opensky_base_url,
            transport=transport,
            sleep=sleep or _default_sleep,
        )
        self._tokens = OpenSkyTokenManager(settings, transport=token_transport, clock=clock)
        self._spend: deque[tuple[float, int]] = deque()
        self._log = get_logger(__name__)
        self.last_credits_remaining: int | None = None
        self.credits_spent_total = 0

    async def aclose(self) -> None:
        await self._http.aclose()
        await self._tokens.aclose()

    async def __aenter__(self) -> OpenSkyClient:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    # --- budget -------------------------------------------------------------
    def _spent_this_hour(self) -> int:
        cutoff = self._clock() - 3600
        while self._spend and self._spend[0][0] < cutoff:
            self._spend.popleft()
        return sum(cost for _, cost in self._spend)

    def _charge(self, credits: int) -> None:
        if self._spent_this_hour() + credits > self._settings.credit_budget_per_hour:
            raise budget_exhausted(self._spent_this_hour(), self._settings.credit_budget_per_hour)
        self._spend.append((self._clock(), credits))
        self.credits_spent_total += credits
        CREDITS_SPENT.inc(credits)

    @property
    def credits_spent_this_hour(self) -> int:
        return self._spent_this_hour()

    @property
    def token_cached(self) -> bool:
        return self._tokens.cached()

    # --- requests -----------------------------------------------------------
    async def _authorized_get(self, path: str, params: dict[str, Any]) -> Any:
        """GET an authenticated endpoint, handling 401 and 429 explicitly."""
        for auth_attempt in range(2):
            token = await self._tokens.token()
            response = await self._http.request_json(
                "GET",
                path,
                params=params,
                headers={"Authorization": f"Bearer {token}"},
            )

            if response.status_code == 401:
                if auth_attempt == 0:
                    self._log.info("token_rejected_retrying", path=path)
                    self._tokens.invalidate()
                    continue
                raise upstream_auth_failed(401, "OpenSky")

            if response.status_code == 429:
                wait = retry_after_seconds_from(response.headers)
                remaining_header = response.headers.get("x-rate-limit-remaining")
                if wait is not None and wait <= self._settings.max_rate_limit_wait_s:
                    self._log.info("rate_limited_waiting", wait_s=wait, path=path)
                    await (self._sleep or _default_sleep)(wait)
                    response = await self._http.request_json(
                        "GET",
                        path,
                        params=params,
                        headers={"Authorization": f"Bearer {token}"},
                    )
                    if response.status_code == 429:
                        raise upstream_rate_limited("OpenSky", wait, remaining_header)
                else:
                    raise upstream_rate_limited("OpenSky", wait, remaining_header)

            if response.status_code == 404:
                return None

            if response.status_code >= 400:
                raise upstream_unavailable("OpenSky", f"HTTP {response.status_code} for {path}")

            remaining = credits_remaining_from(response.headers)
            if remaining is not None:
                self.last_credits_remaining = remaining
                CREDITS_REMAINING.set(float(remaining))
            return response.json_body

        raise upstream_auth_failed(401, "OpenSky")  # pragma: no cover

    async def states_in_bbox(self, bbox: BBox, *, max_vectors: int = 200) -> StatesSnapshot:
        cost = bbox.credit_cost
        self._charge(cost)
        body = await self._authorized_get("/states/all", bbox.as_params())
        if not isinstance(body, dict):
            raise upstream_unavailable("OpenSky", "states response was not a JSON object")

        rows = body.get("states") or []
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

        snapshot_time = body.get("time")
        return StatesSnapshot(
            time=snapshot_time if isinstance(snapshot_time, int) else None,
            vectors=vectors,
            credits_spent=cost,
            credits_remaining=self.last_credits_remaining,
            truncated=truncated,
            notes=notes,
            unreadable=len(rows) - len(readable),
            received=len(rows),
        )

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
        cost = 4  # live / < 24h window
        self._charge(cost)
        body = await self._authorized_get(
            f"/flights/{direction}",
            {"airport": airport_icao, "begin": begin, "end": end},
        )
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
            callsign = row.get("callsign")
            out.append(
                Movement(
                    icao24=str(row.get("icao24") or ""),
                    callsign=callsign.strip() if isinstance(callsign, str) else None,
                    first_seen=row.get("firstSeen"),
                    last_seen=row.get("lastSeen"),
                    est_departure_airport=row.get("estDepartureAirport"),
                    est_arrival_airport=row.get("estArrivalAirport"),
                )
            )
        return Movements(out, unreadable=len(body) - len(out), received=len(body))


def _is_state_row(row: object) -> bool:
    """A state vector is an array whose first field is the transponder address."""
    return isinstance(row, list) and bool(row) and isinstance(row[0], str) and bool(row[0])


async def _default_sleep(seconds: float) -> None:
    await asyncio.sleep(seconds)


def retry_after_seconds_from(headers: dict[str, str]) -> float | None:
    for header in ("x-rate-limit-retry-after-seconds", "retry-after"):
        raw = headers.get(header) or headers.get(header.title())
        if not raw:
            continue
        try:
            return max(0.0, float(raw))
        except ValueError:
            continue
    return None


def credits_remaining_from(headers: dict[str, str]) -> int | None:
    raw = headers.get("x-rate-limit-remaining") or headers.get("X-Rate-Limit-Remaining")
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None
