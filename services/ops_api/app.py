"""A mock internal operations API that holds passenger data.

    uv run uvicorn --factory services.ops_api.app:create_app --port 9100

It exists to be the downstream of an on-behalf-of call, so it is strict about
exactly one thing: it accepts only tokens that were *exchanged* for it. A token
must be signed by the IdP, name this API as its audience, carry the passenger
scope as a delegated permission, and carry an `act` claim naming the service
that exchanged it. The user's original token fails the audience check; a
service principal's token fails the delegation check.

It also takes one write, `POST /rebookings`, guarded by an `Idempotency-Key`. The
rules for it are in `rebookings.py`; this module only authenticates the caller
and keeps the audit trail.

Every passenger here is invented. The records are deliberately PII-shaped
(names, contact details, booking references) so that redaction and data
minimisation upstream of this service are tested against realistic fields.
"""

from __future__ import annotations

import copy
import json
import time
from collections.abc import Callable
from typing import Any

import httpx
import jwt
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from pydantic_settings import BaseSettings, SettingsConfigDict

from .rebookings import Problem, RebookingLedger

PASSENGERS_READ = "flightops.passengers.read"
REBOOKING_WRITE = "flightops.rebooking.write"


class OpsApiConfig(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="OPS_", extra="ignore")

    issuer: str = "http://127.0.0.1:9000"
    jwks_url: str = "http://127.0.0.1:9000/.well-known/jwks.json"
    audience: str = "api://flightops-ops"


class Refused(Exception):
    def __init__(self, status: int, error: str, description: str) -> None:
        super().__init__(description)
        self.status = status
        self.error = error
        self.description = description


def _passenger(
    number: int,
    *,
    checked_in: bool = True,
    connecting_flight: str | None = None,
    connection_minutes: int | None = None,
    needs_assistance: bool = False,
) -> dict[str, Any]:
    return {
        "passenger_name": f"Example Passenger {number:02d}",
        "email": f"passenger{number:02d}@example.com",
        # 070-1740 6xx is the range the Swedish regulator reserves for fiction.
        "phone": f"+46 70 1740 6{number:02d}",
        "booking_ref": f"XMPL{number:02d}",
        "date_of_birth": f"19{60 + number}-01-{number:02d}",
        "seat": f"{number + 3}{'ABCDEF'[number % 6]}",
        "checked_in": checked_in,
        "connecting_flight": connecting_flight,
        "connection_minutes": connection_minutes,
        "needs_assistance": needs_assistance,
    }


DEFAULT_MANIFESTS: dict[str, list[dict[str, Any]]] = {
    "SK1429": [
        _passenger(1, connecting_flight="SK903", connection_minutes=35),
        _passenger(2, connecting_flight="SK903", connection_minutes=35, needs_assistance=True),
        _passenger(3, connecting_flight="LH803", connection_minutes=95),
        _passenger(4),
        _passenger(5, checked_in=False),
        _passenger(6, needs_assistance=True),
    ],
    # A positioning flight: known to the system, nobody booked on it.
    "SK9001": [],
}

# Free seats on the flights passengers can be moved to.
DEFAULT_SEATS: dict[str, int] = {"SK1431": 4, "SK1433": 1}


def create_app(
    config: OpsApiConfig | None = None,
    *,
    jwks_transport: httpx.AsyncBaseTransport | None = None,
    manifests: dict[str, list[dict[str, Any]]] | None = None,
    seats: dict[str, int] | None = None,
    clock: Callable[[], float] = time.time,
) -> FastAPI:
    resolved = config or OpsApiConfig()
    # Copied: a rebooking moves passengers, and one app must not edit another's data.
    data = copy.deepcopy(DEFAULT_MANIFESTS if manifests is None else manifests)
    ledger = RebookingLedger(data, dict(DEFAULT_SEATS if seats is None else seats), clock)
    app = FastAPI(title="flightops mock ops API", docs_url=None, redoc_url=None, openapi_url=None)
    # Who was served, as the downstream saw it: the user, and the service acting for them.
    app.state.audit = []
    # One entry per rebooking actually written. A replay adds nothing here.
    app.state.writes = ledger.writes
    keys: dict[str, Any] = {}

    async def signing_key(kid: str) -> Any:
        if kid not in keys:
            async with httpx.AsyncClient(transport=jwks_transport, timeout=5.0) as client:
                response = await client.get(resolved.jwks_url)
                response.raise_for_status()
            for entry in response.json()["keys"]:
                keys[entry["kid"]] = jwt.PyJWK(entry, algorithm="RS256").key
        return keys.get(kid)

    async def authorize(request: Request, scope: str = PASSENGERS_READ) -> dict[str, Any]:
        header = request.headers.get("authorization", "")
        if not header.lower().startswith("bearer "):
            raise Refused(401, "invalid_token", "a bearer token is required")
        token = header[7:]
        try:
            unverified = jwt.get_unverified_header(token)
            if unverified.get("alg") != "RS256":
                raise jwt.InvalidAlgorithmError("only RS256 is accepted")
            key = await signing_key(str(unverified.get("kid")))
            if key is None:
                raise jwt.InvalidKeyError("unknown signing key")
            claims: dict[str, Any] = jwt.decode(
                token,
                key,
                algorithms=["RS256"],
                issuer=resolved.issuer,
                audience=resolved.audience,
                options={"require": ["exp", "iss", "aud", "sub"]},
            )
        except (jwt.PyJWTError, httpx.HTTPError, KeyError, ValueError) as exc:
            raise Refused(401, "invalid_token", "the token is not valid for this API") from exc

        actor = claims.get("act")
        if not isinstance(actor, dict) or not actor.get("sub"):
            # Signed, for us, but not the product of an exchange.
            raise Refused(401, "invalid_token", "only exchanged tokens are accepted")
        if scope not in str(claims.get("scp") or "").split():
            raise Refused(403, "insufficient_scope", f"requires {scope}")
        return claims

    @app.exception_handler(Refused)
    async def refused(_request: Request, exc: Refused) -> JSONResponse:
        return JSONResponse(
            {"error": exc.error, "error_description": exc.description},
            status_code=exc.status,
            headers={"WWW-Authenticate": f'Bearer error="{exc.error}"'},
        )

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/flights/{flight}/passengers")
    async def passengers(flight: str, request: Request) -> JSONResponse:
        claims = await authorize(request)
        app.state.audit.append(
            {"flight": flight, "user": claims["sub"], "actor": claims["act"]["sub"]}
        )
        if flight not in data:
            return JSONResponse({"error": "flight_not_found"}, status_code=404)
        return JSONResponse({"flight": flight, "passengers": data[flight]})

    @app.exception_handler(Problem)
    async def problem(_request: Request, exc: Problem) -> JSONResponse:
        return JSONResponse({"error": exc.error, **exc.detail}, status_code=exc.status)

    @app.post("/rebookings")
    async def rebook(request: Request) -> Response:
        # A token that may read passengers may not move them: a separate scope.
        claims = await authorize(request, REBOOKING_WRITE)
        key = request.headers.get("idempotency-key", "")
        try:
            body = json.loads(await request.body())
        except ValueError:
            raise Problem(400, "invalid_request", field="body") from None
        outcome = ledger.submit(key, body, user=str(claims["sub"]))
        app.state.audit.append(
            {
                "action": "rebooking",
                "flight": body["from_flight"],
                "user": claims["sub"],
                "actor": claims["act"]["sub"],
                "approved_by": body["approved_by"],
                "idempotency_key": key,
                "outcome": "replayed" if outcome.replayed else "executed",
            }
        )
        return Response(
            outcome.body,
            status_code=outcome.status,
            media_type="application/json",
            headers={"Idempotent-Replayed": "true"} if outcome.replayed else None,
        )

    return app
