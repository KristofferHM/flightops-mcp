"""adsb.lol: live traffic under the Open Database License. Licence class B.

The only traffic source in the checked table whose terms permit commercial use
without a contract: ODbL 1.0 asks for attribution, and for a derived database used
publicly to be offered under ODbL (docs/COMMERCIAL.md says what "derived" means
here). Both duties are carried by the payload, not by this client.

The API (`https://api.adsb.lol/docs`, OpenAPI version 0.0.2, read 2026-11-20):

* `GET /v2/point/{lat}/{lon}/{radius}`: aircraft within `radius` nautical miles,
  at most 250. The answer is readsb's `aircraft.json` shape: `ac`, and `now` in
  Unix **milliseconds**.
* Altitudes are feet (`alt_baro` is the string `"ground"` on the surface), speeds
  knots, rates feet per minute. They are converted here to OpenSky's units, so a
  traffic payload means the same thing whichever source answered.
* Without a key, 500 requests a day per IP. Over quota is `429` with `Retry-After`,
  which the shared policy in `http.py` handles like any other upstream's.

A box is asked as the circle around it and cut back to the box, so
`airport_live_traffic` answers the same question with either source.
"""

from __future__ import annotations

import asyncio
import math
import re
import time
from collections.abc import Callable
from typing import Any, Final

import httpx

from .config import Settings
from .credits import CreditLedger
from .errors import ToolFailure, upstream_unavailable
from .http import UpstreamHttp, authorized_get
from .licences import licensed
from .opensky import BBox, StatesSnapshot, StateVector

LICENCE: Final = licensed("adsblol")
UPSTREAM: Final = "adsb.lol"
MAX_RADIUS_NM: Final = 250
FT_TO_M: Final = 0.3048
KT_TO_MS: Final = 1852.0 / 3600.0
FPM_TO_MS: Final = FT_TO_M / 60.0
# A leading `~` is adsb.lol's mark for an address that is not an ICAO one (TIS-B).
HEX: Final = re.compile(r"~?[0-9a-f]{6}")
# adsb.lol's published allowance for a caller without a key.
UNKEYED_DAILY_REQUESTS: Final = 500


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value) if math.isfinite(value) else None


def _scaled(value: object, factor: float) -> float | None:
    number = _number(value)
    return None if number is None else round(number * factor, 2)


def _vector(row: dict[str, Any], now: int | None) -> StateVector:
    altitude = row.get("alt_baro")
    seen = _number(row.get("seen"))
    flight = row.get("flight")
    callsign = flight.strip() or None if isinstance(flight, str) else None
    return StateVector(
        icao24=str(row["hex"]).lower(),
        callsign=callsign,
        origin_country=None,
        longitude=_number(row.get("lon")),
        latitude=_number(row.get("lat")),
        baro_altitude_m=None if altitude == "ground" else _scaled(altitude, FT_TO_M),
        on_ground=altitude == "ground",
        velocity_ms=_scaled(row.get("gs"), KT_TO_MS),
        true_track_deg=_number(row.get("track")),
        vertical_rate_ms=_scaled(row.get("baro_rate"), FPM_TO_MS),
        last_contact=None if now is None or seen is None else int(now - seen),
    )


def _inside(vector: StateVector, bbox: BBox) -> bool:
    if vector.latitude is None or vector.longitude is None:
        return False
    return (
        bbox.lamin <= vector.latitude <= bbox.lamax and bbox.lomin <= vector.longitude <= bbox.lomax
    )


def decode_point(body: object, *, bbox: BBox, max_vectors: int) -> StatesSnapshot:
    """`/v2/point` as a snapshot of the aircraft inside `bbox`.

    A row that is not an aircraft is counted in `unreadable`. An aircraft outside
    the box, or with no position, was not asked about and is left out of
    `received`; a note says how many.
    """
    if not isinstance(body, dict):
        raise upstream_unavailable(UPSTREAM, "point response was not a JSON object")
    rows = body.get("ac")
    if rows is None:
        rows = []
    if not isinstance(rows, list):
        raise upstream_unavailable(UPSTREAM, "ac field was not a list")
    now_ms = body.get("now")
    now = (
        int(now_ms) // 1000
        if isinstance(now_ms, int) and not isinstance(now_ms, bool) and now_ms > 0
        else None
    )
    readable = [r for r in rows if isinstance(r, dict) and HEX.fullmatch(str(r.get("hex", "")))]
    vectors = [_vector(row, now) for row in readable]
    inside = [v for v in vectors if _inside(v, bbox)]
    notes: list[str] = []
    if len(inside) < len(vectors):
        notes.append(
            f"{len(vectors) - len(inside)} aircraft heard in the circle around the box were "
            "outside it or had no position, and are not counted."
        )
    inside.sort(key=lambda v: v.last_contact or 0, reverse=True)
    truncated = len(inside) > max_vectors
    if truncated:
        notes.append(
            f"{len(inside)} aircraft seen; returning the {max_vectors} most recently heard."
        )
    unreadable = len(rows) - len(readable)
    return StatesSnapshot(
        time=now,
        vectors=inside[:max_vectors],
        credits_spent=0,
        credits_remaining=None,
        truncated=truncated,
        notes=notes,
        unreadable=unreadable,
        received=len(inside) + unreadable,
    )


def covering_radius_nm(bbox: BBox) -> float:
    """The radius of the circle through the box's corners, in nautical miles."""
    centre = (bbox.lamin + bbox.lamax) / 2
    half_lat = (bbox.lamax - bbox.lamin) / 2 * 60.0
    half_lon = (bbox.lomax - bbox.lomin) / 2 * 60.0 * max(0.05, math.cos(math.radians(centre)))
    return math.hypot(half_lat, half_lon)


class AdsbLolClient:
    """One endpoint, one request per answer, charged to a local hourly ceiling."""

    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Any] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._settings = settings
        self._sleep = sleep or asyncio.sleep
        self._http = UpstreamHttp(
            settings,
            "adsblol",
            settings.adsblol_base_url,
            transport=transport,
            sleep=self._sleep,
        )
        self._ledger = CreditLedger(settings.adsblol_requests_per_hour, clock=clock)

    async def aclose(self) -> None:
        await self._http.aclose()

    @property
    def credentials_mode(self) -> str:
        return "api_key" if self._settings.adsblol_api_key else "none"

    @property
    def requests_this_hour(self) -> int:
        return self._ledger.spent()

    @property
    def budget_per_hour(self) -> int:
        return self._ledger.budget

    def freshness(self) -> dict[str, Any]:
        """What adsb.lol promises about an answer: the latest positions, nothing older."""
        return {
            "credentials": self.credentials_mode,
            "time_resolution_s": None,
            "most_recent_only": True,
            "time_parameter_honoured": False,
            "upstream_daily_requests": (
                None if self._settings.adsblol_api_key else UNKEYED_DAILY_REQUESTS
            ),
        }

    async def states_in_bbox(self, bbox: BBox, *, max_vectors: int = 200) -> StatesSnapshot:
        spent = self._ledger.spent()
        if spent + 1 > self._ledger.budget:
            raise ToolFailure(
                code="local_budget_exhausted",
                message=f"The local adsb.lol guard stopped this call ({spent}/"
                f"{self._ledger.budget} requests used this hour).",
                hint="adsb.lol allows 500 requests a day per IP without a key. Wait, or raise "
                "FLIGHTOPS_ADSBLOL_REQUESTS_PER_HOUR deliberately.",
                details={"requests_this_hour": spent, "budget": self._ledger.budget},
            )
        self._ledger.charge(1)
        centre_lat = round((bbox.lamin + bbox.lamax) / 2, 4)
        centre_lon = round((bbox.lomin + bbox.lomax) / 2, 4)
        wanted = covering_radius_nm(bbox)
        radius = min(MAX_RADIUS_NM, math.ceil(wanted))
        key = self._settings.adsblol_api_key
        response = await authorized_get(
            self._http,
            None,
            f"/v2/point/{centre_lat}/{centre_lon}/{radius}",
            {},
            upstream=UPSTREAM,
            settings=self._settings,
            sleep=self._sleep,
            extra_headers={"x-api-key": key.get_secret_value()} if key else None,
        )
        snapshot = decode_point(response.json_body, bbox=bbox, max_vectors=max_vectors)
        snapshot.upstream_time = response.upstream_time_epoch
        if wanted > MAX_RADIUS_NM:
            snapshot.notes.append(
                f"adsb.lol answers at most {MAX_RADIUS_NM} nm from a point; the box's corners "
                "beyond that were not asked about."
            )
        return snapshot
