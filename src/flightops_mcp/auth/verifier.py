"""Bearer-token verification for the resource-server role.

The verifier answers one question: was this token signed by our issuer, for
this server, and is it still inside its validity window? A rejection never
raises and never says why to the caller; the reason goes to the log, without
the token.

"For this server" is the audience check, and it is what stops token
passthrough at the front door. A token minted for another API is perfectly
valid: right issuer, good signature, not expired. Accepting it would let anyone
holding a token for *any* service behind this issuer use this one, and would
make this server a place to launder tokens it was never meant to see.

The algorithm is pinned to RS256 before any key is looked up. Reading `alg` from
the token and trusting it is the classic downgrade: `none` skips the signature,
and `HS256` verifies against the *public* key used as an HMAC secret.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

import jwt
from mcp.server.auth.provider import AccessToken

from ..telemetry import get_logger
from .jwks import JwksCache

ALGORITHM = "RS256"

# Claims copied onto the AccessToken. Deliberately a short allow-list: the rest
# of the token can carry user attributes that nothing downstream needs.
_KEPT_CLAIMS = ("iss", "aud", "tid", "azp", "jti", "act")


class TokenRejected(Exception):
    """An inbound token failed verification. `reason` is a stable log key."""

    def __init__(self, reason: str, **context: Any) -> None:
        super().__init__(reason)
        self.reason = reason
        self.context = context


class JwtVerifier:
    """Validates RS256 JWTs against a JWKS endpoint. Implements `TokenVerifier`."""

    def __init__(
        self,
        *,
        issuer: str,
        audience: str,
        jwks: JwksCache,
        clock: Callable[[], float] = time.time,
        clock_skew_s: float = 30.0,
    ) -> None:
        self._issuer = issuer
        self._audience = _canonical(audience)
        self._jwks = jwks
        self._clock = clock
        self._clock_skew_s = clock_skew_s
        self._log = get_logger(__name__)

    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            claims = await self._verify(token)
        except TokenRejected as exc:
            self._log.warning("token_rejected", reason=exc.reason, **exc.context)
            return None
        return AccessToken(
            token=token,
            client_id=str(claims.get("azp") or claims.get("appid") or claims["sub"]),
            scopes=scopes_from_claims(claims),
            expires_at=int(claims["exp"]),
            # Reported so the SDK's bearer middleware can make the same audience
            # decision independently: two checks, either of which is sufficient.
            resource=self._audience,
            subject=str(claims["sub"]),
            claims={name: claims[name] for name in _KEPT_CLAIMS if name in claims},
        )

    async def _verify(self, token: str) -> dict[str, Any]:
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError as exc:
            raise TokenRejected("malformed") from exc

        if header.get("alg") != ALGORITHM:
            raise TokenRejected("algorithm_not_allowed", alg=str(header.get("alg")))
        kid = header.get("kid")
        if not isinstance(kid, str) or not kid:
            raise TokenRejected("missing_key_id")

        key = await self._jwks.key_for(kid)
        if key is None:
            raise TokenRejected("unknown_key_id", kid=kid)

        try:
            claims: dict[str, Any] = jwt.decode(
                token,
                key,
                algorithms=[ALGORITHM],
                issuer=self._issuer,
                options={
                    "require": ["exp", "iat", "iss", "sub"],
                    # The validity window is checked below against the injected
                    # clock, so the suite can move time instead of sleeping.
                    "verify_exp": False,
                    "verify_nbf": False,
                    "verify_iat": False,
                    "verify_aud": False,
                },
            )
        except jwt.InvalidSignatureError as exc:
            raise TokenRejected("bad_signature", kid=kid) from exc
        except jwt.InvalidIssuerError as exc:
            raise TokenRejected("wrong_issuer") from exc
        except jwt.MissingRequiredClaimError as exc:
            raise TokenRejected("missing_claim", claim=exc.claim) from exc
        except jwt.PyJWTError as exc:
            raise TokenRejected("invalid", error_class=type(exc).__name__) from exc

        if self._audience not in _audiences(claims):
            # `aud` is logged: it names the service the token was meant for,
            # which is exactly what an operator needs to see, and it is not secret.
            raise TokenRejected("wrong_audience", aud=claims.get("aud"), expected=self._audience)

        now = self._clock()
        exp = _numeric(claims, "exp")
        # No leeway on expiry: a short-lived token that outlives its `exp` by the
        # skew allowance is the window a stolen token gets used in.
        if exp <= now:
            raise TokenRejected("expired", expired_for_s=round(now - exp, 1))
        if "nbf" in claims and _numeric(claims, "nbf") > now + self._clock_skew_s:
            raise TokenRejected("not_yet_valid")
        return claims


def scopes_from_claims(claims: dict[str, Any]) -> list[str]:
    """Granted permissions, from whichever claim the issuer uses.

    Entra puts delegated permissions in `scp` (space-delimited) and application
    permissions in `roles` (a list); RFC 9068 uses `scope`. All three are read so
    a user token and a service-principal token go through the same scope check.
    """
    granted: list[str] = []
    for name in ("scp", "scope"):
        value = claims.get(name)
        if isinstance(value, str):
            granted.extend(value.split())
    roles = claims.get("roles")
    if isinstance(roles, list):
        granted.extend(str(role) for role in roles)
    return list(dict.fromkeys(granted))


def _canonical(audience: str) -> str:
    """A trailing slash does not make a different resource; nothing else is normalised."""
    return audience.rstrip("/")


def _audiences(claims: dict[str, Any]) -> list[str]:
    """`aud` as a list. RFC 7519 allows a string or an array; absent means none."""
    value = claims.get("aud")
    values = [value] if isinstance(value, str) else value if isinstance(value, list) else []
    return [_canonical(item) for item in values if isinstance(item, str)]


def _numeric(claims: dict[str, Any], name: str) -> float:
    value = claims.get(name)
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TokenRejected("invalid", claim=name)
    return float(value)
