"""Phase 1 acceptance matrix against live local processes, with no Docker.

Run it through `scripts/local_matrix.sh`, which starts the services this talks to.
Every line printed is an observed result: a real HTTP request to a real socket, and
for the counting lines, a count taken from the IdP's own access log rather than from
this client. Tokens are never printed; the last line checks that none of them
reached either log.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

IDP = os.environ.get("MATRIX_IDP", "http://127.0.0.1:9000")
OTHER_IDP = os.environ.get("MATRIX_OTHER_IDP", "http://127.0.0.1:9001")
OPS = os.environ.get("MATRIX_OPS", "http://127.0.0.1:9100")
SERVER = os.environ.get("MATRIX_SERVER", "http://127.0.0.1:8001")
RESOURCE = f"{SERVER}/mcp"
DOWNSTREAM = "api://flightops-ops"

IDP_LOG = Path(os.environ["MATRIX_IDP_LOG"])
SERVER_LOG = Path(os.environ["MATRIX_SERVER_LOG"])
AGENT_SECRET = os.environ["DEV_AGENT_CLIENT_SECRET"]
MCP_SECRET = os.environ["DEV_MCP_CLIENT_SECRET"]

TOKEN_EXCHANGE = "urn:ietf:params:oauth:grant-type:token-exchange"  # noqa: S105 - a URN
ACCESS_TOKEN = "urn:ietf:params:oauth:token-type:access_token"  # noqa: S105 - a URN
MCP_HEADERS = {"Accept": "application/json, text/event-stream"}

# Every token minted or received during the run, to prove none reached a log.
SEEN: list[str] = []


@dataclass
class Line:
    number: int
    claim: str
    passed: bool
    observed: str
    evidence: list[str] = field(default_factory=list)


# --- reading the services' own records ----------------------------------------------
def read_log(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def idp_token_posts() -> Counter[str]:
    """`POST /token` responses the IdP has logged so far, by status code."""
    text = read_log(IDP_LOG)
    return Counter(re.findall(r'"POST /token HTTP/1\.1" (\d{3})', text))


def server_events(name: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for raw in read_log(SERVER_LOG).splitlines():
        if not raw.startswith("{"):
            continue
        try:
            entry = json.loads(raw)
        except ValueError:
            continue
        if entry.get("event") == name:
            events.append(entry)
    return events


def claims_of(token: str) -> dict[str, Any]:
    """Payload of a JWT, unverified. For showing what was issued, never for trusting it."""
    body = token.split(".")[1]
    decoded: dict[str, Any] = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    return decoded


# --- talking to the services --------------------------------------------------------
async def mint(
    client: httpx.AsyncClient,
    *,
    sub: str,
    scope: str,
    audience: str = RESOURCE,
    expires_in: int | None = None,
    idp: str = IDP,
) -> str:
    """A delegated user token from the IdP's test endpoint (stands in for a sign-in)."""
    body: dict[str, Any] = {
        "sub": sub,
        "audience": audience,
        "scope": scope,
        "client_id": "flightops-agent",
    }
    if expires_in is not None:
        body["expires_in"] = expires_in
    response = await client.post(f"{idp}/dev/user-token", json=body)
    response.raise_for_status()
    token: str = response.json()["access_token"]
    SEEN.append(token)
    return token


async def call_tool(
    client: httpx.AsyncClient, name: str, arguments: dict[str, Any], token: str | None
) -> httpx.Response:
    headers = dict(MCP_HEADERS)
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }
    return await client.post(RESOURCE, json=body, headers=headers)


def payload_of(response: httpx.Response) -> dict[str, Any]:
    """The tool's structured result, or an empty dict when the response is not one."""
    try:
        result = response.json()["result"]
    except (ValueError, KeyError, TypeError):
        return {}
    structured = result.get("structuredContent")
    return structured if isinstance(structured, dict) else {}


async def exchange(
    client: httpx.AsyncClient, subject: str, *, audience: str = DOWNSTREAM, scope: str | None = None
) -> httpx.Response:
    """The server's own client trading a user token for a downstream one (RFC 8693)."""
    data = {
        "grant_type": TOKEN_EXCHANGE,
        "subject_token": subject,
        "subject_token_type": ACCESS_TOKEN,
        "requested_token_type": ACCESS_TOKEN,
        "audience": audience,
    }
    if scope is not None:
        data["scope"] = scope
    response = await client.post(f"{IDP}/token", data=data, auth=("flightops-mcp", MCP_SECRET))
    if response.status_code == 200:
        SEEN.append(response.json()["access_token"])
    return response


async def revoke(client: httpx.AsyncClient, token: str) -> None:
    response = await client.post(
        f"{IDP}/revoke", data={"token": token}, auth=("flightops-agent", AGENT_SECRET)
    )
    response.raise_for_status()


def www_authenticate(response: httpx.Response) -> str:
    return response.headers.get("www-authenticate", "")


# --- the eleven lines ---------------------------------------------------------------
async def line_1(c: httpx.AsyncClient) -> Line:
    response = await call_tool(c, "resolve_airport", {"query": "ESGG"}, None)
    challenge = www_authenticate(response)
    ok = response.status_code == 401 and challenge.startswith("Bearer ")
    return Line(
        1,
        "no token -> 401",
        ok,
        f"HTTP {response.status_code}",
        [f"WWW-Authenticate: {challenge}", f"tool output in body: {'matches' in response.text}"],
    )


async def line_2(c: httpx.AsyncClient) -> Line:
    before = len(server_events("token_rejected"))
    token = await mint(c, sub="matrix-user-2", scope="flightops.reference.read", expires_in=-60)
    response = await call_tool(c, "resolve_airport", {"query": "ESGG"}, token)
    await asyncio.sleep(0.5)
    reasons = [e["reason"] for e in server_events("token_rejected")[before:]]
    claims = claims_of(token)
    return Line(
        2,
        "expired token -> 401",
        response.status_code == 401 and reasons == ["expired"],
        f"HTTP {response.status_code}",
        [
            f"token exp is {claims['iat'] - claims['exp']}s before iat; signature valid",
            f"server logged reason: {reasons}",
        ],
    )


async def line_3(c: httpx.AsyncClient) -> Line:
    before = len(server_events("token_rejected"))
    token = await mint(c, sub="matrix-user-3", scope="flightops.reference.read", idp=OTHER_IDP)
    response = await call_tool(c, "resolve_airport", {"query": "ESGG"}, token)
    await asyncio.sleep(0.5)
    reasons = [e["reason"] for e in server_events("token_rejected")[before:]]
    iss = claims_of(token)["iss"]
    return Line(
        3,
        "token from a different issuer -> 401",
        response.status_code == 401 and iss != IDP and len(reasons) == 1,
        f"HTTP {response.status_code}",
        [
            f"token iss={iss} (trusted issuer is {IDP}); aud was this server's",
            f"server logged reason: {reasons}",
        ],
    )


async def line_4(c: httpx.AsyncClient) -> Line:
    before = len(server_events("token_rejected"))
    token = await mint(
        c, sub="matrix-user-4", scope="flightops.reference.read", audience=DOWNSTREAM
    )
    response = await call_tool(c, "resolve_airport", {"query": "ESGG"}, token)
    await asyncio.sleep(0.5)
    new = server_events("token_rejected")[before:]
    reasons = [e["reason"] for e in new]
    in_log = token in read_log(SERVER_LOG)
    detail = [{k: e.get(k) for k in ("reason", "aud", "expected")} for e in new]
    return Line(
        4,
        "aud != this server -> 401, reason logged without the token",
        response.status_code == 401 and reasons == ["wrong_audience"] and not in_log,
        f"HTTP {response.status_code}",
        [f"server logged: {detail}", f"token string present in server log: {in_log}"],
    )


async def line_5(c: httpx.AsyncClient) -> Line:
    token = await mint(c, sub="matrix-user-5", scope="flightops.reference.read")
    before = len(server_events("scope_challenge"))
    response = await call_tool(c, "airport_weather", {"airport": "ESGG"}, token)
    await asyncio.sleep(0.5)
    challenge = www_authenticate(response)
    logged = server_events("scope_challenge")[before:]
    ok = (
        response.status_code == 403
        and 'error="insufficient_scope"' in challenge
        and 'scope="flightops.weather.read"' in challenge
    )
    return Line(
        5,
        "valid token, insufficient scope -> 403 + WWW-Authenticate naming the scope",
        ok,
        f"HTTP {response.status_code}",
        [
            "token scope: flightops.reference.read; tool needs flightops.weather.read",
            f"WWW-Authenticate: {challenge}",
            f"server logged: {[(e.get('tool'), e.get('missing_scopes')) for e in logged]}",
        ],
    )


async def line_6(c: httpx.AsyncClient) -> Line:
    token = await mint(c, sub="matrix-user-6", scope="flightops.reference.read")
    user = await call_tool(c, "resolve_airport", {"query": "ESGG"}, token)
    user_payload = payload_of(user)
    matches = user_payload.get("matches") or [{}]

    # The same tool as a service principal: client_credentials, permissions in `roles`.
    grant = await c.post(
        f"{IDP}/token",
        data={"grant_type": "client_credentials", "scope": f"{RESOURCE}/.default"},
        auth=("flightops-agent", AGENT_SECRET),
    )
    grant.raise_for_status()
    service_token: str = grant.json()["access_token"]
    SEEN.append(service_token)
    service = await call_tool(c, "resolve_airport", {"query": "ESGG"}, service_token)
    service_payload = payload_of(service)
    ok = (
        user.status_code == 200
        and user_payload.get("ok") is True
        and matches[0].get("icao") == "ESGG"
        and service.status_code == 200
        and service_payload.get("ok") is True
    )
    return Line(
        6,
        "valid token, sufficient scope -> 200 and the tool answers",
        ok,
        f"HTTP {user.status_code} (user), HTTP {service.status_code} (service principal)",
        [
            f"resolve_airport('ESGG') -> ok={user_payload.get('ok')}, "
            f"icao={matches[0].get('icao')}, name={matches[0].get('name')}",
            f"service principal roles: {claims_of(service_token).get('roles')}",
        ],
    )


async def line_7(c: httpx.AsyncClient) -> tuple[Line, str]:
    subject = await mint(
        c,
        sub="matrix-user-7",
        scope="flightops.passengers.read flightops.weather.read flightops.reference.read",
    )
    response = await exchange(c, subject)
    if response.status_code != 200:
        return Line(7, "token exchange narrows scope", False, f"HTTP {response.status_code}"), ""
    exchanged: str = response.json()["access_token"]
    before, after = claims_of(subject), claims_of(exchanged)
    ok = after["scp"] == "flightops.passengers.read" and after["aud"] == DOWNSTREAM
    line = Line(
        7,
        "token exchange narrows scope",
        ok,
        f"HTTP {response.status_code}",
        [
            f"user token scp: {before['scp']}",
            f"exchanged token scp: {after['scp']}  aud: {after['aud']}  act: {after['act']}",
            f"exchanged ttl {after['exp'] - after['iat']}s, "
            f"user token ttl {before['exp'] - before['iat']}s",
        ],
    )
    return line, exchanged


async def line_8(c: httpx.AsyncClient) -> Line:
    wide = await mint(
        c, sub="matrix-user-8", scope="flightops.passengers.read flightops.weather.read"
    )
    weather_only = await mint(c, sub="matrix-user-8b", scope="flightops.weather.read")
    before = idp_token_posts()
    attempts = {
        "ask for a scope the target does not allow": await exchange(
            c, wide, scope="flightops.passengers.read flightops.weather.read"
        ),
        "ask for passengers.read with a token that lacks it": await exchange(
            c, weather_only, scope="flightops.passengers.read"
        ),
        "ask for an audience the client may not target": await exchange(
            c, wide, audience="api://someone-else"
        ),
    }
    await asyncio.sleep(0.3)
    after = idp_token_posts()
    granted = after["200"] - before["200"]
    refused = after["400"] - before["400"]
    evidence = []
    for name, response in attempts.items():
        body = response.json()
        evidence.append(
            f"{name}: HTTP {response.status_code} {body.get('error')}: "
            f"{body.get('error_description')}"
        )
    evidence.append(
        f"IdP log during these 3 attempts: {granted} issued (200), {refused} refused (400)"
    )
    ok = all(r.status_code == 400 for r in attempts.values()) and granted == 0 and refused == 3
    return Line(
        8,
        "exchange escalation attempt -> refused, not silently granted",
        ok,
        "HTTP " + ", ".join(str(r.status_code) for r in attempts.values()),
        evidence,
    )


async def line_9(c: httpx.AsyncClient, exchanged: str) -> Line:
    before = len(server_events("token_rejected"))
    downstream_token = await mint(
        c, sub="matrix-user-9", scope="flightops.passengers.read", audience=DOWNSTREAM
    )
    # (a) a token minted for the ops API is presented to this server.
    laundered = await call_tool(
        c, "flight_passenger_impact", {"flight": "SK1429"}, downstream_token
    )
    # (b) the token exchanged for the ops API is replayed against this server.
    replayed = await call_tool(c, "flight_passenger_impact", {"flight": "SK1429"}, exchanged)
    await asyncio.sleep(0.5)
    reasons = [e["reason"] for e in server_events("token_rejected")[before:]]

    # (c) the reverse: a token sent to this server is carried straight to the ops API.
    server_token = await mint(c, sub="matrix-user-9c", scope="flightops.passengers.read")
    direct = await c.get(
        f"{OPS}/flights/SK1429/passengers", headers={"Authorization": f"Bearer {server_token}"}
    )
    # Control: the exchanged token is the one the ops API does accept.
    control = await c.get(
        f"{OPS}/flights/SK1429/passengers", headers={"Authorization": f"Bearer {exchanged}"}
    )
    log_text = read_log(SERVER_LOG)
    leaked = any(t in log_text for t in (downstream_token, exchanged, server_token))
    ok = (
        laundered.status_code == 401
        and replayed.status_code == 401
        and reasons == ["wrong_audience", "wrong_audience"]
        and direct.status_code == 401
        and control.status_code == 200
        and not leaked
    )
    return Line(
        9,
        "passthrough attempt -> rejected, with the reason logged",
        ok,
        f"HTTP {laundered.status_code}, {replayed.status_code}, {direct.status_code}",
        [
            f"ops-API-audience token presented to the server: HTTP {laundered.status_code}",
            f"exchanged token replayed at the server: HTTP {replayed.status_code}",
            f"server logged reasons: {reasons}; token present in server log: {leaked}",
            f"server-audience token presented to the ops API directly: HTTP {direct.status_code} "
            f"{direct.json().get('error_description')}",
            f"control, exchanged token at the ops API: HTTP {control.status_code}",
        ],
    )


async def line_10(c: httpx.AsyncClient) -> Line:
    token = await mint(c, sub="matrix-user-10", scope="flightops.passengers.read")
    before = idp_token_posts()
    server_before = len(server_events("obo_exchanged"))

    responses = await asyncio.gather(
        *(call_tool(c, "flight_passenger_impact", {"flight": "SK1429"}, token) for _ in range(50))
    )
    await asyncio.sleep(0.5)
    after = idp_token_posts()
    issued = after["200"] - before["200"]
    attempted = sum(after.values()) - sum(before.values())
    statuses = Counter(r.status_code for r in responses)
    answered = sum(1 for r in responses if payload_of(r).get("ok") is True)

    # A 51st call a moment later must still find the cached token.
    await call_tool(c, "flight_passenger_impact", {"flight": "SK1429"}, token)
    await asyncio.sleep(0.3)
    later = idp_token_posts()["200"] - before["200"]
    ok = issued == 1 and attempted == 1 and answered == 50 and later == 1
    return Line(
        10,
        "50 concurrent calls, one identity -> exactly one exchange (counted at the IdP)",
        ok,
        f"{issued} exchange at the IdP for {answered}/50 answered calls",
        [
            f"client-side HTTP statuses: {dict(statuses)}",
            f"IdP access log, POST /token during the burst: {attempted} total, {issued} issued",
            f"after a 51st call: {later} issued in all",
            f"server log obo_exchanged events: "
            f"{len(server_events('obo_exchanged')) - server_before}",
        ],
    )


async def line_11(c: httpx.AsyncClient) -> Line:
    # (a) revoked before its first use: the server has nothing cached for this user.
    fresh = await mint(c, sub="matrix-user-11a", scope="flightops.passengers.read")
    await revoke(c, fresh)
    before = idp_token_posts()
    rejected_before = len(server_events("obo_exchange_rejected"))
    a = await call_tool(c, "flight_passenger_impact", {"flight": "SK1429"}, fresh)
    await asyncio.sleep(0.5)
    after = idp_token_posts()
    a_payload = payload_of(a)
    a_issued = after["200"] - before["200"]
    a_refused = after["400"] - before["400"]
    rejected = server_events("obo_exchange_rejected")[rejected_before:]

    # (b) revoked after the server already holds an exchanged token for this user.
    cached = await mint(c, sub="matrix-user-11b", scope="flightops.passengers.read")
    warm = await call_tool(c, "flight_passenger_impact", {"flight": "SK1429"}, cached)
    await revoke(c, cached)
    before_b = idp_token_posts()
    b = await call_tool(c, "flight_passenger_impact", {"flight": "SK1429"}, cached)
    await asyncio.sleep(0.3)
    b_requests = sum(idp_token_posts().values()) - sum(before_b.values())
    b_payload = payload_of(b)

    # (c) revoked, then used on a tool that needs no exchange. The server verifies
    # tokens offline against the JWKS, so nothing tells it the IdP revoked this one.
    plain = await mint(
        c, sub="matrix-user-11c", scope="flightops.reference.read flightops.passengers.read"
    )
    await revoke(c, plain)
    before_c = sum(idp_token_posts().values())
    cc = await call_tool(c, "resolve_airport", {"query": "ESGG"}, plain)
    c_requests = sum(idp_token_posts().values()) - before_c

    ok = a.status_code == 401 and a_issued == 0
    return Line(
        11,
        "revoked user token -> 401 and a re-exchange does not happen",
        ok,
        f"HTTP {a.status_code}; no token issued: {a_issued == 0}",
        [
            f"(a) revoked before first use: HTTP {a.status_code}, tool ok={a_payload.get('ok')}, "
            f"error code={(a_payload.get('error') or {}).get('code')}",
            f"    IdP during that call: {a_issued} issued (200), {a_refused} refused (400); "
            f"server logged obo_exchange_rejected: "
            f"{[e.get('idp_error') for e in rejected]}",
            f"(b) revoked after a successful call (HTTP {warm.status_code}): next call "
            f"HTTP {b.status_code}, tool ok={b_payload.get('ok')}, "
            f"IdP requests during it: {b_requests}",
            f"(c) revoked, then resolve_airport (no exchange): HTTP {cc.status_code}, "
            f"tool ok={payload_of(cc).get('ok')}, IdP requests during it: {c_requests}",
        ],
    )


async def log_hygiene() -> Line:
    server = read_log(SERVER_LOG)
    idp = read_log(IDP_LOG)
    found = [i for i, t in enumerate(SEEN) if t in server or t in idp]
    return Line(
        12,
        "extra: no token string in either service log",
        not found,
        f"{len(SEEN)} tokens checked, {len(found)} found in a log",
    )


# --- driver ---------------------------------------------------------------------------
async def wait_until_ready(client: httpx.AsyncClient) -> None:
    probes = [
        f"{IDP}/.well-known/jwks.json",
        f"{OTHER_IDP}/.well-known/jwks.json",
        f"{OPS}/healthz",
        f"{SERVER}/.well-known/oauth-protected-resource/mcp",
    ]
    for url in probes:
        for _ in range(120):
            try:
                if (await client.get(url)).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            await asyncio.sleep(0.5)
        else:
            raise SystemExit(f"not ready after 60 s: {url}")


async def main() -> int:
    limits = httpx.Limits(max_connections=100)
    async with httpx.AsyncClient(timeout=30.0, limits=limits) as client:
        await wait_until_ready(client)
        lines = [
            await line_1(client),
            await line_2(client),
            await line_3(client),
            await line_4(client),
            await line_5(client),
            await line_6(client),
        ]
        seven, exchanged = await line_7(client)
        lines += [
            seven,
            await line_8(client),
            await line_9(client, exchanged),
            await line_10(client),
            await line_11(client),
        ]
        lines.append(await log_hygiene())

    for entry in lines:
        verdict = "PASS" if entry.passed else "FAIL"
        sys.stdout.write(
            f"{entry.number:>2}  {verdict}  {entry.claim}\n      -> {entry.observed}\n"
        )
        for item in entry.evidence:
            sys.stdout.write(f"         {item}\n")
    failed = [entry.number for entry in lines if not entry.passed]
    sys.stdout.write(
        f"\n{len(lines) - len(failed)}/{len(lines)} passed; failed: {failed or 'none'}\n"
    )
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
