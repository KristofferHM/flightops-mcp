"""Configuration for the local IdP. Client secrets arrive by environment, never in code."""

from __future__ import annotations

from pydantic import BaseModel, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class ClientConfig(BaseModel):
    """A confidential client (Entra: an app registration with a client secret)."""

    client_id: str
    client_secret: SecretStr
    # audience -> application permissions granted by client_credentials (Entra `roles`).
    app_roles: dict[str, list[str]] = Field(default_factory=dict)
    # The audience this client is the API for. Token exchange only accepts subject
    # tokens issued to it: a client may exchange what was sent to it, nothing else.
    resource: str | None = None
    # audience -> the scopes this client may ask for when exchanging a user token.
    exchange_targets: dict[str, list[str]] = Field(default_factory=dict)


class IdpConfig(BaseSettings):
    """Environment-driven. `IDP_CLIENTS` is a JSON list of `ClientConfig`."""

    model_config = SettingsConfigDict(env_prefix="IDP_", extra="ignore")

    issuer: str = "http://127.0.0.1:9000"
    # Entra's `tid` is a GUID; this one is obviously not a real tenant.
    tenant_id: str = "00000000-0000-4000-8000-000000000001"
    access_token_ttl_s: int = 3600
    # Exchanged tokens are short-lived on purpose: they cannot be revoked at the
    # API that accepts them, so expiry is the only bound on a leaked one.
    exchanged_token_ttl_s: int = 300
    clients: list[ClientConfig] = Field(default_factory=list)

    def client(self, client_id: str) -> ClientConfig | None:
        return next((c for c in self.clients if c.client_id == client_id), None)
