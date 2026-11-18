# Operator runbook

What to do when `flightops-mcp` misbehaves. Written for the person on call who did not build it.

Ports in the compose stack: server `8000` (MCP at `/mcp`, plus `/healthz`, `/readyz`, `/metrics`),
IdP `9000`, ops API `9100` (not published to the host), Jaeger UI `16686`, Prometheus `9090`,
Grafana `3000`.

---

## 1. Health and readiness — they answer different questions

| Endpoint | Question | Behaviour | Correct reaction |
|---|---|---|---|
| `GET /healthz` | Is this process alive and serving? | Always `200` when the app answers. Never touches an upstream. | `503`/timeout → restart the container. |
| `GET /readyz` | Can this instance answer requests at all? | `200` when the airport reference database opens and holds rows; `503` otherwise. Reports breaker states **informationally**. | `503` → do **not** send traffic here; check the volume/database, not the upstreams. |

**Why readiness does not gate on upstream health.** A NOAA outage or an open circuit degrades the
traffic tools, but `resolve_airport` still answers. Gating readiness on upstreams would remove *every*
replica from the load balancer because a third party is down — turning someone else's incident into
your outage. The breaker states are printed in `/readyz` so an operator can see them without opening a
dashboard.

## 2. Metric catalogue

All on `/metrics`, all registered in `telemetry.py`.

| Metric | Type | Labels | Read it as |
|---|---|---|---|
| `flightops_tool_calls_total` | counter | `tool`, `outcome` | `outcome="ok"` is success; every other value is a `ToolFailure` code. Rate, not total. |
| `flightops_upstream_requests_total` | counter | `upstream`, `status` | `status="transport_error"` is a network fault; a 5xx body counts as `status="503"` etc. |
| `flightops_upstream_latency_seconds` | histogram | `upstream` | p95 per upstream; compare across days rather than against a fixed threshold. |
| `flightops_opensky_credits_spent_total` | counter | — | Credits burned by this process. A standard OpenSky account gets 4,000/day per endpoint family. |
| `flightops_opensky_credits_remaining` | gauge | — | Last value the upstream reported in `X-Rate-Limit-Remaining`. |
| `flightops_token_refreshes_total` | counter | `outcome` | `ok` should tick roughly every 30 minutes, not every minute. |
| `flightops_circuit_open` | gauge | `upstream` | `1` means calls to that upstream are being refused without an attempt. |
| `flightops_upstream_credits_spent_total` | counter | `upstream` | Metered units charged by the local ledger: OpenSky credits, Schiphol requests. |
| `flightops_cache_requests_total` | counter | `tool`, `outcome` | `hit`, `miss`, `coalesced`, `bypass`. A falling hit rate under steady traffic means the questions changed, or the TTLs did. |
| `flightops_cache_entries` | gauge | — | Tool results held. Bounded by `FLIGHTOPS_CACHE_MAX_ENTRIES` (1024). |

### Suggested alerts

| Alert | Expression | Why it matters |
|---|---|---|
| Tool error ratio | `sum(rate(flightops_tool_calls_total{outcome!="ok"}[10m])) / sum(rate(flightops_tool_calls_total[10m])) > 0.25` | A quarter of calls failing is a product problem, not a blip. |
| Circuit open | `max by (upstream) (flightops_circuit_open) == 1` | The breaker is protecting you; find out why it opened. |
| Credit burn | `increase(flightops_opensky_credits_spent_total[1h]) > 1000` | The local budget is 400/hour by default; crossing it means the guard is misconfigured or a loop is running. |
| Readiness failing | `probe_success{job="flightops-mcp"} == 0` on `/readyz` | An instance cannot serve; check reference data first. |
| Latency regression | p95 of `flightops_upstream_latency_seconds` above twice its 7-day median | Slow tickets, not errors — often a provider-side change. |

## 3. Playbooks

### `unknowns` is non-empty and severity is `unknown`

Not a bug. It means the tool could not see enough to judge — missing credentials, no METAR, or an empty
movements window. The payload names which. Tell the user what is missing; do not "fix" it by filling in
a plausible answer.

### `upstream_circuit_open`

The breaker opened after `FLIGHTOPS_BREAKER_FAILURE_THRESHOLD` consecutive faults and is refusing
calls for `FLIGHTOPS_BREAKER_RECOVERY_TIMEOUT_S` (default 30 s).

1. Check `flightops_upstream_requests_total{upstream="..."}` for the status pattern that opened it.
2. Check the provider's own status page before touching anything.
3. Do not restart the service to "reset" the breaker: it reopens on the next failure, and you lose the
   evidence. Wait out the window; the first call after it acts as the probe.

### `upstream_rate_limited`

Two different situations, distinguishable by `retry_after_s`:

- **Small (≤ `FLIGHTOPS_MAX_RATE_LIMIT_WAIT_S`, default 5 s)** — the client already waited and retried.
  If the user still sees this, the upstream is throttling hard: narrow the query (smaller radius,
  shorter window).
- **Large** — returned deliberately instead of sleeping inside the tool call. The number is data; the
  agent should report it, and a human decides whether to retry later.

### `local_budget_exhausted`

The in-process credit guard (`FLIGHTOPS_CREDIT_BUDGET_PER_HOUR`, default 400) stopped the call. This is
a feature: one runaway agent loop must not drain the day's allowance. Look for a loop before raising
the limit. Check the cache hit rate too: with `FLIGHTOPS_CACHE_ENABLED=false` the load test spends
the whole 400 in under a minute (docs/LOAD-TEST.md). The error's `details` name the upstream's numbers;
Schiphol has its own budget, `FLIGHTOPS_SCHIPHOL_REQUEST_BUDGET_PER_HOUR` (1200 requests).

### `credentials_missing`

`FLIGHTOPS_OPENSKY_CLIENT_ID` / `_SECRET` are unset. Weather and reference tools keep working; traffic
and movement tools cannot. Create the client in the OpenSky account page (the id ends in `-api-client`
and is only valid for the REST API), then restart the server. For `schiphol_flights` the variables are
`FLIGHTOPS_SCHIPHOL_CLIENT_ID` / `_SECRET` (an application on developer.schiphol.nl); `details.upstream`
says which. A Schiphol `upstream_auth_failed` with status 403 from the token endpoint is usually the
wrong `FLIGHTOPS_SCHIPHOL_AUDIENCE` or an API the application is not subscribed to.

### `reference_data_missing` / `/readyz` failing

`data/airports.sqlite3` is absent or empty. Rebuild it:

```bash
uv run python scripts/build_airports_db.py --csv data/airports_seed.csv   # bundled, 25 airports
uv run python scripts/build_airports_db.py                                # full set, ~49k airports
```

An empty database is treated as not-ready on purpose: every coordinate-dependent tool would fail.

## 4. Reading a trace

A well-formed request is one trace with this shape (span names are asserted in
`tests/test_otel.py`):

```
tools/call disruption_triage            SERVER  parent = the client's traceparent (from _meta)
└── tool.disruption_triage              INTERNAL
    ├── tool.airport_weather            INTERNAL
    │   ├── HTTP GET  (upstream.name=aviationweather)  CLIENT  http.response.status_code=200
    │   └── HTTP GET  (upstream.name=aviationweather)  CLIENT
    ├── tool.airport_movements
    │   └── HTTP GET  (upstream.name=opensky) ×2
    └── tool.airport_live_traffic
        └── HTTP GET  (upstream.name=opensky)
```

Useful attributes: `mcp.method.name`, `mcp.protocol.version`, `jsonrpc.request.id`, `gen_ai.tool.name`,
`mcp.tool.outcome`, `mcp.tool.ok`, `upstream.name`, `http.response.status_code`, `http.attempt`.

If a client supplies `traceparent` in the request `_meta`, the server span joins that trace; if it does
not, the server starts a new one. `server/discover` and `tools/list` carry no context and therefore
appear as separate traces — that is correct, not a broken link.

**Note:** `MCPServer` installs the SDK's own `OpenTelemetryMiddleware` by default. Do not register a
second request-span middleware: doing so yields two `tools/call` spans per request (this is how the
default was discovered).

## 5. Turning tracing on

`FLIGHTOPS_OTEL_EXPORTER` is `none` by default (no provider, negligible overhead).

| Value | Use |
|---|---|
| `none` | production default until you have a collector |
| `console` | a single developer debugging spans locally |
| `otlp` | a collector at `FLIGHTOPS_OTEL_ENDPOINT` (compose points it at Jaeger) |
| `memory` | tests; spans are asserted directly |

`FLIGHTOPS_OTEL_SAMPLE_RATIO` (default `1.0`) is applied with `ParentBased`, so a sampled upstream
decision is respected downstream.

## 6. Secret rotation

| Secret | Where it lives | Rotate by |
|---|---|---|
| `FLIGHTOPS_OPENSKY_CLIENT_SECRET` | `.env` / secret store, never the repo | New API client in the OpenSky account page, update the value, restart. The old token stays valid until it expires (≤30 min). |
| `DEV_MCP_CLIENT_SECRET` | `.env` (local IdP only) | Regenerate, update the IdP's `IDP_CLIENTS` and the server's `FLIGHTOPS_AUTH_CLIENT_SECRET`, restart both. Exchanges fail with `invalid_client` until they match. |
| `DEV_AGENT_CLIENT_SECRET` | `.env` (local IdP only) | Same, for the test client that mints user tokens. |
| `FLIGHTOPS_SCHIPHOL_CLIENT_SECRET` | `.env` / secret store, never the repo | New secret on the Schiphol portal (shown once), update, restart. The cached token is refreshed 60 s before its 30 minutes run out. |

No secret is ever logged: the redaction pipeline strips `authorization`, `client_secret`, `access_token`,
PII field names, bearer strings and JWTs, and `tests/test_redaction.py` proves it with canary values.

## 7. Known limitations of this stack

- **The observability images are not built by this repo and are not exercised by CI.** The
  `compose-matrix` CI job proves the auth behaviour with the app services; Jaeger, Prometheus and
  Grafana are pinned by tag in `docker-compose.yml` and should be bumped deliberately.
- **The IdP is a test fixture, not an identity provider.** `/dev/user-token` mints a token for any user
  without a sign-in. Never expose it beyond localhost.
- **Logs go to stdout** as JSON with no retention of their own; ship them to your platform's log store.
- **One write path, with its idempotency in the ops API's memory.** `propose_rebooking` executes only
  with a signed approval (ADR 0003), once per idempotency key, for the lifetime of the ops API process.
- **The cache, the credit ledgers, the breakers and single flight are per process.** Two replicas have
  two caches and two budgets; size `FLIGHTOPS_CREDIT_BUDGET_PER_HOUR` per replica accordingly.
- **The Schiphol adapter has never seen the live API** (ADR 0005). Expect the first real run to find
  something; read its `unknowns` and the mock's assumptions before trusting its counts.
