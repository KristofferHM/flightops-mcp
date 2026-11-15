"""Test-side token minting and an in-memory JWKS endpoint.

Keys are generated per test session and never written anywhere: there is no
signing material in the repository, not even a throwaway one.
"""

from __future__ import annotations

import base64
import functools
import hashlib
import hmac
import json
import time
from collections.abc import Callable
from typing import Any

import httpx
import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

ISSUER = "http://idp.test"
JWKS_URL = f"{ISSUER}/.well-known/jwks.json"
RESOURCE = "http://127.0.0.1:8000/mcp"


class SigningKey:
    """An RSA key pair with a key id, able to mint tokens and publish its JWK."""

    def __init__(self, kid: str) -> None:
        self.kid = kid
        self._private = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    @property
    def jwk(self) -> dict[str, Any]:
        document: dict[str, Any] = json.loads(
            jwt.algorithms.RSAAlgorithm.to_jwk(self._private.public_key())
        )
        document.update(kid=self.kid, use="sig", alg="RS256")
        return document

    @property
    def public_pem(self) -> bytes:
        return self._private.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        )

    def mint(
        self,
        *,
        issuer: str = ISSUER,
        subject: str = "user-1",
        audience: str | None = RESOURCE,
        scp: str | None = None,
        roles: list[str] | None = None,
        expires_in: float = 300.0,
        now: float | None = None,
        kid: str | None = None,
        drop: tuple[str, ...] = (),
        **extra: Any,
    ) -> str:
        issued = int(time.time() if now is None else now)
        claims: dict[str, Any] = {
            "iss": issuer,
            "sub": subject,
            "iat": issued,
            "exp": issued + int(expires_in),
        }
        if audience is not None:
            claims["aud"] = audience
        if scp is not None:
            claims["scp"] = scp
        if roles is not None:
            claims["roles"] = roles
        claims.update(extra)
        for name in drop:
            claims.pop(name, None)
        return jwt.encode(
            claims, self._private, algorithm="RS256", headers={"kid": kid or self.kid}
        )


@functools.cache
def signing_key(kid: str) -> SigningKey:
    """One key per id for the whole run; RSA generation is the slow part."""
    return SigningKey(kid)


class JwksEndpoint:
    """A JWKS endpoint as an httpx mock handler: counts fetches, can fail on demand."""

    def __init__(self, *keys: SigningKey) -> None:
        self.keys = list(keys)
        self.calls = 0
        self.failing = False
        self.extra_entries: list[Any] = []

    def __call__(self, _request: httpx.Request) -> httpx.Response:
        self.calls += 1
        if self.failing:
            return httpx.Response(503)
        return httpx.Response(
            200, json={"keys": [key.jwk for key in self.keys] + self.extra_entries}
        )

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def forge(
    header: dict[str, Any],
    claims: dict[str, Any],
    sign: Callable[[bytes], bytes] = lambda _signing_input: b"",
) -> str:
    """Assemble a JWT by hand, for the shapes a real library refuses to produce."""
    signing_input = f"{_b64(json.dumps(header).encode())}.{_b64(json.dumps(claims).encode())}"
    return f"{signing_input}.{_b64(sign(signing_input.encode()))}"


def hs256_with_public_key(key: SigningKey, claims: dict[str, Any]) -> str:
    """The algorithm-confusion token: HS256, keyed with the issuer's *public* key."""
    return forge(
        {"alg": "HS256", "typ": "JWT", "kid": key.kid},
        claims,
        lambda signing_input: hmac.new(key.public_pem, signing_input, hashlib.sha256).digest(),
    )


def tamper(token: str, **changes: Any) -> str:
    """Rewrite claims but keep the original signature."""
    header, payload, signature = token.split(".")
    claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    claims.update(changes)
    return f"{header}.{_b64(json.dumps(claims).encode())}.{signature}"
