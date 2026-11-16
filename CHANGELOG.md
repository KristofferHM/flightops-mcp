# Changelog

All notable changes to this project. Format based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.3.0] - 2026-11-16

Phase 2 of [ROADMAP.md](ROADMAP.md): the server becomes something an operator can run.

### Added
- OpenTelemetry tracing with the client's `traceparent` read from the request `_meta` (the stateless
  2026-07-28 way) and re-injected on every upstream request. Exporters: `none`, `console`, `otlp`,
  `memory`. One triage call is one trace with the nested lookups visible.
- A span per tool body (`tool.<name>`) carrying `mcp.tool.outcome` and `mcp.tool.ok`, so a failure the
  model treats as data is still visible as an error to an operator.
- `/healthz`, `/readyz` and `/metrics` on the HTTP transport. Readiness checks local capability only and
  reports breaker state informationally: a provider outage must not empty the load balancer.
- A circuit breaker per upstream, with a half-open probe and `retry_after_s` on the refusal. `429` and
  4xx are deliberately neutral for it: throttling is a contract, not evidence of a broken provider.
- `docs/runbook.md`: alert suggestions, a playbook per failure code, secret rotation, trace reading.
- `observability/`: Prometheus scrape config and a provisioned Grafana dashboard, wired to a compose
  `observability` profile with Jaeger, Prometheus and Grafana.

### Changed
- `upstream_status` now reports breaker snapshots, tracing kind and whether the reference database is
  actually ready, not merely present.
- `docker-compose.yml` gives the server a healthcheck on `/healthz`; the CI override pins
  `FLIGHTOPS_OTEL_EXPORTER=none` because the matrix runs without a collector.

### Fixed
- `/metrics` exported the default Prometheus registry, so only Python runtime collectors appeared. It now
  scrapes the project's own registry.
- Registering an OTel request-span middleware duplicated every server span: `MCPServer` already installs
  one by default. Removed, with a regression test on the span count.

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
