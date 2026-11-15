# Changelog

All notable changes to this project. Format based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- OpenTelemetry traces across the tool boundary, with the client's `traceparent` read from the request
  `_meta` as the 2026-07-28 revision specifies, and re-injected on every upstream request.
- `/healthz`, `/readyz` and `/metrics` on the HTTP transport, registered with `custom_route`.
- A circuit breaker per upstream, so a dead provider fails fast instead of spending the retry budget.

## [0.2.0] - 2026-11-15

Phase 1 of [ROADMAP.md](ROADMAP.md): the server becomes an OAuth 2.1 resource server.

### Added
- Streamable HTTP transport with RS256 JWT verification against JWKS.
- A local IdP (`services/idp`) implementing `client_credentials`, RFC 8693 token exchange, and
  revoke/expire, so the whole auth matrix is testable without a cloud tenant.
- Per-tool scopes in code (`auth/scopes.py`) with a `WWW-Authenticate` `insufficient_scope` challenge.
- Wrong-audience rejection and an explicit no-token-passthrough check (`auth/passthrough.py`).
- On-behalf-of token exchange wired to a PII-bearing mock ops API (`services/ops_api`).
- `docs/LOCAL-MATRIX.md`: the 12-line acceptance matrix, runnable without Docker.
- `docker-compose.ci.yml` and a `compose-matrix` CI job that runs that matrix against the stack.

### Fixed
- `scripts/local_matrix.sh` passed an MSYS `/tmp/...` path to native Python, which resolved it as
  `\tmp\...`; the documented quickstart failed with `FileNotFoundError` on git-bash. The log directory
  is now translated with `cygpath -w` for the arguments given to Python.

## [0.1.0] - 2026-11-14

First working slice.

### Added
- Six typed MCP tools (`resolve_airport`, `airport_weather`, `airport_live_traffic`,
  `airport_movements`, `disruption_triage`, `upstream_status`) on protocol revision `2026-07-28`.
- OAuth2 client-credentials token manager with expiry skew and single-flight refresh.
- Credit-aware rate limiting: `429` honoured under a configured ceiling, surfaced as data beyond it,
  plus a local hourly credit ledger charged before the request.
- Redaction pipeline for secrets and passenger PII, proven with canary strings.
- 73 tests, no network, no sleeps, no real clock; ruff and mypy `--strict` clean.
- Multi-stage non-root container with the airport reference database baked in at build time.
