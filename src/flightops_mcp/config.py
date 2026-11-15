"""Typed configuration. Everything is environment-driven; nothing is hardcoded secrets."""

from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_USER_AGENT = "flightops-mcp/0.1 (+https://github.com/KristofferHM/flightops-mcp)"


class Settings(BaseSettings):
    """Runtime configuration, loaded from the environment or a local .env file."""

    model_config = SettingsConfigDict(
        env_prefix="FLIGHTOPS_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- OpenSky Network (OAuth2 client credentials) -------------------------
    opensky_client_id: str | None = None
    opensky_client_secret: str | None = None
    opensky_base_url: str = "https://opensky-network.org/api"
    opensky_auth_url: str = (
        "https://auth.opensky-network.org/auth/realms/opensky-network/protocol/openid-connect/token"
    )

    # --- AviationWeather.gov (no authentication) ----------------------------
    avwx_base_url: str = "https://aviationweather.gov/api/data"

    # --- Reference data ------------------------------------------------------
    airports_db: Path = Field(default=Path("data/airports.sqlite3"))

    # --- HTTP behaviour ------------------------------------------------------
    user_agent: str = DEFAULT_USER_AGENT
    http_timeout_s: float = 15.0
    max_retries: int = 3
    backoff_base_s: float = 0.25
    # A Retry-After longer than this is surfaced to the agent as a tool error
    # instead of blocking the request: waiting 40 minutes inside a tool call is
    # worse for the caller than an honest "come back later".
    max_rate_limit_wait_s: float = 5.0

    # --- Token lifecycle -----------------------------------------------------
    token_expiry_skew_s: float = 30.0

    # --- Local credit guard --------------------------------------------------
    # OpenSky allows 4,000 credits/day per endpoint family for a standard user.
    # This is a *local* ceiling so one runaway agent loop cannot drain the day.
    credit_budget_per_hour: int = 400

    log_level: str = "INFO"
