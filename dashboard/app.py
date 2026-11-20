"""The FastAPI app: one port, short JSON requests, static files. No streams.

Polling only, on purpose: through a cloudflared quick tunnel, long-lived SSE and
WebSocket connections are cut. A short request that fails is simply retried.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path as FilePath
from typing import Annotated, Any, Final, Literal
from urllib.parse import parse_qs, urlsplit

from fastapi import Depends, FastAPI, HTTPException, Path, Query, Request, Response
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from .aircraft import CAPS, DEFAULT_CAP, RADII_NM, Selection
from .board import MAX_SNAPSHOT_EPOCH
from .gate import COOKIE_NAME, LOCAL_HOSTS, SESSION_TTL_S, Gate, host_name
from .inbound import DRAWN_MAX
from .service import (
    DEFAULT_REGION,
    LIST_PAGE,
    LIST_PAGE_MAX,
    BoardService,
    UnknownBoardItem,
    View,
)

STATIC: Final = FilePath(__file__).parent / "static"
MAX_LOGIN_BODY: Final = 4096
OPEN_PATHS: Final = frozenset({"/login", "/login.css", "/healthz", "/favicon.ico"})
# Everything is served from here. This is also what keeps a CDN from creeping back in:
# the browser would refuse to load one.
CONTENT_SECURITY_POLICY: Final = (
    "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; "
    "connect-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
)
LOGIN_PAGE: Final = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Airport board: sign in</title><link rel="stylesheet" href="/login.css"></head>
<body><main><h1>Airport board</h1>
<form method="post" action="/login">
<label for="passcode">Passcode</label>
<input id="passcode" name="passcode" type="password" autocomplete="current-password"
 required autofocus>
<button type="submit">Open the board</button></form>{message}</main></body></html>"""

Next = Callable[[Request], Awaitable[Response]]


def _login_page(message: str = "", status_code: int = 200) -> HTMLResponse:
    note = f'<p role="alert">{message}</p>' if message else ""
    return HTMLResponse(LOGIN_PAGE.format(message=note), status_code=status_code)


def _same_origin(request: Request) -> bool:
    origin = request.headers.get("origin")
    return origin is None or urlsplit(origin).netloc == request.headers.get("host")


def create_app(
    service: BoardService,
    gate: Gate,
    *,
    on_close: Callable[[], Awaitable[None]] | None = None,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        yield
        if on_close is not None:
            await on_close()

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)

    async def guard(request: Request, call_next: Next) -> Response:
        if not gate.enabled and host_name(request.headers.get("host", "")) not in LOCAL_HOSTS:
            return Response("Set a passcode to reach this board remotely.", status_code=403)
        if request.method == "POST" and not _same_origin(request):
            return Response("Cross-origin request refused.", status_code=403)
        path = request.url.path
        if (
            gate.enabled
            and path not in OPEN_PATHS
            and not gate.valid(request.cookies.get(COOKIE_NAME))
        ):
            if path.startswith("/api/"):
                return JSONResponse({"error": "sign_in_required"}, status_code=401)
            return RedirectResponse("/login", status_code=303)
        return await call_next(request)

    async def headers(request: Request, call_next: Next) -> Response:
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = CONTENT_SECURITY_POLICY
        response.headers["X-Content-Type-Options"] = "nosniff"
        # Not `no-referrer`: with it, browsers send `Origin: null` on a form post, and
        # the same-origin check above would refuse the board's own login form.
        response.headers["Referrer-Policy"] = "same-origin"
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    # Added last runs first, so even a refusal from the gate carries the headers.
    app.middleware("http")(guard)
    app.middleware("http")(headers)
    # A continent's airports are a few hundred kB of JSON every 30 s; compressed, a tenth.
    app.add_middleware(GZipMiddleware, minimum_size=2048)

    @app.exception_handler(UnknownBoardItem)
    async def not_listed(_request: Request, _exc: UnknownBoardItem) -> JSONResponse:
        return JSONResponse({"error": "not_on_this_board"}, status_code=404)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/favicon.ico")
    async def favicon() -> Response:
        return Response(status_code=204)

    @app.get("/login", response_model=None)
    async def login_form() -> Response:
        return RedirectResponse("/", status_code=303) if not gate.enabled else _login_page()

    @app.get("/login.css")
    async def login_css() -> FileResponse:
        return FileResponse(STATIC / "login.css", media_type="text/css")

    @app.post("/login", response_model=None)
    async def login(request: Request) -> Response:
        if not gate.enabled:
            return RedirectResponse("/", status_code=303)
        if gate.locked_out():
            return _login_page("Too many attempts. Wait a minute.", 429)
        body = await request.body()
        if len(body) > MAX_LOGIN_BODY:
            return _login_page("That passcode is not right.", 401)
        fields = parse_qs(body.decode("utf-8", "replace"), keep_blank_values=True)
        if not gate.attempt(fields.get("passcode", [""])[0]):
            return _login_page("That passcode is not right.", 401)
        response = RedirectResponse("/", status_code=303)
        forwarded = request.headers.get("x-forwarded-proto", request.url.scheme)
        response.set_cookie(
            COOKIE_NAME,
            gate.issue(),
            max_age=SESSION_TTL_S,
            httponly=True,
            samesite="strict",
            secure=forwarded == "https",
        )
        return response

    @app.post("/logout")
    async def logout() -> RedirectResponse:
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie(COOKIE_NAME)
        return response

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(STATIC / "index.html", media_type="text/html")

    @app.get("/api/board")
    async def api_board(
        *,
        region: Annotated[str, Query(max_length=32)] = DEFAULT_REGION,
        traffic_snapshot: Annotated[int | None, Query(ge=0, le=MAX_SNAPSHOT_EPOCH)] = None,
        traffic_upstream: Annotated[int | None, Query(ge=0, le=MAX_SNAPSHOT_EPOCH)] = None,
        lat: Annotated[float | None, Query(ge=-90, le=90)] = None,
        lon: Annotated[float | None, Query(ge=-180, le=180)] = None,
        radius: Annotated[float | None, Query(gt=0, le=180)] = None,
    ) -> dict[str, Any]:
        # A view needs all three figures; a partial one is ignored, not guessed at.
        view = None
        if lat is not None and lon is not None and radius is not None:
            view = View(lat, lon, radius)
        return await service.snapshot(region, traffic_snapshot, traffic_upstream, view)

    @app.get("/api/airports")
    async def api_airports(
        region: Annotated[str, Query(max_length=32)] = DEFAULT_REGION,
        q: Annotated[str, Query(max_length=64)] = "",
        offset: Annotated[int, Query(ge=0, le=100_000)] = 0,
        limit: Annotated[int, Query(ge=1, le=LIST_PAGE_MAX)] = LIST_PAGE,
    ) -> dict[str, Any]:
        return service.airport_list(region, q, offset, limit)

    @app.get("/api/airport/{icao}")
    async def api_airport(icao: str) -> dict[str, Any]:
        return await service.detail(icao)

    @app.post("/api/airport/{icao}/triage")
    async def api_triage(icao: str) -> dict[str, Any]:
        return await service.triage(icao)

    _traffic_routes(app, service)
    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app


def _selection(
    request: Request,
    *,
    airport: Annotated[str | None, Query(max_length=8)] = None,
    radius_nm: int | None = None,
    airline: Annotated[str | None, Query(pattern="^[A-Z]{3}$")] = None,
    band: Literal["low", "mid", "high"] | None = None,
    phase: Literal["airborne", "ground"] | None = None,
    q: Annotated[str, Query(max_length=16)] = "",
    sort: Literal["distance", "altitude", "callsign", "last_heard", "speed"] = "distance",
    cap: int = DEFAULT_CAP,
    focus: Annotated[str | None, Query(pattern="^[0-9a-f]{6}$")] = None,
) -> Selection:
    """The reader's aircraft choices, from the query string, checked against the board."""
    # Only the choices the page offers, so a query string cannot ask for more.
    if (radius_nm is not None and radius_nm not in RADII_NM) or cap not in CAPS:
        raise HTTPException(422, f"radius_nm must be one of {RADII_NM}, cap one of {CAPS}")
    service: BoardService = request.app.state.board_service
    return service.selection(
        airport,
        radius_nm=float(radius_nm) if radius_nm is not None else None,
        airline=airline,
        band=band,
        phase=phase,
        query=q,
        sort=sort,
        cap=cap,
        focus=focus,
    )


# Module level, because annotations here are strings resolved against this module.
Chosen = Annotated[Selection, Depends(_selection)]


def _traffic_routes(app: FastAPI, service: BoardService) -> None:
    """The traffic layer: one route that spends, two that read what it holds."""
    app.state.board_service = service

    @app.get("/api/traffic/{region}")
    async def api_traffic(region: str, choice: Chosen) -> dict[str, Any]:
        """A refresh: spends credits, within the layer's ceiling."""
        return await service.airspace(region, choice)

    @app.get("/api/traffic/{region}/selection")
    async def api_traffic_selection(region: str, choice: Chosen) -> dict[str, Any]:
        """The held layer under new choices. Spends nothing."""
        return service.reselect(region, choice)

    @app.get("/api/traffic/{region}/aircraft/{icao24}/track")
    async def api_track(
        region: str,
        icao24: Annotated[str, Path(pattern="^[0-9a-fA-F]{6}$")],
        estimate: bool = False,
    ) -> dict[str, Any]:
        return await service.track(region, icao24, estimate=estimate)

    @app.post("/api/traffic/{region}/inbound/{icao}/tracks")
    async def api_inbound_tracks(
        region: str, icao: str, count: Annotated[int, Query(ge=0, le=DRAWN_MAX)]
    ) -> dict[str, Any]:
        """Observed tracks for what approaches an airport, at the price the page showed."""
        return await service.inbound_tracks(region, icao, count)

    @app.post("/api/airport/{icao}/arrivals")
    async def api_arrivals(icao: str) -> dict[str, Any]:
        """The arrival board, for matching approaching aircraft by callsign. Spends."""
        return await service.arrival_board(icao)
