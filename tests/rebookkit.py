"""The whole write path, in process: MCP server, local IdP, mock ops API.

Requests go through the SDK's own client over the real wire protocol, because the
approval gate lives in that protocol: `resultType: "input_required"`, the sealed
`requestState`, and the retry that carries the approval. Nothing between the
client and the ops API's ledger is mocked.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import httpx
import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.types import (
    CallToolResult,
    ElicitRequestFormParams,
    ElicitResult,
    InputRequiredResult,
)

from flightops_mcp.auth import Scopes
from flightops_mcp.auth.approval import APPROVAL_DETAIL_TYPE
from flightops_mcp.config import Settings
from flightops_mcp.runtime import Runtime, set_runtime
from services.ops_api.app import OpsApiConfig, create_app

from .authkit import ISSUER, JWKS_URL
from .harness import BASE_URL, RemoteServer, remote_settings, serve
from .idpkit import OPS_AUDIENCE, SERVER, LocalIdp
from .test_server_tools import install

TOKEN_URL = f"{ISSUER}/token"
OPS_URL = "http://ops.test"
APPROVAL_AUDIENCE = "api://flightops-approvals"
PROPOSER = "dispatcher-3"
APPROVER = "duty-manager-7"

ARGS: dict[str, Any] = {
    "from_flight": "SK1429",
    "flight_date": "2026-11-16",
    "to_flight": "SK1431",
    "cohort": "tight_connections",
    "reason_code": "late_inbound",
    "passenger_count": 2,
}


class ShiftedClock:
    """Real time plus an offset the test moves: the IdP stays on real time, the server jumps."""

    def __init__(self) -> None:
        self.offset = 0.0

    def __call__(self) -> float:
        return time.time() + self.offset

    def advance(self, seconds: float) -> None:
        self.offset += seconds


class OpsTap(httpx.AsyncBaseTransport):
    """Passes requests to the ops API, recording them, and can lose a response on the way back."""

    def __init__(self, inner: httpx.AsyncBaseTransport) -> None:
        self._inner = inner
        self.requests: list[httpx.Request] = []
        self.lose_responses = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0)
        self.requests.append(request)
        response = await self._inner.handle_async_request(request)
        if self.lose_responses:
            # The API has acted; the caller never hears. The worst moment to retry.
            self.lose_responses -= 1
            await response.aread()
            raise httpx.ReadError("connection reset after the request was processed")
        return response

    def rebookings(self) -> list[httpx.Request]:
        return [request for request in self.requests if request.url.path == "/rebookings"]


async def _unused_elicitation(_context: Any, _params: Any) -> ElicitResult:
    """Declares the capability. The tests answer each approval request by hand."""
    return ElicitResult(action="cancel")


@dataclass(slots=True)
class WriteStack:
    idp: LocalIdp
    ops_app: Any
    ops: OpsTap
    runtime: Runtime
    remote: RemoteServer
    clock: ShiftedClock
    _clients: list[Any] = field(default_factory=list)

    @property
    def writes(self) -> list[dict[str, Any]]:
        writes: list[dict[str, Any]] = self.ops_app.state.writes
        return writes

    async def proposer_token(self, sub: str = PROPOSER, scope: str = Scopes.REBOOKING_WRITE) -> str:
        return await self.idp.user_token(sub, scope=scope)

    async def grant(
        self,
        proposal: dict[str, Any],
        *,
        sub: str = APPROVER,
        scope: str = Scopes.REBOOKING_APPROVE,
        audience: str = APPROVAL_AUDIENCE,
        expires_in: int = 300,
        details: list[dict[str, Any]] | None = None,
    ) -> str:
        """An approval grant, as a person signing in to approve `proposal` would be issued."""
        return await self.idp.user_token(
            sub,
            scope=scope,
            audience=audience,
            expires_in=expires_in,
            authorization_details=(
                [{"type": APPROVAL_DETAIL_TYPE, "proposal": proposal}]
                if details is None
                else details
            ),
        )

    @asynccontextmanager
    async def client(self, token: str, *, can_elicit: bool = True) -> AsyncIterator[Client]:
        async with (
            httpx2.AsyncClient(
                transport=httpx2.ASGITransport(app=self.remote.app),
                headers={"Authorization": f"Bearer {token}"},
            ) as http_client,
            Client(
                streamable_http_client(f"{BASE_URL}/mcp", http_client=http_client),
                elicitation_callback=_unused_elicitation if can_elicit else None,
            ) as client,
        ):
            yield client


@asynccontextmanager
async def held(manager: Any) -> AsyncIterator[Any]:
    """Enter `manager` in a task of its own and hand its value to the caller.

    The SDK client owns a task group, which must be exited by the task that
    entered it. pytest-asyncio sets a fixture up and tears it down in different
    tasks, so a fixture cannot hold the client open directly.
    """
    ready: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
    stop = asyncio.Event()

    async def hold() -> None:
        try:
            async with manager as value:
                ready.set_result(value)
                await stop.wait()
        except BaseException as exc:
            if not ready.done():
                ready.set_exception(exc)
            raise

    task = asyncio.create_task(hold())
    try:
        yield await ready
    finally:
        stop.set()
        await task


async def propose(
    client: Client,
    arguments: dict[str, Any] | None = None,
    *,
    answer: ElicitResult | None = None,
    state: str | None = None,
) -> CallToolResult | InputRequiredResult:
    """One `tools/call` round, without the SDK's automatic retry loop."""
    return await client.session.call_tool(
        "propose_rebooking",
        ARGS if arguments is None else arguments,
        input_responses=None if answer is None else {"approval": answer},
        request_state=state,
        allow_input_required=True,
    )


def asked(result: CallToolResult | InputRequiredResult) -> InputRequiredResult:
    assert isinstance(result, InputRequiredResult), f"expected an approval request: {result!r}"
    return result


def payload(result: CallToolResult | InputRequiredResult) -> dict[str, Any]:
    assert isinstance(result, CallToolResult), f"expected a tool result: {result!r}"
    structured = result.structured_content
    assert isinstance(structured, dict), f"no structured payload in {result!r}"
    return structured


def proposal_of(request: InputRequiredResult) -> dict[str, Any]:
    """The canonical proposal the server asks an approver to approve, read from the form."""
    assert request.input_requests is not None
    params = request.input_requests["approval"].params
    assert isinstance(params, ElicitRequestFormParams)
    proposal: dict[str, Any] = json.loads(
        params.requested_schema["properties"]["proposal"]["default"]
    )
    return proposal


def accept(grant: str) -> ElicitResult:
    return ElicitResult(action="accept", content={"approval_token": grant})


@asynccontextmanager
async def write_stack(
    settings: Settings,
    *,
    manifests: dict[str, list[dict[str, Any]]] | None = None,
    **overrides: Any,
) -> AsyncIterator[WriteStack]:
    clock = ShiftedClock()
    idp = LocalIdp()
    ops_app = create_app(
        OpsApiConfig(issuer=ISSUER, jwks_url=JWKS_URL, audience=OPS_AUDIENCE),
        jwks_transport=idp.transport,
        manifests=manifests,
    )
    ops = OpsTap(httpx.ASGITransport(app=ops_app))
    configured = remote_settings(settings).model_copy(
        update={
            "auth_token_url": TOKEN_URL,
            "auth_client_id": SERVER,
            "auth_client_secret": idp.secrets[SERVER],
            "ops_api_base_url": OPS_URL,
            "ops_api_audience": OPS_AUDIENCE,
            "approval_audience": APPROVAL_AUDIENCE,
            **overrides,
        }
    )
    install(configured)  # builds the airport database
    runtime = Runtime.build(configured, idp_transport=idp.transport, ops_transport=ops, clock=clock)
    set_runtime(runtime)
    try:
        async with serve(configured, idp.transport) as remote:
            yield WriteStack(
                idp=idp, ops_app=ops_app, ops=ops, runtime=runtime, remote=remote, clock=clock
            )
    finally:
        set_runtime(None)
