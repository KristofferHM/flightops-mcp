"""Token issuance rules for the local IdP, free of any HTTP framework.

The claims follow Entra's access-token shape (`aud`, `scp`, `roles`, `tid`,
`azp`) so the resource server is tested against what it will meet in production.
Where this stub differs from Entra is recorded in docs/adr/0001.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import uuid
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

from .config import ClientConfig, IdpConfig

ACCESS_TOKEN_TYPE = "urn:ietf:params:oauth:token-type:access_token"  # noqa: S105 - a URN
TOKEN_EXCHANGE_GRANT = "urn:ietf:params:oauth:grant-type:token-exchange"  # noqa: S105 - a URN
ALGORITHM = "RS256"


class OAuthError(Exception):
    """An RFC 6749 section 5.2 error response."""

    def __init__(self, error: str, description: str, status: int = 400) -> None:
        super().__init__(f"{error}: {description}")
        self.error = error
        self.description = description
        self.status = status


@dataclass(frozen=True, slots=True)
class IssuedToken:
    access_token: str
    expires_in: int
    scope: str
    claims: dict[str, Any]


class IdentityProvider:
    """Signs tokens with a key generated at start-up and tracks what it has revoked.

    The key lives only in this process. Restarting the IdP invalidates every
    token it issued, which is the right behaviour for a test fixture and the
    reason no signing key exists anywhere in the repository.
    """

    def __init__(self, config: IdpConfig, *, clock: Callable[[], float] = time.time) -> None:
        self.config = config
        self._clock = clock
        self._private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        public_jwk: dict[str, Any] = json.loads(
            jwt.algorithms.RSAAlgorithm.to_jwk(self._private_key.public_key())
        )
        self.kid = _thumbprint(public_jwk)
        # Publish the minimum: PyJWT also emits `key_ops`, which RFC 7517 says
        # should not appear next to `use`.
        self._public_jwk = {
            "kty": "RSA",
            "n": public_jwk["n"],
            "e": public_jwk["e"],
            "kid": self.kid,
            "use": "sig",
            "alg": ALGORITHM,
        }
        # jti -> why the token is dead ("revoked" or "expired").
        self._dead: dict[str, str] = {}
        self.issued: Counter[str] = Counter()

    # --- published metadata -------------------------------------------------
    def jwks(self) -> dict[str, Any]:
        return {"keys": [self._public_jwk]}

    # --- client authentication ----------------------------------------------
    def authenticate_client(self, client_id: str | None, client_secret: str | None) -> ClientConfig:
        client = self.config.client(client_id) if client_id else None
        # Compare against a dummy when the client is unknown, so an unknown id
        # and a wrong secret take the same time and return the same error.
        expected = client.client_secret.get_secret_value() if client else uuid.uuid4().hex
        matches = hmac.compare_digest(expected.encode(), (client_secret or "").encode())
        if client is None or not matches:
            raise OAuthError("invalid_client", "client authentication failed", status=401)
        return client

    # --- grants --------------------------------------------------------------
    def client_credentials(
        self, client: ClientConfig, *, scope: str | None, audience: str | None
    ) -> IssuedToken:
        """A service principal acting as itself. Permissions arrive in `roles`."""
        requested = (scope or "").split()
        # Entra's form: `scope=<app id uri>/.default` names the audience and
        # means "every application permission this client has been granted".
        defaults = [item for item in requested if item.endswith("/.default")]
        if defaults:
            if len(requested) > 1:
                raise OAuthError("invalid_scope", ".default cannot be combined with other scopes")
            audience = defaults[0].removesuffix("/.default")
            requested = []
        if not audience:
            raise OAuthError("invalid_target", "name the audience with `resource` or `/.default`")
        granted = client.app_roles.get(audience)
        if granted is None:
            raise OAuthError(
                "invalid_target", "this client has no application permissions on that audience"
            )
        roles = requested or granted
        excess = sorted(set(roles) - set(granted))
        if excess:
            raise OAuthError("invalid_scope", f"not granted to this client: {' '.join(excess)}")

        self.issued["client_credentials"] += 1
        return self._issue(
            {
                "aud": audience,
                "sub": client.client_id,
                "azp": client.client_id,
                "idtyp": "app",
                "roles": roles,
            },
            ttl_s=self.config.access_token_ttl_s,
            scope=" ".join(roles),
        )

    def exchange(
        self,
        client: ClientConfig,
        *,
        subject_token: str | None,
        subject_token_type: str | None,
        audience: str | None,
        scope: str | None,
        requested_token_type: str | None = None,
    ) -> IssuedToken:
        """RFC 8693 on-behalf-of: a user's token in, a narrower token for one API out."""
        if not subject_token:
            raise OAuthError("invalid_request", "subject_token is required")
        if subject_token_type != ACCESS_TOKEN_TYPE:
            raise OAuthError("invalid_request", "subject_token_type must be an access token")
        if requested_token_type not in (None, ACCESS_TOKEN_TYPE):
            raise OAuthError("invalid_request", "only access tokens can be issued")
        if not client.exchange_targets:
            raise OAuthError("unauthorized_client", "this client may not exchange tokens")

        subject = self._verify_own(subject_token)
        if subject.get("aud") != client.resource:
            # The confused-deputy check: a client exchanges tokens that were
            # sent *to it*. One it merely got hold of proves nothing.
            raise OAuthError("invalid_grant", "subject token was not issued to this client")
        subject_scopes = str(subject.get("scp") or "").split()
        if subject.get("idtyp") == "app" or not subject_scopes:
            raise OAuthError("invalid_grant", "on-behalf-of needs a delegated user token")

        if not audience:
            raise OAuthError("invalid_target", "audience is required")
        allowed = client.exchange_targets.get(audience)
        if allowed is None:
            raise OAuthError("invalid_target", "this client may not exchange for that audience")

        # The narrowing rule. A scope survives only if the user's token carries it
        # *and* the target accepts it; asking for anything else is an error rather
        # than a silent drop, so an escalation attempt is visible to its caller.
        ceiling = [item for item in subject_scopes if item in allowed]
        requested = scope.split() if scope else ceiling
        excess = sorted(set(requested) - set(ceiling))
        if excess:
            raise OAuthError(
                "invalid_scope", f"exceeds the subject token's authority: {' '.join(excess)}"
            )
        if not requested:
            raise OAuthError(
                "invalid_scope", "the subject token has no scope valid for that audience"
            )

        # Nor may the exchanged token outlive the one it was derived from.
        remaining = int(subject["exp"] - self._clock())
        self.issued["token_exchange"] += 1
        return self._issue(
            {
                "aud": audience,
                "sub": subject["sub"],
                "oid": subject.get("oid", subject["sub"]),
                "azp": client.client_id,
                "scp": " ".join(requested),
                # RFC 8693 section 4.1: who is acting for the subject.
                "act": {"sub": client.client_id},
            },
            ttl_s=min(self.config.exchanged_token_ttl_s, remaining),
            scope=" ".join(requested),
        )

    # --- test controls -------------------------------------------------------
    def mint_user_token(
        self,
        *,
        sub: str,
        audience: str,
        scope: str,
        client_id: str,
        expires_in: int | None = None,
    ) -> IssuedToken:
        """A delegated token, standing in for the authorization-code flow.

        A real IdP issues this after a user signs in through a browser. The stub
        skips the browser so the matrix can run unattended; a negative
        `expires_in` yields a correctly signed token that is already expired.
        """
        self.issued["user_token"] += 1
        return self._issue(
            {"aud": audience, "sub": sub, "oid": sub, "azp": client_id, "scp": scope},
            ttl_s=self.config.access_token_ttl_s if expires_in is None else expires_in,
            scope=scope,
        )

    def revoke(self, client: ClientConfig, token: str) -> None:
        """RFC 7009. Silent on every failure: the response never confirms a token."""
        try:
            claims = self._decode(token)
        except jwt.PyJWTError:
            return
        if claims.get("azp") != client.client_id:
            return  # a client revokes its own tokens, not another client's
        self._dead[str(claims["jti"])] = "revoked"

    def expire(self, token: str) -> bool:
        """Treat a token as expired from now on, whatever its `exp` says."""
        try:
            claims = self._decode(token)
        except jwt.PyJWTError:
            return False
        self._dead[str(claims["jti"])] = "expired"
        return True

    # --- internals -----------------------------------------------------------
    def _issue(self, claims: dict[str, Any], *, ttl_s: int, scope: str) -> IssuedToken:
        now = int(self._clock())
        full = {
            "iss": self.config.issuer,
            "tid": self.config.tenant_id,
            "iat": now,
            "nbf": now,
            "exp": now + ttl_s,
            "jti": uuid.uuid4().hex,
            "ver": "2.0",
            **claims,
        }
        token = jwt.encode(full, self._private_key, algorithm=ALGORITHM, headers={"kid": self.kid})
        return IssuedToken(access_token=token, expires_in=ttl_s, scope=scope, claims=full)

    def _decode(self, token: str) -> dict[str, Any]:
        """Signature and issuer only; liveness is the caller's question."""
        claims: dict[str, Any] = jwt.decode(
            token,
            self._private_key.public_key(),
            algorithms=[ALGORITHM],
            issuer=self.config.issuer,
            options={
                "require": ["exp", "iss", "sub", "jti"],
                "verify_exp": False,
                "verify_nbf": False,
                "verify_iat": False,
                "verify_aud": False,
            },
        )
        return claims

    def _verify_own(self, token: str) -> dict[str, Any]:
        try:
            claims = self._decode(token)
        except jwt.PyJWTError as exc:
            raise OAuthError("invalid_grant", "subject token is not valid") from exc
        dead = self._dead.get(str(claims["jti"]))
        if dead:
            raise OAuthError("invalid_grant", f"subject token is {dead}")
        if claims["exp"] <= self._clock():
            raise OAuthError("invalid_grant", "subject token is expired")
        return claims


def _thumbprint(jwk: dict[str, Any]) -> str:
    """RFC 7638 key id: stable for a key, different for every key."""
    canonical = json.dumps(
        {name: jwk[name] for name in ("e", "kty", "n")}, separators=(",", ":"), sort_keys=True
    )
    return (
        base64.urlsafe_b64encode(hashlib.sha256(canonical.encode()).digest()).rstrip(b"=").decode()
    )
