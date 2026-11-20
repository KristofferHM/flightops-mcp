"""AviationWeather.gov client: METAR and TAF.

This upstream needs no credentials, which makes it the honest baseline in the
repo: the server is useful with an empty `.env`, and the OAuth2 path is an
addition rather than a prerequisite. NOAA asks for a custom `User-Agent` and
returns **204 No Content** for a valid query with no data — a distinction the
tool layer must preserve, because "no observation" is not "the airport is fine".
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import httpx

from . import progress
from .config import Settings
from .errors import ToolFailure, upstream_unavailable
from .http import UpstreamHttp
from .licences import licensed
from .telemetry import get_logger

# This adapter's terms (licences.py). A source with no entry fails import here.
LICENCE = licensed("aviationweather")

# Flight-category thresholds are advisory: report them, never act on them alone.
IFR_CATEGORIES = frozenset({"IFR", "LIFR"})
# The other two flags. The triage's "why not higher" lines quote the same numbers.
GUST_FLAG_KT = 35
VISIBILITY_FLAG_SM = 1.5
SIGNIFICANT_WEATHER = ("TS", "FZ", "FG", "SN", "GR", "IC", "PL", "BLSN", "DS", "SS", "FC")


@dataclass(slots=True)
class Reports:
    """One product's reports, and NOAA's clock when it answered.

    `upstream_time` comes from the response's `Date` header and is None when it had
    none. A METAR's age is `upstream_time - observation_time`, so NOAA's clock judges
    NOAA's data and this machine's clock is not an input.
    """

    items: list[dict[str, Any]] = field(default_factory=list)
    upstream_time: int | None = None


# NOAA's limits on one /metar request. None is documented; each was measured live on
# 2026-11-19 (docs/DASHBOARD.md, "Weather requests, measured"):
#  * 400 records per answer. A longer one is cut at 400, silently, with 200 OK.
#  * 400 station ids. More gets 200 OK and {"status": "error", ...} instead of data.
#  * about 8 KB of URL, near 1,200 ids: nginx answers 414.
#  * one id outside [A-Z0-9], such as OurAirports' local code "CN-0062", makes NOAA
#    answer 400 for the whole request, so one such id cost 99 airports their weather.
# At hours=2 a station averages about 2.4 records, so the record cap binds first:
# 150 ids already lost stations. 50 keeps a chunk near 120 records, under a third of it.
METAR_CHUNK = 50
NOAA_RECORD_CAP = 400
# The request itself was refused, so a smaller one may pass: split until it does.
REJECTED_STATUS = frozenset({400, 413, 414})
STATION_ID = re.compile(r"[A-Z0-9]+")


@dataclass(frozen=True, slots=True)
class Failure:
    """Why one airport has no answer: a code the board shows, and the reason in words."""

    code: str
    message: str


@dataclass(slots=True)
class Batch:
    """Many stations' METARs. Every id asked ends in exactly one of the two maps.

    `reports` holds the ids NOAA answered for, with an empty list when it holds no
    observation; `failed` holds the ids it did not answer for, each with its own reason.
    """

    reports: dict[str, Reports] = field(default_factory=dict)
    failed: dict[str, Failure] = field(default_factory=dict)
    requests: int = 0


class _Rejected(Exception):
    """NOAA refused this request's shape or size. A smaller request may pass."""


class WeatherClient:
    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._http = UpstreamHttp(
            settings, "aviationweather", settings.avwx_base_url, transport=transport
        )
        self._log = get_logger(__name__)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> WeatherClient:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    async def metar(self, icao: str) -> list[dict[str, Any]]:
        return (await self.metar_reports(icao)).items

    async def taf(self, icao: str) -> list[dict[str, Any]]:
        return (await self.taf_reports(icao)).items

    async def metar_reports(self, icao: str) -> Reports:
        return await self._observations("metar", icao, hours=2)

    async def metar_batch(self, icaos: Sequence[str], chunk_size: int | None = None) -> Batch:
        """Many stations' METARs, one request per chunk, degrading per airport.

        A rejected chunk is halved until the part NOAA refuses is one id, so one bad
        id costs only itself. A chunk NOAA fails to answer is asked once more as two
        halves, then its airports are marked unavailable: a struggling upstream gets
        one second chance, not a bisection's worth of requests. The chunks go one at
        a time; the caller's concurrency gate is the only parallelism.
        """
        batch = Batch()
        askable: list[str] = []
        for code in dict.fromkeys(code.strip().upper() for code in icaos if code.strip()):
            if STATION_ID.fullmatch(code):
                askable.append(code)
            else:
                batch.failed[code] = Failure(
                    "station_rejected",
                    f"{code} is not an identifier NOAA accepts. One in a request makes it "
                    "refuse the whole request, so it was not asked about.",
                )
        size = chunk_size or METAR_CHUNK
        progress.expect(-(-len(askable) // size))
        for start in range(0, len(askable), size):
            await self._chunk(askable[start : start + size], batch, retried=False)
        return batch

    async def _counted_metars(self, ids: list[str]) -> Reports:
        """One METAR request, reported as progress whether NOAA answered it or not."""
        try:
            reports = await self._observations("metar", ",".join(ids), hours=2, batch=True)
        except asyncio.CancelledError:
            raise
        except Exception:
            await progress.upstream_call(
                f"NOAA did not answer a METAR request for {len(ids)} airport(s)."
            )
            raise
        await progress.upstream_call(f"NOAA answered a METAR request for {len(ids)} airport(s).")
        return reports

    async def _chunk(self, ids: list[str], batch: Batch, *, retried: bool) -> None:
        batch.requests += 1
        try:
            reports = await self._counted_metars(ids)
        except _Rejected as exc:
            if len(ids) == 1:
                batch.failed[ids[0]] = Failure(
                    "station_rejected", f"NOAA refused a request for {ids[0]} alone: {exc}."
                )
                return
            await self._halves(ids, batch, retried=retried)
            return
        except (ToolFailure, httpx.HTTPError) as exc:
            if not retried and len(ids) > 1:
                await self._halves(ids, batch, retried=True)
                return
            code, reason = _reason(exc)
            again = ", also when asked again at half size" if retried else ""
            for station in ids:
                batch.failed[station] = Failure(
                    code, f"NOAA did not answer for {station}{again}: {reason}"
                )
            return
        by_station: dict[str, list[dict[str, Any]]] = {code: [] for code in ids}
        for item in reports.items:
            station = str(item.get("station_id") or "").upper()
            if station in by_station:
                by_station[station].append(item)
        for code, items in by_station.items():
            batch.reports[code] = Reports(items, reports.upstream_time)

    async def _halves(self, ids: list[str], batch: Batch, *, retried: bool) -> None:
        middle = (len(ids) + 1) // 2
        progress.expect(2)  # two requests nobody planned for
        await self._chunk(ids[:middle], batch, retried=retried)
        await self._chunk(ids[middle:], batch, retried=retried)

    async def taf_reports(self, icao: str) -> Reports:
        # No `hours` parameter: AviationWeather.gov rejects it on /taf with
        # HTTP 400 "Unexpected query parameter provided" (verified live), while
        # /metar accepts it. Sending it anyway silently breaks TAF retrieval.
        return await self._observations("taf", icao, hours=None)

    async def _observations(
        self, product: str, icao: str, hours: int | None, *, batch: bool = False
    ) -> Reports:
        """One request. With `batch`, a refusal NOAA would accept smaller is `_Rejected`."""
        params: dict[str, object] = {"ids": icao.upper(), "format": "json"}
        if hours is not None:
            params["hours"] = hours
        response = await self._http.request_json(
            "GET",
            f"/{product}",
            params=params,
            allow_status=frozenset({204}),
        )
        upstream_time = response.upstream_time_epoch
        if response.status_code == 204:
            return Reports([], upstream_time)
        body = response.json_body
        if batch and response.status_code in REJECTED_STATUS:
            raise _Rejected(f"HTTP {response.status_code}{_noaa_error(body)}")
        if response.status_code >= 400:
            # A batch's ids would make its reason a hundred codes long.
            asked = f"{icao.count(',') + 1} stations" if batch else icao
            raise upstream_unavailable(
                "AviationWeather.gov", f"HTTP {response.status_code} for {product} {asked}"
            )
        if body is None:
            return Reports([], upstream_time)
        if isinstance(body, dict) and body.get("status") == "error":
            # NOAA's answer to too many ids is 200 OK with this body. Read as a report,
            # it became one row with no station, and every airport read "no METAR".
            if batch:
                raise _Rejected(f"HTTP 200{_noaa_error(body)}")
            raise upstream_unavailable("AviationWeather.gov", f"error{_noaa_error(body)}")
        if isinstance(body, dict):
            body = [body]
        if not isinstance(body, list):
            raise upstream_unavailable("AviationWeather.gov", "unexpected response shape")
        if batch and len(body) >= NOAA_RECORD_CAP and "," in icao:
            # Exactly the cap means NOAA cut the answer: the missing stations would
            # otherwise read "no observation", which is a claim about the weather.
            raise _Rejected(f"the answer stopped at NOAA's cap of {NOAA_RECORD_CAP} records")

        out: list[dict[str, Any]] = []
        for item in body:
            if not isinstance(item, dict):
                continue
            out.append(
                {
                    "station_id": item.get("station_id") or item.get("icaoId"),
                    "observation_time": item.get("observation_time") or item.get("reportTime"),
                    "raw_text": item.get("raw_text") or item.get("rawOb"),
                    "flight_category": item.get("flight_category") or item.get("fltCat"),
                    "temperature_c": _number(item.get("temp")),
                    "dewpoint_c": _number(item.get("dewp")),
                    "wind_dir_deg": _number(item.get("wdir")),
                    "wind_variable": str(item.get("wdir") or "").strip().upper() == "VRB",
                    "wind_speed_kt": _number(item.get("wspd")),
                    "wind_gust_kt": _number(item.get("wgst")),
                    "visibility_statute_mi": _number(item.get("visib")),
                    # NOAA writes "6+" for "6 or more": the number is a floor, not a reading.
                    "visibility_at_least": str(item.get("visib") or "").strip().endswith("+"),
                    "altimeter_hpa": _number(item.get("altim")),
                    "wx_string": item.get("wx_string") or item.get("wxString"),
                    # None when NOAA sent no `clouds` at all, [] when it sent an empty list:
                    # "not reported" and "no layers" are different statements.
                    "clouds": _layers(item.get("clouds")),
                    "vertical_visibility_ft": _number(item.get("vertVis")),
                    "product": product,
                }
            )
        return Reports(out, upstream_time)


def _reason(exc: ToolFailure | httpx.HTTPError) -> tuple[str, str]:
    """The code and the words a failed chunk leaves on each of its airports."""
    if isinstance(exc, httpx.HTTPError):
        return "upstream_unavailable", type(exc).__name__
    detail = exc.details.get("detail")
    return exc.code, f"{exc.message} ({detail})" if detail else exc.message


def _noaa_error(body: object) -> str:
    """NOAA's own words for a refusal, when its body carried any."""
    if isinstance(body, dict) and isinstance(body.get("error"), str):
        return f' "{body["error"][:200]}"'
    return ""


def _number(value: object) -> float | None:
    """Coerce NOAA's sometimes-stringly numerics.

    Live data returns `"visib": "6+"` and numeric strings, so a strict isinstance
    check silently drops real observations — which is how a flag goes missing.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        stripped = value.strip().rstrip("+").strip()
        match = re.match(r"^-?\d+(?:\.\d+)?", stripped)
        if match:
            return float(match.group(0))
    return None


def _layers(value: object) -> list[dict[str, Any]] | None:
    """NOAA's `clouds`, as cover and base in feet. A layer it cannot read is left out."""
    if not isinstance(value, list):
        return None
    layers: list[dict[str, Any]] = []
    for layer in value:
        if not isinstance(layer, dict) or not isinstance(layer.get("cover"), str):
            continue
        base = _number(layer.get("base"))
        layers.append({"cover": layer["cover"].strip().upper(), "base_ft": base})
    return layers


# A ceiling is the lowest broken or overcast layer, or a vertical visibility.
CEILING_COVERS = frozenset({"BKN", "OVC", "OVX"})


def ceiling(observation: dict[str, Any]) -> dict[str, Any]:
    """The ceiling in feet and what it was read from, or why there is no number.

    "No broken or overcast layer" and "not in the report" both give no number, and
    they mean different things: the first is a ceiling above every reported layer,
    the second is a ceiling nobody told us about.
    """
    clouds = observation.get("clouds")
    vertical = observation.get("vertical_visibility_ft")
    covering = [
        layer
        for layer in clouds or []
        if layer.get("cover") in CEILING_COVERS and isinstance(layer.get("base_ft"), int | float)
    ]
    if covering:
        lowest = min(covering, key=lambda layer: float(layer["base_ft"]))
        return {"ft": int(lowest["base_ft"]), "basis": lowest["cover"]}
    if isinstance(vertical, int | float):
        return {"ft": int(vertical), "basis": "VV"}
    if clouds is None:
        return {"ft": None, "basis": "not in the report"}
    return {"ft": None, "basis": "no broken or overcast layer"}


def _fmt(value: float) -> str:
    """Render a number the way a METAR reader expects: 45, not 45.0."""
    return f"{value:g}"


def fleet_impact_flags(observations: list[dict[str, Any]]) -> list[str]:
    """Turn observations into *flags*, not verdicts.

    An agent that reads "LIFR" and says "airport closed" is the exact failure
    mode this project exists to prevent, so each flag names the observation and
    its limit, and an empty list means "nothing flagged", not "all clear".
    """
    flags: list[str] = []
    for obs in observations:
        station = obs.get("station_id") or "?"
        category = obs.get("flight_category")
        if category in IFR_CATEGORIES:
            flags.append(f"{station}: flight category {category}")
        gust = obs.get("wind_gust_kt")
        if isinstance(gust, int | float) and gust >= GUST_FLAG_KT:
            flags.append(f"{station}: gusts {_fmt(gust)} kt")
        visibility = obs.get("visibility_statute_mi")
        if isinstance(visibility, int | float) and visibility < VISIBILITY_FLAG_SM:
            flags.append(f"{station}: visibility {_fmt(visibility)} sm")
        wx = obs.get("wx_string")
        if isinstance(wx, str) and any(token in wx.upper() for token in SIGNIFICANT_WEATHER):
            flags.append(f"{station}: significant weather {wx}")
    return flags
