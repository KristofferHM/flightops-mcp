"""Measure one World weather poll of the board against the live NOAA upstream.

`PYTHONPATH=. uv run python scripts/measure_world_weather.py [--region world] [--log FILE]`

It builds the board exactly as `python -m dashboard` does, asks for one snapshot, and
counts what reached aviationweather.gov through a transport that only observes. The
numbers in `docs/DASHBOARD.md` ("Weather requests, measured") come from this script.
It prints no response bodies: a request's status, its id count and its latency are
enough to tell a batch limit from a rate limit.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

import httpx

from dashboard.__main__ import load_airports
from dashboard.service import BoardService
from dashboard.sources import LiveSource
from flightops_mcp import weather
from flightops_mcp.config import Settings
from flightops_mcp.resilience import configure_breakers
from flightops_mcp.runtime import Runtime, set_runtime


class CountingTransport(httpx.AsyncBaseTransport):
    """Passes every request through and keeps one line about it."""

    def __init__(self, inner: httpx.AsyncBaseTransport) -> None:
        self._inner = inner
        self.log: list[dict[str, Any]] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        ids = request.url.params.get("ids", "")
        started = time.perf_counter()
        entry: dict[str, Any] = {
            "path": request.url.path.rsplit("/", 1)[-1],
            "ids": len([code for code in ids.split(",") if code]),
            "url_bytes": len(str(request.url)),
        }
        try:
            response = await self._inner.handle_async_request(request)
        except httpx.HTTPError as exc:
            entry.update(status=type(exc).__name__, seconds=round(time.perf_counter() - started, 2))
            self.log.append(entry)
            raise
        body = await response.aread()
        entry.update(status=response.status_code, seconds=round(time.perf_counter() - started, 2))
        if response.status_code == 200 and body[:1] == b"[":
            # How close the answer came to NOAA's 400-record cap.
            entry["records"] = len(json.loads(body))
        self.log.append(entry)
        return response

    async def aclose(self) -> None:
        await self._inner.aclose()


def classify(weather: dict[str, Any]) -> str:
    summary = str(weather["summary"])
    if weather.get("neighbour") or summary == "Unknown: no METAR returned":
        return "no observation"
    if not summary.startswith("Unknown"):
        return "own report"
    if summary.endswith("(station_rejected)"):
        return "id refused"
    if summary.startswith("Unknown: weather unavailable"):
        return "upstream failed"
    return "other unknown"


async def measure(
    region: str,
    settings: Settings | None = None,
    inner: httpx.AsyncBaseTransport | None = None,
) -> dict[str, Any]:
    """One snapshot of `region`. `inner` replaces the network, which only a test does."""
    settings = settings or Settings()
    configure_breakers(
        failure_threshold=settings.breaker_failure_threshold,
        recovery_timeout_s=settings.breaker_recovery_timeout_s,
    )
    transport = CountingTransport(inner or httpx.AsyncHTTPTransport())
    runtime = Runtime.build(settings, weather_transport=transport)
    set_runtime(runtime)
    try:
        service = BoardService(LiveSource(), load_airports(settings))
        started = time.perf_counter()
        snapshot = await service.snapshot(region)
        wall = time.perf_counter() - started
    finally:
        await runtime.aclose()
        set_runtime(None)
    airports = snapshot["airports"]
    kinds = Counter(classify(a["weather"]) for a in airports)
    failures = Counter(
        a["weather"]["summary"]
        for a in airports
        if classify(a["weather"]) in ("upstream failed", "id refused")
    )
    return {
        "region": region,
        "airports": len(airports),
        "requests": len(transport.log),
        "statuses": dict(Counter(str(entry["status"]) for entry in transport.log)),
        "wall_s": round(wall, 1),
        "own_report": kinds["own report"],
        "no_observation": kinds["no observation"],
        "upstream_failed": kinds["upstream failed"],
        "id_refused": kinds["id refused"],
        "other_unknown": kinds["other unknown"],
        "neighbour_annotated": sum(1 for a in airports if a["weather"].get("neighbour")),
        "failure_reasons": dict(failures),
        "banner": snapshot["banner"]["label"],
        "request_log": transport.log,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--region", default="world")
    parser.add_argument("--log", type=Path, help="write every request's line here as JSON")
    parser.add_argument("--chunk", type=int, help="ids per request, to compare sizes")
    args = parser.parse_args()
    if args.chunk:
        weather.METAR_CHUNK = args.chunk
    result = asyncio.run(measure(args.region))
    result["chunk"] = weather.METAR_CHUNK
    log = result.pop("request_log")
    if args.log:
        args.log.write_text(json.dumps(log, indent=1), encoding="utf-8")
    json.dump(result, sys.stdout, indent=2)
    print()


if __name__ == "__main__":
    main()
