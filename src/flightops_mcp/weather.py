"""AviationWeather.gov client: METAR and TAF.

This upstream needs no credentials, which makes it the honest baseline in the
repo: the server is useful with an empty `.env`, and the OAuth2 path is an
addition rather than a prerequisite. NOAA asks for a custom `User-Agent` and
returns **204 No Content** for a valid query with no data — a distinction the
tool layer must preserve, because "no observation" is not "the airport is fine".
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import httpx

from .config import Settings
from .errors import upstream_unavailable
from .http import UpstreamHttp
from .telemetry import get_logger

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

    async def metar_reports_many(self, icaos: Sequence[str]) -> Reports:
        """Several stations in one request. NOAA takes a comma-separated `ids`."""
        return await self._observations("metar", ",".join(icaos), hours=2)

    async def taf_reports(self, icao: str) -> Reports:
        # No `hours` parameter: AviationWeather.gov rejects it on /taf with
        # HTTP 400 "Unexpected query parameter provided" (verified live), while
        # /metar accepts it. Sending it anyway silently breaks TAF retrieval.
        return await self._observations("taf", icao, hours=None)

    async def _observations(self, product: str, icao: str, hours: int | None) -> Reports:
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
        if response.status_code >= 400:
            raise upstream_unavailable(
                "AviationWeather.gov", f"HTTP {response.status_code} for {product} {icao}"
            )
        body = response.json_body
        if body is None:
            return Reports([], upstream_time)
        if isinstance(body, dict):
            body = [body]
        if not isinstance(body, list):
            raise upstream_unavailable("AviationWeather.gov", "unexpected response shape")

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
