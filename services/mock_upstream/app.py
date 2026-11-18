"""A mock of the three third-party upstreams, in the shapes their vendors document.

    uv run uvicorn --factory services.mock_upstream.app:create_app --port 9200

It is a fixture, never deployed, and it exists for two jobs:

* **The second adapter's proof.** `/schiphol/...` implements the auth shape in
  Schiphol's API Consumer Migration Guide (2026-07-20): an Auth0-style token
  endpoint that requires `audience` and answers `expires_in`, and a flights
  resource that accepts only a bearer token issued for that audience and not yet
  expired. It is strict where the vendor says it is strict, and it counts what a
  client got wrong (a wrong audience, an expired token, the retired `app_id`/
  `app_key`/`ResourceVersion` headers) so a test can assert those counts are zero.
* **The load test.** `/opensky/...` and `/noaa/...` answer in OpenSky's and
  AviationWeather's documented shapes, so the server can be driven at a fixed
  rate with no third party in the loop and no credits spent anywhere real.

What it cannot prove: that the vendors still behave this way. It is a mock of
the documentation, not of the service.

Client secrets arrive by environment (`MOCK_SCHIPHOL_CLIENT_SECRET`,
`MOCK_OPENSKY_CLIENT_SECRET`). With none set, the token endpoints refuse every
client: a mock with a default secret in its source is a secret in the repository.
"""

from __future__ import annotations

import asyncio
import math
import secrets
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from pydantic_settings import BaseSettings, SettingsConfigDict

SCHIPHOL_PAGE_SIZE = 20
LEGACY_HEADERS = ("app_id", "app_key", "resourceversion")


class MockUpstreamConfig(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MOCK_", extra="ignore")

    schiphol_client_id: str = "flightops-schiphol"
    schiphol_client_secret: str | None = None
    schiphol_audience: str = "https://api.schiphol.nl/public"
    schiphol_token_ttl_s: int = 1800
    opensky_client_id: str = "flightops-opensky"
    opensky_client_secret: str | None = None
    opensky_token_ttl_s: int = 1800
    # Simulated network and processing time per data request, in milliseconds.
    latency_ms: float = 0.0
    # Aircraft the mock "hears" per /states/all answer.
    aircraft: int = 12
    # The credit balance OpenSky reports, counted down per request.
    opensky_daily_credits: int = 4000


@dataclass(slots=True)
class _Issued:
    audience: str | None
    expires_at: float


class _Tokens:
    def __init__(self, clock: Callable[[], float]) -> None:
        self._clock = clock
        self._issued: dict[str, _Issued] = {}

    def issue(self, audience: str | None, ttl_s: int) -> str:
        token = secrets.token_urlsafe(32)
        self._issued[token] = _Issued(audience, self._clock() + ttl_s)
        return token

    def check(self, request: Request, audience: str | None) -> str | None:
        """None if the bearer is good for `audience`, else the reason it is not."""
        scheme, _, token = request.headers.get("authorization", "").partition(" ")
        if scheme.lower() != "bearer" or not token:
            return "missing_token"
        issued = self._issued.get(token)
        if issued is None:
            return "unknown_token"
        if issued.audience != audience:
            return "wrong_audience"
        if self._clock() >= issued.expires_at:
            return "expired_token"
        return None


@dataclass(slots=True)
class _Mock:
    """What every route group shares: the config, the clock, the tokens and the counters."""

    app: FastAPI
    config: MockUpstreamConfig
    clock: Callable[[], float]
    tokens: _Tokens
    stats: Counter[str]

    async def latency(self) -> None:
        if self.config.latency_ms > 0:
            await asyncio.sleep(self.config.latency_ms / 1000)

    async def client_credentials(
        self, request: Request, prefix: str, client_id: str, secret: str | None
    ) -> tuple[dict[str, str], JSONResponse | None]:
        form = {key: str(value) for key, value in (await request.form()).items()}
        self.stats[f"{prefix}.token_requests"] += 1
        if form.get("grant_type") != "client_credentials":
            return form, _refused(400, "invalid_grant", "grant_type must be client_credentials")
        if secret is None or form.get("client_id") != client_id:
            return form, _refused(401, "invalid_client", "unknown client")
        if not secrets.compare_digest(form.get("client_secret", ""), secret):
            return form, _refused(401, "invalid_client", "wrong client secret")
        return form, None


def create_app(
    config: MockUpstreamConfig | None = None, *, clock: Callable[[], float] = time.time
) -> FastAPI:
    resolved = config or MockUpstreamConfig()
    app = FastAPI(title="flightops mock upstreams", docs_url=None, redoc_url=None, openapi_url=None)
    # Everything a test or the load test wants to count, by name.
    stats: Counter[str] = Counter()
    app.state.stats = stats
    app.state.bearers = []  # every bearer a data endpoint was sent, for passthrough checks
    mock = _Mock(app=app, config=resolved, clock=clock, tokens=_Tokens(clock), stats=stats)
    _schiphol(mock)
    _opensky(mock)
    _noaa(mock)

    @app.get("/stats")
    async def read_stats() -> Response:
        return JSONResponse(dict(sorted(stats.items())))

    @app.get("/healthz")
    async def healthz() -> Response:
        return JSONResponse({"status": "ok"})

    return app


def _refused(status: int, error: str, description: str) -> JSONResponse:
    return JSONResponse({"error": error, "error_description": description}, status_code=status)


def _schiphol(mock: _Mock) -> None:
    """Auth0 client credentials with an audience, and the v4 flights resource."""
    app, config, stats = mock.app, mock.config, mock.stats

    @app.post("/schiphol/oauth/token")
    async def schiphol_token(request: Request) -> Response:
        form, problem = await mock.client_credentials(
            request, "schiphol", config.schiphol_client_id, config.schiphol_client_secret
        )
        if problem is not None:
            return problem
        if "audience" not in form:
            stats["schiphol.token_without_audience"] += 1
            return _refused(400, "invalid_request", "audience is required")
        if form["audience"] != config.schiphol_audience:
            stats["schiphol.token_wrong_audience"] += 1
            return _refused(403, "access_denied", "Service not enabled within domain")
        stats["schiphol.tokens_issued"] += 1
        token = mock.tokens.issue(form["audience"], config.schiphol_token_ttl_s)
        return JSONResponse(
            {
                "access_token": token,
                "token_type": "Bearer",
                "expires_in": config.schiphol_token_ttl_s,
            }
        )

    @app.get("/schiphol/public/public-flights/v4/flights")
    async def schiphol_flights(request: Request) -> Response:
        stats["schiphol.flight_requests"] += 1
        if any(name in request.headers for name in LEGACY_HEADERS):
            stats["schiphol.legacy_headers"] += 1
        app.state.bearers.append(request.headers.get("authorization", ""))
        reason = mock.tokens.check(request, config.schiphol_audience)
        if reason is not None:
            stats[f"schiphol.refused.{reason}"] += 1
            return _refused(401, "invalid_token", reason)
        await mock.latency()
        params = request.query_params
        direction = params.get("flightDirection")
        try:
            begin = _local(params["fromDateTime"])
            end = _local(params["toDateTime"])
            page = int(params.get("page", "0"))
        except (KeyError, ValueError):
            return _refused(400, "invalid_request", "fromDateTime, toDateTime and page")
        if direction not in {"A", "D"} or end <= begin or page < 0:
            return _refused(400, "invalid_request", "flightDirection, window or page")
        flights = _schedule(direction, begin, end)
        window = flights[page * SCHIPHOL_PAGE_SIZE : (page + 1) * SCHIPHOL_PAGE_SIZE]
        if not window:
            return Response(status_code=204)
        headers = {}
        if (page + 1) * SCHIPHOL_PAGE_SIZE < len(flights):
            headers["Link"] = f'<{request.url.include_query_params(page=page + 1)}>; rel="next"'
        return JSONResponse({"flights": window}, headers=headers)


def _opensky(mock: _Mock) -> None:
    """Client credentials without an audience, credits, and the documented array formats."""
    app, config, stats = mock.app, mock.config, mock.stats

    @app.post("/opensky/token")
    async def opensky_token(request: Request) -> Response:
        _, problem = await mock.client_credentials(
            request, "opensky", config.opensky_client_id, config.opensky_client_secret
        )
        if problem is not None:
            return problem
        stats["opensky.tokens_issued"] += 1
        token = mock.tokens.issue(None, config.opensky_token_ttl_s)
        return JSONResponse({"access_token": token, "expires_in": config.opensky_token_ttl_s})

    def refusal(request: Request) -> JSONResponse | None:
        app.state.bearers.append(request.headers.get("authorization", ""))
        reason = mock.tokens.check(request, None)
        if reason is None:
            return None
        stats[f"opensky.refused.{reason}"] += 1
        return _refused(401, "invalid_token", reason)

    def credits_header(cost: int) -> dict[str, str]:
        stats["opensky.credits"] += cost
        remaining = max(0, config.opensky_daily_credits - stats["opensky.credits"])
        return {"X-Rate-Limit-Remaining": str(remaining)}

    @app.get("/opensky/api/states/all")
    async def opensky_states(request: Request) -> Response:
        stats["opensky.states_requests"] += 1
        if (problem := refusal(request)) is not None:
            return problem
        await mock.latency()
        try:
            box = [float(request.query_params[k]) for k in ("lamin", "lomin", "lamax", "lomax")]
        except (KeyError, ValueError):
            return _refused(400, "invalid_request", "lamin, lomin, lamax, lomax")
        now = int(mock.clock())
        area = (box[2] - box[0]) * (box[3] - box[1])
        cost = 1 if area <= 25 else 2 if area <= 100 else 3 if area <= 400 else 4
        return JSONResponse(
            {"time": now, "states": _states(box, now, config.aircraft)},
            headers=credits_header(cost),
        )

    @app.get("/opensky/api/flights/{direction}")
    async def opensky_flights(direction: str, request: Request) -> Response:
        stats[f"opensky.{direction}_requests"] += 1
        if (problem := refusal(request)) is not None:
            return problem
        await mock.latency()
        airport = request.query_params.get("airport", "")
        end = int(request.query_params.get("end", "0"))
        rows = [
            {
                "icao24": f"4ca{n:03x}",
                "callsign": f"SAS{100 + n:<5}",
                "firstSeen": end - 3600 + n * 600,
                "lastSeen": end - 3000 + n * 600,
                "estDepartureAirport": "ESSA" if direction == "arrival" else airport,
                "estArrivalAirport": airport if direction == "arrival" else "EKCH",
            }
            for n in range(4)
        ]
        return JSONResponse(rows, headers=credits_header(4))


def _noaa(mock: _Mock) -> None:
    """AviationWeather.gov: no auth, METAR and TAF as JSON arrays."""

    @mock.app.get("/noaa/api/data/{product}")
    async def noaa(product: str, request: Request) -> Response:
        mock.stats[f"noaa.{product}_requests"] += 1
        await mock.latency()
        icao = request.query_params.get("ids", "")
        observed = datetime.fromtimestamp(mock.clock() - 600, tz=UTC)
        if product == "metar":
            return JSONResponse(
                [
                    {
                        "icaoId": icao,
                        "reportTime": observed.strftime("%Y-%m-%dT%H:%M:%SZ"),
                        "rawOb": f"{icao} {observed:%d%H%M}Z 24008KT 9999 BKN030 08/05 Q1008",
                        "fltCat": "VFR",
                        "wspd": 8,
                        "visib": "6+",
                    }
                ]
            )
        if product == "taf":
            return JSONResponse([{"icaoId": icao, "rawTAF": f"TAF {icao} 24010KT 9999 SCT030"}])
        return Response(status_code=404)


def _local(stamp: str) -> datetime:
    """Schiphol's local `yyyy-MM-ddTHH:mm:ss`, kept naive: only the order matters here."""
    return datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%S")


def _schedule(direction: str, begin: datetime, end: datetime) -> list[dict[str, Any]]:
    """One flight every ten minutes on the hour grid, so a window's contents are deterministic.

    Every seventh is cancelled, every fifth runs 25 minutes late. Times carry a +01:00
    offset because the mock pretends it is winter; it is not the client's tz logic.
    """
    out: list[dict[str, Any]] = []
    slot = begin.replace(second=0, microsecond=0) + timedelta(minutes=(-begin.minute) % 10)
    number = 0
    while slot <= end:
        number += 1
        designator = 1000 + slot.hour * 6 + slot.minute // 10
        scheduled = slot.strftime("%Y-%m-%dT%H:%M:%S.000+01:00")
        late = (slot + timedelta(minutes=25)).strftime("%Y-%m-%dT%H:%M:%S.000+01:00")
        states = ["CNX"] if designator % 7 == 0 else ["SCH"]
        estimate = late if designator % 5 == 0 else scheduled
        row: dict[str, Any] = {
            "flightName": f"KL{designator}",
            "flightDirection": direction,
            "scheduleDateTime": scheduled,
            "publicFlightState": {"flightStates": states},
            "route": {"destinations": ["LHR" if designator % 2 else "CPH"]},
        }
        row["estimatedLandingTime" if direction == "A" else "publicEstimatedOffBlockTime"] = (
            estimate
        )
        out.append(row)
        slot += timedelta(minutes=10)
    return out


def _states(box: list[float], now: int, count: int) -> list[list[Any]]:
    """`count` aircraft on a circle inside the box, in OpenSky's 17-field array form."""
    lamin, lomin, lamax, lomax = box
    lat0, lon0 = (lamin + lamax) / 2, (lomin + lomax) / 2
    rows: list[list[Any]] = []
    for n in range(count):
        angle = 2 * math.pi * n / max(1, count)
        lat = lat0 + (lamax - lamin) / 4 * math.sin(angle)
        lon = lon0 + (lomax - lomin) / 4 * math.cos(angle)
        on_ground = n % 4 == 0
        rows.append(
            [
                f"4b1{n:03x}",
                f"SAS{200 + n:<5}",
                "Sweden",
                now - 2,
                now - 1,
                round(lon, 4),
                round(lat, 4),
                None if on_ground else 3000.0 + 100 * n,
                on_ground,
                0.0 if on_ground else 180.0,
                90.0,
                0.0,
                None,
                None,
                "1000",
                False,
                0,
            ]
        )
    return rows
