"""The second adapter through a running stack: the server, the local IdP, the mock upstream.

    MATRIX_SERVER=http://localhost:8000 MATRIX_IDP=http://127.0.0.1:9000 \\
    MATRIX_MOCK=http://127.0.0.1:9200 uv run python scripts/second_upstream.py

Run by the `compose-matrix` CI job against `docker compose`, and by
`scripts/load_test.sh` against local processes. Every line is an observed result
from a real socket. What it shows, and what it does not:

* A user token with `flightops.schedule.read` gets Schiphol's schedule; one without
  it is answered 403 with a scope challenge, and the mock is not asked.
* The request count the server's ledger reports equals the count the mock served.
* The mock's own counters: one token, issued for the right audience, never used
  after expiry, and no retired `app_id`/`app_key`/`ResourceVersion` header.

It runs against `services/mock_upstream`, a mock of Schiphol's documentation. It
proves the adapter speaks the documented shape. It cannot prove Schiphol does.
"""

from __future__ import annotations

import asyncio
import os
import sys
from typing import Any

import httpx

SERVER = os.environ.get("MATRIX_SERVER", "http://127.0.0.1:8001")
IDP = os.environ.get("MATRIX_IDP", "http://127.0.0.1:9000")
MOCK = os.environ.get("MATRIX_MOCK", "http://127.0.0.1:9200")
RESOURCE = f"{SERVER}/mcp"
MCP_HEADERS = {"Accept": "application/json, text/event-stream"}


async def mint(client: httpx.AsyncClient, sub: str, scope: str) -> str:
    response = await client.post(
        f"{IDP}/dev/user-token",
        json={"sub": sub, "audience": RESOURCE, "scope": scope, "client_id": "flightops-agent"},
    )
    response.raise_for_status()
    token: str = response.json()["access_token"]
    return token


async def call(
    client: httpx.AsyncClient, token: str, name: str, arguments: dict[str, Any]
) -> httpx.Response:
    return await client.post(
        RESOURCE,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
        headers={**MCP_HEADERS, "Authorization": f"Bearer {token}"},
    )


def payload(response: httpx.Response) -> dict[str, Any]:
    try:
        structured = response.json()["result"].get("structuredContent")
    except (ValueError, KeyError, TypeError, AttributeError):
        return {}
    return structured if isinstance(structured, dict) else {}


async def main(transport: httpx.AsyncBaseTransport | None = None) -> int:
    """Every check, printed. `transport` is for the test suite's in-process stack."""
    results: list[tuple[bool, str]] = []
    async with httpx.AsyncClient(timeout=30, transport=transport) as client:
        before = (await client.get(f"{MOCK}/stats")).json()
        allowed = await mint(client, "second-upstream-1", "flightops.schedule.read")
        refused = await mint(client, "second-upstream-2", "flightops.traffic.read")
        status_token = await mint(client, "second-upstream-3", "flightops.status.read")

        ok = await call(client, allowed, "schiphol_flights", {"window_minutes": 90})
        body = payload(ok)
        counts = body.get("counts") or {}
        spent = (body.get("bookkeeping") or {}).get("credits_spent")
        results.append(
            (
                ok.status_code == 200 and body.get("ok") is True and isinstance(spent, int),
                f"schedule.read -> HTTP {ok.status_code}, ok={body.get('ok')}, "
                f"arrivals={(counts.get('arrival') or {}).get('listed')}, "
                f"departures={(counts.get('departure') or {}).get('listed')}, "
                f"requests charged={spent}",
            )
        )

        mid = (await client.get(f"{MOCK}/stats")).json()
        no = await call(client, refused, "schiphol_flights", {"window_minutes": 90})
        after_refusal = (await client.get(f"{MOCK}/stats")).json()
        challenge = no.headers.get("www-authenticate", "")
        results.append(
            (
                no.status_code == 403
                and 'scope="flightops.schedule.read"' in challenge
                and after_refusal == mid,
                f"traffic.read only -> HTTP {no.status_code}, challenge names "
                f"{'flightops.schedule.read' if 'schedule.read' in challenge else 'nothing'}, "
                f"mock untouched={after_refusal == mid}",
            )
        )

        status = payload(await call(client, status_token, "upstream_status", {}))
        ledger = (status.get("budget") or {}).get("schiphol_requests_this_hour")
        served = after_refusal.get("schiphol.flight_requests", 0) - before.get(
            "schiphol.flight_requests", 0
        )
        results.append(
            (
                ledger is not None and ledger >= served == spent,
                f"ledger says {ledger} request(s) this hour; the mock served {served} "
                f"for this run; the payload charged {spent}",
            )
        )

        stats = (await client.get(f"{MOCK}/stats")).json()
        bad = {
            key: value
            for key, value in stats.items()
            if key.startswith("schiphol.refused")
            or key
            in {
                "schiphol.token_wrong_audience",
                "schiphol.token_without_audience",
                "schiphol.legacy_headers",
            }
        }
        results.append(
            (
                stats.get("schiphol.tokens_issued", 0) >= 1 and not bad,
                f"mock: tokens issued={stats.get('schiphol.tokens_issued', 0)}, "
                f"wrong audience / expired / legacy headers: {bad or 'none'}",
            )
        )

    for number, (passed, text) in enumerate(results, start=1):
        print(f"{number}. {'PASS' if passed else 'FAIL'}  {text}")
    failed = sum(1 for passed, _ in results if not passed)
    print(f"{len(results) - failed}/{len(results)} passed (against a mock of the documented API)")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
