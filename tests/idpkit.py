"""The local IdP, in process, with the client topology the matrix is tested against.

Client secrets are generated per run. Nothing here is a credential for anything.
"""

from __future__ import annotations

import functools
import secrets
import time
from collections.abc import Callable
from typing import Any

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from pydantic import SecretStr

from services.idp.app import create_app
from services.idp.config import ClientConfig, IdpConfig
from services.idp.provider import ACCESS_TOKEN_TYPE, TOKEN_EXCHANGE_GRANT, IdentityProvider

from .authkit import ISSUER, RESOURCE

MCP_AUDIENCE = RESOURCE
OPS_AUDIENCE = "api://flightops-ops"
OTHER_AUDIENCE = "api://reporting"

AGENT = "flightops-agent"  # a service principal that calls the MCP server
SERVER = "flightops-mcp"  # the MCP server itself, as a confidential client
BYSTANDER = "reporting-job"  # an unrelated API with exchange rights of its own
BATCH = "batch-job"  # a service principal that holds the passenger permission

AGENT_ROLES = [
    "flightops.reference.read",
    "flightops.weather.read",
    "flightops.traffic.read",
    "flightops.status.read",
]
PASSENGERS_READ = "flightops.passengers.read"
REBOOKING_WRITE = "flightops.rebooking.write"
CREW_READ = "flightops.crew.read"  # a scope the ops API does not accept for passengers
USER_SCOPES = "flightops.reference.read flightops.weather.read flightops.passengers.read"


@functools.cache
def _shared_key() -> rsa.RSAPrivateKey:
    """One signing key for the run. Generating one per test triples the suite's runtime."""
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


class LocalIdp:
    def __init__(self, clock: Callable[[], float] = time.time, *, own_key: bool = False) -> None:
        self.secrets = {
            name: secrets.token_urlsafe(24) for name in (AGENT, SERVER, BYSTANDER, BATCH)
        }
        self.config = IdpConfig(
            issuer=ISSUER,
            clients=[
                ClientConfig(
                    client_id=AGENT,
                    client_secret=SecretStr(self.secrets[AGENT]),
                    app_roles={MCP_AUDIENCE: AGENT_ROLES},
                ),
                ClientConfig(
                    client_id=SERVER,
                    client_secret=SecretStr(self.secrets[SERVER]),
                    resource=MCP_AUDIENCE,
                    exchange_targets={OPS_AUDIENCE: [PASSENGERS_READ, REBOOKING_WRITE]},
                ),
                ClientConfig(
                    client_id=BYSTANDER,
                    client_secret=SecretStr(self.secrets[BYSTANDER]),
                    resource=OTHER_AUDIENCE,
                    exchange_targets={OPS_AUDIENCE: [PASSENGERS_READ, CREW_READ]},
                ),
                ClientConfig(
                    client_id=BATCH,
                    client_secret=SecretStr(self.secrets[BATCH]),
                    app_roles={MCP_AUDIENCE: [PASSENGERS_READ]},
                ),
            ],
        )
        self.app = create_app(
            self.config, clock=clock, private_key=None if own_key else _shared_key()
        )
        self.provider: IdentityProvider = self.app.state.idp
        self.transport = httpx.ASGITransport(app=self.app)

    def http(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=self.transport, base_url=ISSUER)

    async def token(self, client_id: str, **form: str) -> httpx.Response:
        async with self.http() as client:
            return await client.post(
                "/token", data=form, auth=(client_id, self.secrets.get(client_id, "unknown"))
            )

    async def service_token(self, audience: str = MCP_AUDIENCE, client_id: str = AGENT) -> str:
        response = await self.token(client_id, grant_type="client_credentials", resource=audience)
        assert response.status_code == 200, response.text
        return str(response.json()["access_token"])

    async def user_token(
        self,
        sub: str = "user-1",
        *,
        scope: str = USER_SCOPES,
        audience: str = MCP_AUDIENCE,
        client_id: str = AGENT,
        expires_in: int | None = None,
        authorization_details: list[dict[str, Any]] | None = None,
    ) -> str:
        body: dict[str, Any] = {
            "sub": sub,
            "audience": audience,
            "scope": scope,
            "client_id": client_id,
        }
        if expires_in is not None:
            body["expires_in"] = expires_in
        if authorization_details is not None:
            body["authorization_details"] = authorization_details
        async with self.http() as client:
            response = await client.post("/dev/user-token", json=body)
        assert response.status_code == 200, response.text
        return str(response.json()["access_token"])

    async def exchange(
        self,
        subject_token: str,
        /,
        *,
        client_id: str = SERVER,
        audience: str | None = OPS_AUDIENCE,
        scope: str | None = None,
        **overrides: str,
    ) -> httpx.Response:
        form = {
            "grant_type": TOKEN_EXCHANGE_GRANT,
            "subject_token": subject_token,
            "subject_token_type": ACCESS_TOKEN_TYPE,
        }
        if audience is not None:
            form["audience"] = audience
        if scope is not None:
            form["scope"] = scope
        form.update(overrides)
        return await self.token(client_id, **form)

    async def revoke(self, token: str, client_id: str = AGENT) -> httpx.Response:
        async with self.http() as client:
            return await client.post(
                "/revoke", data={"token": token}, auth=(client_id, self.secrets[client_id])
            )

    async def expire(self, token: str) -> httpx.Response:
        async with self.http() as client:
            return await client.post("/dev/expire", json={"token": token})


def claims_of(token: str) -> dict[str, Any]:
    """Read a token's claims without verifying it; for assertions only."""
    claims: dict[str, Any] = jwt.decode(token, options={"verify_signature": False})
    return claims
