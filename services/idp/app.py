"""The local IdP as an HTTP service.

    uv run uvicorn --factory services.idp.app:create_app --port 9000

A test fixture, not an identity provider. `/dev/*` mints a token for any user on
request, with no sign-in. Never expose this beyond localhost or a compose network.
"""

from __future__ import annotations

import base64
import binascii
import time
from collections.abc import Callable
from typing import Any

from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from .config import ClientConfig, IdpConfig
from .provider import (
    ACCESS_TOKEN_TYPE,
    TOKEN_EXCHANGE_GRANT,
    IdentityProvider,
    IssuedToken,
    OAuthError,
)

# RFC 6749 section 5.1: token responses must not be cached.
_NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}


class UserTokenRequest(BaseModel):
    sub: str
    audience: str
    scope: str
    client_id: str
    expires_in: int | None = None
    authorization_details: list[dict[str, Any]] | None = None


class ExpireRequest(BaseModel):
    token: str


def create_app(
    config: IdpConfig | None = None,
    *,
    clock: Callable[[], float] = time.time,
    private_key: rsa.RSAPrivateKey | None = None,
) -> FastAPI:
    resolved = config or IdpConfig()
    idp = IdentityProvider(resolved, clock=clock, private_key=private_key)
    app = FastAPI(title="flightops local IdP", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.idp = idp
    issuer = resolved.issuer.rstrip("/")

    @app.exception_handler(OAuthError)
    async def oauth_error(_request: Request, exc: OAuthError) -> JSONResponse:
        headers = dict(_NO_STORE)
        if exc.status == 401:
            headers["WWW-Authenticate"] = 'Basic realm="idp"'
        return JSONResponse(
            {"error": exc.error, "error_description": exc.description},
            status_code=exc.status,
            headers=headers,
        )

    @app.get("/.well-known/jwks.json")
    async def jwks() -> dict[str, Any]:
        return idp.jwks()

    @app.get("/.well-known/openid-configuration")
    @app.get("/.well-known/oauth-authorization-server")
    async def metadata() -> dict[str, Any]:
        return {
            "issuer": resolved.issuer,
            "jwks_uri": f"{issuer}/.well-known/jwks.json",
            "token_endpoint": f"{issuer}/token",
            "revocation_endpoint": f"{issuer}/revoke",
            "grant_types_supported": ["client_credentials", TOKEN_EXCHANGE_GRANT],
            "token_endpoint_auth_methods_supported": ["client_secret_basic", "client_secret_post"],
            "id_token_signing_alg_values_supported": ["RS256"],
            "response_types_supported": ["token"],
        }

    @app.post("/token")
    async def token(request: Request) -> JSONResponse:
        form = await _form(request)
        client = _authenticate(idp, request, form)
        grant_type = form.get("grant_type")
        audience = form.get("audience") or form.get("resource")

        if grant_type == "client_credentials":
            issued = idp.client_credentials(client, scope=form.get("scope"), audience=audience)
            return _token_response(issued)
        if grant_type == TOKEN_EXCHANGE_GRANT:
            issued = idp.exchange(
                client,
                subject_token=form.get("subject_token"),
                subject_token_type=form.get("subject_token_type"),
                audience=audience,
                scope=form.get("scope"),
                requested_token_type=form.get("requested_token_type"),
            )
            return _token_response(issued, issued_token_type=ACCESS_TOKEN_TYPE)
        raise OAuthError("unsupported_grant_type", "use client_credentials or token-exchange")

    @app.post("/revoke")
    async def revoke(request: Request) -> JSONResponse:
        form = await _form(request)
        client = _authenticate(idp, request, form)
        idp.revoke(client, form.get("token") or "")
        return JSONResponse({}, headers=_NO_STORE)

    @app.post("/dev/user-token")
    async def dev_user_token(body: UserTokenRequest) -> JSONResponse:
        issued = idp.mint_user_token(
            sub=body.sub,
            audience=body.audience,
            scope=body.scope,
            client_id=body.client_id,
            expires_in=body.expires_in,
            authorization_details=body.authorization_details,
        )
        return _token_response(issued)

    @app.post("/dev/expire")
    async def dev_expire(body: ExpireRequest) -> JSONResponse:
        return JSONResponse({"expired": idp.expire(body.token)}, headers=_NO_STORE)

    return app


async def _form(request: Request) -> dict[str, str]:
    form = await request.form()
    return {name: value for name, value in form.items() if isinstance(value, str)}


def _authenticate(idp: IdentityProvider, request: Request, form: dict[str, str]) -> ClientConfig:
    """client_secret_basic if the header is present, otherwise client_secret_post."""
    header = request.headers.get("authorization", "")
    if header.lower().startswith("basic "):
        try:
            decoded = base64.b64decode(header[6:], validate=True).decode()
        except (binascii.Error, UnicodeDecodeError):
            decoded = ""
        client_id, _, client_secret = decoded.partition(":")
        return idp.authenticate_client(client_id, client_secret)
    return idp.authenticate_client(form.get("client_id"), form.get("client_secret"))


def _token_response(issued: IssuedToken, **extra: str) -> JSONResponse:
    return JSONResponse(
        {
            "access_token": issued.access_token,
            "token_type": "Bearer",
            "expires_in": issued.expires_in,
            "scope": issued.scope,
            **extra,
        },
        headers=_NO_STORE,
    )
