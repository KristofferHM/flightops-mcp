"""Typed configuration. Everything is environment-driven; nothing is hardcoded secrets."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_USER_AGENT = "flightops-mcp/0.1 (+https://github.com/KristofferHM/flightops-mcp)"

Edition = Literal["demo", "commercial", "byo"]


class Settings(BaseSettings):
    """Runtime configuration, loaded from the environment or a local .env file."""

    model_config = SettingsConfigDict(
        env_prefix="FLIGHTOPS_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Edition (src/flightops_mcp/edition.py, docs/COMMERCIAL.md) -----------
    # Which licence classes this process may serve. Checked once, before anything
    # is served: a refused source stops the process with the variable that set it.
    edition: Edition = "demo"

    # --- OpenSky Network (OAuth2 client credentials) -------------------------
    opensky_client_id: str | None = None
    opensky_client_secret: str | None = None
    # Opt-in, and ignored once credentials are set. OpenSky serves anonymous callers
    # at 10 s resolution with the most recent vectors only, so an operator must ask
    # for that lower-grade answer rather than receive it by accident.
    opensky_anonymous: bool = False
    opensky_base_url: str = "https://opensky-network.org/api"
    opensky_auth_url: str = (
        "https://auth.opensky-network.org/auth/realms/opensky-network/protocol/openid-connect/token"
    )

    # --- Schiphol Public Flight API (OAuth2 client credentials + audience) ---
    # The vendor's documented shape (developer.schiphol.nl, API Consumer Migration
    # Guide, 2026-07-20): a token from Auth0 for one audience, 30 minutes, sent as a
    # bearer. No live key has been used with this adapter: see docs/adr/0005.
    schiphol_client_id: str | None = None
    schiphol_client_secret: str | None = None
    schiphol_base_url: str = "https://api.schiphol.nl/public"
    schiphol_auth_url: str = "https://api.auth.schiphol.nl/oauth/token"
    # Must match the tier called, exactly; a token is scoped to one audience.
    schiphol_audience: str = "https://api.schiphol.nl/public"
    # The vendor's advice is to refresh 60 s before expiry, not on a 401.
    schiphol_token_refresh_ahead_s: float = 60.0
    # Requests, not credits: the public tier is rate limited per minute. A local
    # ceiling, charged by the same ledger as OpenSky's credits.
    schiphol_request_budget_per_hour: int = 1200
    # Pages of 20 flights fetched per direction before the answer says it is truncated.
    schiphol_max_pages: int = 3

    # --- AviationWeather.gov (no authentication) ----------------------------
    avwx_base_url: str = "https://aviationweather.gov/api/data"

    # --- adsb.lol (ODbL 1.0; licence class B) --------------------------------
    # Opt-in, because serving it accepts ODbL's obligations: attribution, and a
    # derived database offered under ODbL. When on, it answers live traffic.
    adsblol_enabled: bool = False
    adsblol_base_url: str = "https://api.adsb.lol"
    # Optional. Sent as `x-api-key`; without one the quota is per IP.
    adsblol_api_key: SecretStr | None = None
    # A local ceiling under adsb.lol's own: 500 requests a day per IP without a key.
    adsblol_requests_per_hour: int = Field(default=20, ge=0)

    # --- Paid schedule providers (licence class C) ---------------------------
    # No adapter reads these. They exist so the edition gate can refuse them by
    # name: a key in the environment is a statement that the source is in use.
    aviationstack_access_key: SecretStr | None = None
    aerodatabox_api_key: SecretStr | None = None

    # --- Bring-your-own providers (licence class D; providers.py) ------------
    # Comma-separated `module:attribute` paths to `ProviderAdapter` objects.
    byo_providers: str = ""
    # The labelled fixture adapter. It serves invented flights, never real ones.
    byo_example_provider: bool = False

    # --- Inbound authentication (this server as an OAuth 2.1 resource server) -
    # Both must be set to serve over HTTP; stdio needs neither.
    auth_issuer: str | None = None
    auth_jwks_url: str | None = None
    # The canonical URL clients reach this server at. It is published in the
    # protected-resource metadata and a token must name it in `aud`, so it must
    # be the public URL, not a bind address.
    resource_server_url: str = "http://127.0.0.1:8000/mcp"
    jwks_cache_ttl_s: float = 3600.0
    # Floor between JWKS refetches triggered by an unknown key id.
    jwks_min_refresh_interval_s: float = 30.0
    auth_clock_skew_s: float = 30.0

    # --- On-behalf-of (this server as a confidential client of its issuer) ---
    # The server's own credentials at the issuer's token endpoint, used to
    # exchange a user's token for a downstream one. Unset disables OBO tools only.
    auth_token_url: str | None = None
    auth_client_id: str | None = None
    auth_client_secret: str | None = None

    # --- Internal operations API (passenger data; reached on behalf of the user)
    ops_api_base_url: str | None = None
    ops_api_audience: str = "api://flightops-ops"

    # --- Human approval of writes (docs/adr/0003) -----------------------------
    # The audience an approval grant must name. Deliberately not this server's
    # own: a grant must not work as an access token, nor the reverse.
    approval_audience: str = "api://flightops-approvals"
    # A grant older than this is refused whatever its `exp` says.
    approval_max_age_s: float = 300.0
    # How long a proposal waits for its approval before it must be proposed again.
    approval_window_s: float = 600.0
    # Seals the state a proposal carries between its two requests. At least 32
    # bytes of secret randomness, shared by every replica. Unset means a key per
    # process: correct for one process, and a retry that lands on another replica
    # is then refused rather than trusted.
    request_state_secret: str | None = None

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

    # --- Dashboard (dashboard/) -----------------------------------------------
    # Unset means no gate, so the board binds to localhost only.
    dashboard_passcode: SecretStr | None = None
    # A rolling 24 h ceiling on what the board and `airspace_snapshot` may spend,
    # on top of the hourly guard above.
    dashboard_daily_credit_budget: int = 1000
    # The traffic layer's own hourly ceiling. At 4 credits a refresh, 60 is one refresh
    # every 4 minutes; auto-refresh stops, with the reason on screen, when it is reached.
    dashboard_traffic_credits_per_hour: int = Field(default=60, ge=0)
    # Where recordings go. Unset means ~/.flightops/recordings; never inside the repo.
    dashboard_recordings_dir: Path | None = None

    # --- Tool-result cache (src/flightops_mcp/cache.py) -----------------------
    # The TTL and scope of each tool are policy, in code. These two only say
    # whether the cache runs at all and how many results it may hold.
    cache_enabled: bool = True
    cache_max_entries: int = 1024

    # --- Resilience ----------------------------------------------------------
    # Consecutive upstream failures before the breaker opens. The retry policy
    # counts attempts, so three attempts per call means a dead provider opens the
    # circuit within two calls rather than after a minute of sleeping.
    breaker_failure_threshold: int = 5
    breaker_recovery_timeout_s: float = 30.0

    # --- Observability -------------------------------------------------------
    otel_exporter: str = "none"  # none | console | otlp | memory (tests)
    otel_endpoint: str = "http://127.0.0.1:4318/v1/traces"
    otel_service_name: str = "flightops-mcp"
    otel_environment: str = "local"
    otel_sample_ratio: float = 1.0

    log_level: str = "INFO"
