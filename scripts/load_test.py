"""A fixed-rate load test of the authenticated server against the mock upstream.

    bash scripts/load_test.sh            # starts the stack, runs this, stops the stack

or, against a stack that is already running:

    uv run python scripts/load_test.py --rps 50 --duration 60 --out load.json

**Open loop.** Request *i* is sent at `start + i / rps` whether or not earlier ones
have answered, and its latency is measured from that scheduled instant, not from
when the client got round to sending it. A closed loop (send, wait, send) slows down
when the server does and reports flattering numbers; this one keeps the arrival rate
and lets the queue show up in the percentiles. The time from actual send is
reported next to it.

**The mix** is a fixed cycle over the read tools and eight airports, so two runs ask
the same questions in the same order: reference lookups, weather, traffic, movements,
Schiphol's schedule, a composite triage and the status tool. Writes are left out on
purpose: a rebooking needs a person's approval per call, which is not a load.

**What it counts.** Every response is one of: `ok`, a tool failure by code (the call
was answered, with `ok: false`), an HTTP status other than 200, a JSON-RPC error, or a
transport error. Credit spend is read from the server's own `/metrics` before and
after, and upstream requests from the mock's `/stats`, so neither is this client's
estimate.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import platform
import re
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Any

import httpx

AIRPORTS = ("ESGG", "ESSA", "EKCH", "ENGM", "EHAM", "EGLL", "EDDF", "LFPG")
SCOPES = " ".join(
    f"flightops.{name}.read" for name in ("reference", "weather", "traffic", "status", "schedule")
)
MCP_HEADERS = {"Accept": "application/json, text/event-stream"}


def operation(index: int) -> tuple[str, dict[str, Any]]:
    """The `index`-th call of the fixed mix."""
    airport = AIRPORTS[(index // len(_MIX)) % len(AIRPORTS)]
    name, arguments = _MIX[index % len(_MIX)]
    return name, {key: (airport if value is None else value) for key, value in arguments.items()}


# `None` is replaced by the cycle's airport. Ten calls per cycle.
_MIX: tuple[tuple[str, dict[str, Any]], ...] = (
    ("resolve_airport", {"query": None}),
    ("airport_weather", {"airport": None}),
    ("airport_live_traffic", {"airport": None}),
    ("airport_weather", {"airport": None}),
    ("airport_movements", {"airport": None, "direction": "both"}),
    ("schiphol_flights", {"direction": "both", "window_minutes": 120}),
    ("airport_live_traffic", {"airport": None, "radius_nm": 60.0}),
    ("disruption_triage", {"airport": None}),
    ("resolve_airport", {"query": None}),
    ("upstream_status", {}),
)


@dataclass(slots=True)
class Sample:
    tool: str
    outcome: str
    latency_ms: float  # from the scheduled send instant
    service_ms: float  # from the actual send


def percentile(values: list[float], fraction: float) -> float:
    """Nearest-rank percentile: a value that was actually observed."""
    if not values:
        return float("nan")
    ordered = sorted(values)
    rank = min(len(ordered), max(1, math.ceil(fraction * len(ordered))))
    return ordered[rank - 1]


def classify(response: httpx.Response) -> str:
    if response.status_code != 200:
        return f"http_{response.status_code}"
    try:
        body = response.json()
    except ValueError:
        return "unreadable_response"
    if "error" in body:
        return f"rpc_error_{body['error'].get('code')}"
    structured = (body.get("result") or {}).get("structuredContent") or {}
    if structured.get("ok") is False:
        return f"tool_error:{(structured.get('error') or {}).get('code')}"
    return "ok"


_SAMPLE = re.compile(r"^([a-zA-Z_:][a-zA-Z0-9_:]*)(\{[^}]*\})?\s+([0-9.eE+-]+|NaN)$")


def scrape(text: str) -> dict[str, float]:
    """Prometheus text format, flattened to `name{labels}` -> value."""
    out: dict[str, float] = {}
    for line in text.splitlines():
        match = _SAMPLE.match(line)
        if match and not line.startswith("#"):
            out[match.group(1) + (match.group(2) or "")] = float(match.group(3))
    return out


def delta(after: dict[str, float], before: dict[str, float], prefix: str) -> dict[str, float]:
    return {
        key: after[key] - before.get(key, 0.0)
        for key in sorted(after)
        if key.startswith(prefix) and after[key] - before.get(key, 0.0)
    }


async def run(
    args: argparse.Namespace, *, transport: httpx.AsyncBaseTransport | None = None
) -> dict[str, Any]:
    """One run. `transport` is for the test suite, which runs the stack in process."""
    resource = f"{args.server}/mcp"
    limits = httpx.Limits(
        max_connections=args.connections, max_keepalive_connections=args.connections
    )
    async with httpx.AsyncClient(
        timeout=args.timeout, limits=limits, transport=transport
    ) as client:
        minted = await client.post(
            f"{args.idp}/dev/user-token",
            json={
                "sub": "load-test",
                "audience": resource,
                "scope": SCOPES,
                "client_id": "flightops-agent",
                "expires_in": int(args.duration) + 600,
            },
        )
        minted.raise_for_status()
        token = minted.json()["access_token"]
        headers = {**MCP_HEADERS, "Authorization": f"Bearer {token}"}

        metrics_before = scrape((await client.get(f"{args.server}/metrics")).text)
        mock_before = (await client.get(f"{args.mock}/stats")).json()

        samples: list[Sample] = []
        total = int(args.rps * args.duration)

        async def fire(index: int, scheduled: float) -> None:
            name, arguments = operation(index)
            body = {
                "jsonrpc": "2.0",
                "id": index,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            }
            sent = time.perf_counter()
            try:
                response = await client.post(resource, json=body, headers=headers)
                outcome = classify(response)
            except httpx.HTTPError as exc:
                outcome = f"transport_{type(exc).__name__}"
            done = time.perf_counter()
            samples.append(Sample(name, outcome, (done - scheduled) * 1000, (done - sent) * 1000))

        tasks: list[asyncio.Task[None]] = []
        start = time.perf_counter()
        lag_max = 0.0
        for index in range(total):
            scheduled = start + index / args.rps
            wait = scheduled - time.perf_counter()
            if wait > 0:
                await asyncio.sleep(wait)
            else:
                lag_max = max(lag_max, -wait)
            tasks.append(asyncio.create_task(fire(index, scheduled)))
        sending_s = time.perf_counter() - start
        await asyncio.gather(*tasks)
        wall_s = time.perf_counter() - start

        metrics_after = scrape((await client.get(f"{args.server}/metrics")).text)
        mock_after = (await client.get(f"{args.mock}/stats")).json()

    latencies = [s.latency_ms for s in samples]
    service = [s.service_ms for s in samples]
    outcomes = Counter(s.outcome for s in samples)
    by_tool: dict[str, list[float]] = defaultdict(list)
    for s in samples:
        by_tool[s.tool].append(s.latency_ms)
    failed = sum(count for outcome, count in outcomes.items() if outcome != "ok")
    answered_errors = sum(
        count for outcome, count in outcomes.items() if outcome.startswith("tool_error:")
    )
    credits = delta(metrics_after, metrics_before, "flightops_upstream_credits_spent_total")
    return {
        "config": {
            "rps": args.rps,
            "duration_s": args.duration,
            "requests_scheduled": total,
            "connections": args.connections,
            "mix": [name for name, _ in _MIX],
            "airports": list(AIRPORTS),
            "mock_latency_ms": args.mock_latency_ms,
        },
        "machine": {
            "platform": platform.platform(),
            "processor": platform.processor(),
            "logical_cpus": os.cpu_count(),
            "python": platform.python_version(),
            "note": "load generator, server, IdP and mock upstream all on this one machine",
        },
        "timing": {
            "sending_s": round(sending_s, 2),
            "wall_s": round(wall_s, 2),
            "achieved_send_rps": round(total / sending_s, 1),
            "max_send_lag_ms": round(lag_max * 1000, 1),
        },
        "requests": {
            "completed": len(samples),
            "ok": outcomes.get("ok", 0),
            "failed": failed,
            "error_rate": round(failed / len(samples), 4) if samples else None,
            "of_which_answered_tool_errors": answered_errors,
            "outcomes": dict(sorted(outcomes.items())),
        },
        "latency_ms_from_schedule": {
            "p50": round(percentile(latencies, 0.50), 1),
            "p95": round(percentile(latencies, 0.95), 1),
            "p99": round(percentile(latencies, 0.99), 1),
            "max": round(max(latencies), 1),
        },
        "latency_ms_from_send": {
            "p50": round(percentile(service, 0.50), 1),
            "p95": round(percentile(service, 0.95), 1),
            "p99": round(percentile(service, 0.99), 1),
        },
        "latency_ms_by_tool": {
            tool: {
                "n": len(values),
                "p50": round(percentile(values, 0.50), 1),
                "p95": round(percentile(values, 0.95), 1),
            }
            for tool, values in sorted(by_tool.items())
        },
        "spend": {
            "opensky_credits": delta(
                metrics_after, metrics_before, "flightops_opensky_credits_spent_total"
            ).get("flightops_opensky_credits_spent_total", 0.0),
            "by_upstream": credits,
            "cache": delta(metrics_after, metrics_before, "flightops_cache_requests_total"),
            "upstream_requests_served_by_mock": {
                key: mock_after[key] - mock_before.get(key, 0)
                for key in sorted(mock_after)
                if key.endswith("_requests") and mock_after[key] - mock_before.get(key, 0)
            },
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--server", default="http://127.0.0.1:8002")
    parser.add_argument("--idp", default="http://127.0.0.1:9000")
    parser.add_argument("--mock", default="http://127.0.0.1:9200")
    parser.add_argument("--rps", type=float, default=50.0)
    parser.add_argument("--duration", type=float, default=60.0)
    parser.add_argument("--connections", type=int, default=100)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument(
        "--mock-latency-ms",
        type=float,
        default=float(os.environ.get("MOCK_LATENCY_MS", "0")),
        help="recorded with the result; the mock reads MOCK_LATENCY_MS itself",
    )
    parser.add_argument("--out", help="write the result as JSON here")
    args = parser.parse_args()

    result = asyncio.run(run(args))
    text = json.dumps(result, indent=2)
    print(text)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            handle.write(text + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
