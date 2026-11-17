"""The whole system in one process: MCP server, local IdP, mock operations API.

This is what an offline run talks to. Nothing between the agent and the
operations API's ledger is mocked: requests go through the SDK's own client over
the real wire protocol to the real authenticated server, tokens are signed and
exchanged by the IdP fixture, and the approval gate is the one in
`auth/approval.py`. No socket is opened; every hop is an ASGI transport.

What is a fixture, and is named as one:

* **The upstreams.** OpenSky and AviationWeather answer from here. Unless a run
  says otherwise (`Upstreams`) they answer with nothing: no aircraft, no METAR.
  That is a legitimate state of the world, and the tools must report it as
  unknown. The evaluation harness describes other states of the world, one per
  scenario, through the same seam.
* **The IdP.** `services/idp` mints a user token on request (ADR 0001). The
  approver obtains a grant from it like any other principal (ADR 0003).
* **Its secrets.** Client secrets are generated per stack and die with it.

The stack runs on real time. The server's bearer check and the operations API
both judge a token's expiry against the wall clock, so the IdP cannot be put on
a simulated one. Nothing from that clock reaches a transcript. The age of
upstream data is the exception: the server judges it against the scenario's
start (`Runtime.data_clock`), because that is the moment the fixture describes.
"""

from __future__ import annotations

import gc
import secrets
import tempfile
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

import httpx
import httpx2
from pydantic import SecretStr

from flightops_mcp import server as srv
from flightops_mcp.airports import build_from_csv
from flightops_mcp.auth import Scopes, refuse_inbound_token
from flightops_mcp.auth.approval import APPROVAL_DETAIL_TYPE
from flightops_mcp.config import Settings
from flightops_mcp.runtime import Runtime, set_runtime
from services.idp.app import create_app as create_idp
from services.idp.config import ClientConfig, IdpConfig
from services.ops_api.app import OpsApiConfig
from services.ops_api.app import create_app as create_ops_api

from .approval import ApprovalDecision, ApprovalRequest, Approver
from .scenarios import ApprovalScript, Scenario
from .tools import McpTools, connect

ISSUER: Final = "http://idp.local"
OPS_URL: Final = "http://ops.local"
SERVER_URL: Final = "http://127.0.0.1:8000"
RESOURCE: Final = f"{SERVER_URL}/mcp"
OPS_AUDIENCE: Final = "api://flightops-ops"
APPROVAL_AUDIENCE: Final = "api://flightops-approvals"
# The agent's own client id at the IdP, and the server's as a confidential client.
AGENT_CLIENT: Final = "flightops-agent"
SERVER_CLIENT: Final = "flightops-mcp"

SEED_AIRPORTS: Final = Path(__file__).parent.parent / "data" / "airports_seed.csv"


Handler = Callable[[httpx.Request], httpx.Response]


def _no_traffic(request: httpx.Request) -> httpx.Response:
    if "/flights/" in request.url.path:
        return httpx.Response(200, json=[])
    return httpx.Response(200, json={"time": 0, "states": []})


def _upstream_token(_request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"access_token": _throwaway(), "expires_in": 1800})


def _no_weather(_request: httpx.Request) -> httpx.Response:
    return httpx.Response(204)


def _no_schedule(_request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"flights": []})


@dataclass(frozen=True, slots=True)
class Upstreams:
    """What the world outside the server answers during one run.

    The defaults are the empty world: no aircraft, no weather report, a working
    OpenSky client. A handler may raise an `httpx` error to be a dead upstream.
    """

    opensky: Handler = _no_traffic
    opensky_token: Handler = _upstream_token
    weather: Handler = _no_weather
    # False runs the server with no OpenSky client configured at all.
    opensky_credentials: bool = True
    # Schiphol's flights resource and its Auth0 token endpoint, the same way.
    schiphol: Handler = _no_schedule
    schiphol_token: Handler = _upstream_token
    schiphol_credentials: bool = True
    # Answers a request to the operations API in its place. None lets it through.
    ops: Callable[[httpx.Request], httpx.Response | None] | None = None
    # Free seats per target flight. None keeps the operations API's own.
    seats: dict[str, int] | None = field(default=None)


class _OpsTap(httpx.AsyncBaseTransport):
    """Passes requests through to the operations API and counts the rebooking ones."""

    def __init__(
        self,
        inner: httpx.AsyncBaseTransport,
        override: Callable[[httpx.Request], httpx.Response | None] | None = None,
    ) -> None:
        self._inner = inner
        self._override = override
        self.rebooking_requests = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/rebookings":
            self.rebooking_requests += 1
        answered = self._override(request) if self._override else None
        if answered is not None:
            return answered
        return await self._inner.handle_async_request(request)


class FixtureApprover:
    """The approval channel of an offline run: a scripted person at the fixture IdP.

    It stands where a duty manager's approval screen would. On an accept it
    obtains a grant from the IdP for exactly the proposal it was shown, as the
    approver, for the approval audience. It cannot do more than a person with
    that sign-in could, and the server still decides whether the grant counts.
    """

    def __init__(self, script: ApprovalScript, idp: httpx.AsyncBaseTransport) -> None:
        self._script = script
        self._idp = idp

    async def decide(self, request: ApprovalRequest) -> ApprovalDecision | None:
        action = self._script.action
        if action == "unanswered":
            return None
        if action == "decline":
            return ApprovalDecision(action="decline", approver=self._script.approver)
        if action == "cancel":
            return ApprovalDecision(action="cancel", approver=self._script.approver)
        grant = await _user_token(
            self._idp,
            sub=self._script.approver,
            audience=APPROVAL_AUDIENCE,
            scope=Scopes.REBOOKING_APPROVE,
            authorization_details=[{"type": APPROVAL_DETAIL_TYPE, "proposal": request.proposal}],
        )
        return ApprovalDecision(action="accept", approver=self._script.approver, grant=grant)


@dataclass(slots=True)
class LocalStack:
    tools: McpTools
    runtime: Runtime
    _ops_app: Any
    _ops_tap: _OpsTap

    def ops_api_counts(self) -> dict[str, int]:
        """What the operations API itself saw and did. The count of record for a write."""
        return {
            "rebooking_requests": self._ops_tap.rebooking_requests,
            "rebookings_written": len(self._ops_app.state.writes),
        }


@asynccontextmanager
async def local_stack(
    scenario: Scenario,
    *,
    clock: Callable[[], float],
    manifests: dict[str, list[dict[str, Any]]] | None = None,
    approver: Approver | None = None,
    upstreams: Upstreams | None = None,
) -> AsyncIterator[LocalStack]:
    """Bring the system up for one run, connected as the scenario's user.

    `clock` is the run's clock, used for what the agent records. `manifests`
    replaces the operations API's passengers, `approver` the scripted approval
    channel, and `upstreams` what OpenSky, AviationWeather and the operations API
    answer. They exist for tests and for the evaluation harness.
    """
    world = upstreams or Upstreams()
    server_secret = secrets.token_urlsafe(24)
    idp_app = create_idp(
        IdpConfig(
            issuer=ISSUER,
            clients=[
                ClientConfig(client_id=AGENT_CLIENT, client_secret=SecretStr(_throwaway())),
                ClientConfig(
                    client_id=SERVER_CLIENT,
                    client_secret=SecretStr(server_secret),
                    resource=RESOURCE,
                    exchange_targets={
                        OPS_AUDIENCE: [Scopes.PASSENGERS_READ, Scopes.REBOOKING_WRITE]
                    },
                ),
            ],
        )
    )
    idp = httpx.ASGITransport(app=idp_app)
    jwks_url = f"{ISSUER}/.well-known/jwks.json"
    ops_app = create_ops_api(
        OpsApiConfig(issuer=ISSUER, jwks_url=jwks_url, audience=OPS_AUDIENCE),
        jwks_transport=idp,
        manifests=manifests,
        seats=world.seats,
    )
    ops = _OpsTap(httpx.ASGITransport(app=ops_app), world.ops)

    with tempfile.TemporaryDirectory(
        prefix="flightops-agent-", ignore_cleanup_errors=True
    ) as workdir:
        airports_db = Path(workdir) / "airports.sqlite3"
        build_from_csv(SEED_AIRPORTS, airports_db)
        settings = Settings(
            _env_file=None,  # type: ignore[call-arg]  # a developer's .env must not leak into a run
            opensky_client_id="offline-fixture" if world.opensky_credentials else None,
            opensky_client_secret=_throwaway() if world.opensky_credentials else None,
            schiphol_client_id="offline-fixture" if world.schiphol_credentials else None,
            schiphol_client_secret=_throwaway() if world.schiphol_credentials else None,
            airports_db=airports_db,
            backoff_base_s=0.0,
            auth_issuer=ISSUER,
            auth_jwks_url=jwks_url,
            resource_server_url=RESOURCE,
            auth_token_url=f"{ISSUER}/token",
            auth_client_id=SERVER_CLIENT,
            auth_client_secret=server_secret,
            ops_api_base_url=OPS_URL,
            ops_api_audience=OPS_AUDIENCE,
            approval_audience=APPROVAL_AUDIENCE,
        )
        runtime = Runtime.build(
            settings,
            opensky_transport=httpx.MockTransport(world.opensky),
            token_transport=httpx.MockTransport(world.opensky_token),
            weather_transport=httpx.MockTransport(world.weather),
            schiphol_transport=httpx.MockTransport(world.schiphol),
            schiphol_token_transport=httpx.MockTransport(world.schiphol_token),
            idp_transport=idp,
            ops_transport=ops,
            # The fixture describes the world at the scenario's start, so the
            # age of its data is judged from there, whichever backend is planning.
            # Tokens are still judged on real time (see the module docstring).
            data_clock=lambda: scenario.clock_start,
        )
        set_runtime(runtime)
        try:
            server = srv.create_http_server(settings, jwks_transport=idp)
            app = server.streamable_http_app(**srv.HTTP_OPTIONS)
            token = await _user_token(
                idp, sub=scenario.user, audience=RESOURCE, scope=" ".join(scenario.scopes)
            )
            if approver is None and scenario.approval is not None:
                approver = FixtureApprover(scenario.approval, idp)
            async with (
                app.router.lifespan_context(app),
                httpx2.AsyncClient(
                    transport=httpx2.ASGITransport(app=app),
                    headers={"Authorization": f"Bearer {token}"},
                ) as http_client,
                connect(RESOURCE, http_client, approver=approver, clock=clock) as tools,
            ):
                yield LocalStack(tools=tools, runtime=runtime, _ops_app=ops_app, _ops_tap=ops)
        finally:
            set_runtime(None)
            await runtime.aclose()
            # `AirportDB` leaves each lookup's SQLite connection to the garbage
            # collector, and Windows will not delete a file that is still open.
            # Collect before the directory goes, so nothing is left behind.
            gc.collect()


async def _user_token(
    idp: httpx.AsyncBaseTransport,
    *,
    sub: str,
    audience: str,
    scope: str,
    authorization_details: list[dict[str, Any]] | None = None,
) -> str:
    """A delegated token from the fixture IdP, standing in for a person signing in."""
    body: dict[str, Any] = {
        "sub": sub,
        "audience": audience,
        "scope": scope,
        "client_id": AGENT_CLIENT,
    }
    if authorization_details is not None:
        body["authorization_details"] = authorization_details
    async with httpx.AsyncClient(
        transport=idp, base_url=ISSUER, event_hooks={"request": [refuse_inbound_token]}
    ) as client:
        response = await client.post("/dev/user-token", json=body)
    response.raise_for_status()
    return str(response.json()["access_token"])


def _throwaway() -> str:
    """A secret for a fixture that lives as long as this process. Never a constant."""
    return secrets.token_urlsafe(24)
