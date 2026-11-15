# flightops-mcp

[![ci](https://github.com/KristofferHM/flightops-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/KristofferHM/flightops-mcp/actions/workflows/ci.yml)

A governed [Model Context Protocol](https://modelcontextprotocol.io) server that lets an AI agent ask
questions about the operational state of an airport — and refuses to answer badly.

Most MCP examples on GitHub are a stdio server with two read-only tools. This one is built around the
parts that actually break in production: OAuth2 client-credential token lifecycles, a credit-metered
upstream with a real `429` contract, honest reporting of missing data, and logs that never contain a
credential or a passenger identifier.

- **Protocol:** MCP revision `2026-07-28` (stateless: no `initialize` handshake, no session id,
  per-request capability negotiation), via the Python SDK's `MCPServer`.
- **Verified:** `73 passed, 0 skipped` · `ruff` clean · `mypy --strict` clean · a live end-to-end
  stdio run against AviationWeather.gov returning real METARs.
- **Cost:** zero. Every upstream is free; the authenticated one is optional.

---

## Requirements → what is actually in the repo

| Capability | Where it lives | How it is proven |
|---|---|---|
| MCP tools with typed schemas | `src/flightops_mcp/server.py` | `test_all_tools_are_published_with_schemas` asserts every tool has a usable description and an object schema |
| OAuth2 **client credentials** (M2M) | `opensky_token.py` | `test_fifty_concurrent_callers_make_exactly_one_token_request` |
| Token lifecycle (expiry, revocation) | `opensky_token.py`, `opensky.py` | refresh-ahead inside the skew; a `401` invalidates and retries once |
| Rate limits | `http.py`, `opensky.py` | `429` + `Retry-After` / `X-Rate-Limit-Retry-After-Seconds` honoured when short, surfaced as data when long |
| API budget / least privilege | `opensky.py` credit ledger | `test_local_credit_budget_stops_a_runaway_loop` |
| REST integration, no-auth path | `weather.py` | live NOAA call in CI (advisory job) |
| Safe logging, no PII | `telemetry.py` | canary tests: a planted token and passenger record must not appear in emitted logs |
| Ground-truth reference data | `airports.py` + `scripts/build_airports_db.py` | distance validated two independent ways |
| Failures the model can act on | `errors.py` | every failure carries `code`, `message`, `hint`, `retry_after_s` |
| Containerisation | `Dockerfile`, `docker-compose.yml` | CI builds the image, asserts non-root, asserts the reference DB is baked in |
| Version control hygiene | `.gitignore`, `.gitattributes`, `uv.lock` | no secrets, no multi-megabyte data blobs, LF-normalised |

---

## Architecture

```
        MCP host (Claude Desktop, Inspector, an agent loop)
                              │  JSON-RPC over stdio or Streamable HTTP
                              ▼
                    ┌───────────────────┐
                    │  flightops-mcp    │  6 typed tools, guarded
                    │  (MCPServer)      │  failures become data, never 500s
                    └─────┬───────┬─────┘
             OAuth2 M2M   │       │   no auth
        client_credentials│       │
                   ┌─────▼─────┐ ┌─▼──────────────────┐ ┌──────────────────────┐
                   │ OpenSky   │ │ AviationWeather.gov│ │ OurAirports SQLite   │
                   │ REST API  │ │ (NOAA)             │ │ (bundled, offline)   │
                   │ credits   │ │ METAR / TAF        │ │ 49,353 airports      │
                   │ 429 wall  │ │ 204 = no data      │ │ public domain        │
                   └───────────┘ └────────────────────┘ └──────────────────────┘
```

Three upstreams with three different auth models is the point: the tool layer must not care, and the
governance layer must hold for all of them.

## Quickstart

```bash
uv sync

# The image and CI bake this from the bundled seed; locally it is one command.
uv run python scripts/build_airports_db.py --csv data/airports_seed.csv

# No credentials needed for this much:
uv run flightops-mcp           # stdio, for Claude Desktop / mcp dev
```

To unlock the traffic and movement tools, create a free account at
<https://opensky-network.org> (Account → API Client) and put the pair in `.env`:

```bash
FLIGHTOPS_OPENSKY_CLIENT_ID=your-id-api-client
FLIGHTOPS_OPENSKY_CLIENT_SECRET=your-secret
```

Then in `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "flightops": {
      "command": "uv",
      "args": ["run", "--project", "/absolute/path/to/flightops-mcp", "flightops-mcp"]
    }
  }
}
```

## The tools

| Tool | Upstream | Credentials | Cost |
|---|---|---|---|
| `resolve_airport` | OurAirports (local) | none | free, offline |
| `airport_weather` | AviationWeather.gov | none | free |
| `airport_live_traffic` | OpenSky `/states/all` | OAuth2 | 1–4 credits |
| `airport_movements` | OpenSky `/flights/*` | OAuth2 | 4 credits per direction |
| `disruption_triage` | all three, composed | OAuth2 for the traffic half | ~9 credits |
| `upstream_status` | process state | none | free |

## Design decisions worth defending

1. **A `429` is a contract, not noise.** If the upstream says "retry in 3 s" and our ceiling is 5 s,
   the client waits and retries exactly once. If it says "retry in 900 s", the call returns a
   structured error carrying that number. Sleeping fifteen minutes inside a tool call is not
   resilience; it is a hung agent.
2. **Tokens are single-flight.** Fifty concurrent tool calls that find an empty cache produce one
   token request, not fifty. The lock is double-checked so the winner refreshes and the rest reuse.
3. **Credits are charged before the request.** A call that times out still spends upstream budget, so
   the local ledger must record it too or the guard under-reports real spend. `/states/all` costs a
   documented function of bounding-box area — `BBox.credit_cost` mirrors that table and the test pins
   every band boundary.
4. **Absence of data is never "all clear".** A `204` from NOAA, an empty movements list, and a missing
   credential produce three different explicit `unknowns` entries. An empty result from an ADS-B feed
   means "nothing heard", which is not "nothing happening".
5. **Redaction is tested, not asserted.** A canary string is planted as a token, a PNR and a passenger
   name; the emitted log lines are parsed and searched for it. GDPR claims that are not executed are
   marketing.
6. **Triage degrades to `unknown`, not to `low`.** Without credentials the composite tool still
   returns weather, and reports severity `unknown` with the reason — it does not imply the airport is
   fine because it could not see the traffic.
7. **Provenance travels with the data.** Every payload names the upstream and its licence, so an
   operator can see that "traffic" means ADS-B estimates and "weather" means a government observation.

## What running it actually found

Two defects that reviewing the code would not have caught, both now pinned by tests:

- **NOAA rejects `hours` on `/taf`.** `GET /api/data/taf?ids=ESGG&format=json&hours=12` returns
  `HTTP 400 {"status":"error","error":"Unexpected query parameter provided"}`, while `/metar` accepts
  it happily. The first live run failed here; `test_taf_request_omits_the_hours_parameter` is the
  regression.
- **A monotonic clock is the wrong clock for epoch maths.** The runtime originally defaulted to
  `time.monotonic()`, which is right for token TTLs and wrong for movement windows — it would have
  asked OpenSky for traffic in January 1970. Wall clock is now the default, with the reasoning in a
  comment.

Live data also carries `"visib": "6+"` — a string, not a number — so a strict `isinstance` check would
have silently dropped real observations and lost a flag. `_number()` coerces it, and a test covers it.

## The test suite

```
tests/test_token.py          7   caching, single flight, skew, rejection, malformed response
tests/test_http.py          12   Retry-After (seconds + HTTP-date), retry counts, 429 passthrough
tests/test_opensky.py       21   credit table, decoding, 429 policy, 401 recovery, budget guard
tests/test_airports.py       9   build, resolve, nearest, cross-checked distances
tests/test_weather.py        8   no-data semantics, field mapping, flag thresholds
tests/test_server_tools.py  11   end-to-end through a real MCP client, in memory
tests/test_redaction.py      5   keyed secrets, nested structures, bearer scrubbing, canary
                            --
                            73
```

No network, no sleeps, no real clock: transports, sleep and clock are injected. The suite runs on a
bare interpreter in under 10 seconds, and CI fails if any test reports as skipped.

## What this deliberately does not do

- **No write operations.** Nothing here rebooks a passenger. The scope is observation and triage;
  a write path needs an idempotency story and a human-approval gate that this repo does not claim.
- **No LLM in the loop.** The server is the hard part and it stays testable without a model. Adding an
  agent loop on top is the next increment, not a hidden requirement.
- **No Kubernetes, no frontend, no vector database.** Each would be a week of work that demonstrates
  nothing about integration engineering.
- **ADS-B is not a schedule.** OpenSky deduces arrivals and departures from transponder positions, so
  origin and destination fields are estimates. That caveat is in the tool description, not only here.

## Data sources and licences

| Source | Data | Licence / terms |
|---|---|---|
| [OpenSky Network](https://opensky-network.org) | state vectors, movements | Free for personal and non-profit use; **commercial use requires OpenSky's consent** |
| [AviationWeather.gov](https://aviationweather.gov/data/api/) (NOAA) | METAR, TAF | US Government open data, public domain |
| [OurAirports](https://ourairports.com/data/) | airports, runways, navaids | Public domain |

This project is the personal-use case: no commercial deployment, no redistribution of upstream data.

## Roadmap

`v0.1.0` ships the governed tool layer and its test suite. [`ROADMAP.md`](ROADMAP.md) is the phased plan
to `v1.0`: auth-hardened remote server with OBO, observability and a runbook, an agent loop with a human
approval gate, an evaluation harness, then hardening and documentation.

## Roadmap → next milestones

### Near-term milestones

- [ ] Streamable HTTP server with MCP-spec OAuth 2.1 resource-server behaviour (JWKS validation, scope
      challenges in `WWW-Authenticate`, no token passthrough)
- [ ] On-behalf-of token exchange (RFC 8693) so a user's identity, not a service's, reaches the upstream
- [ ] OpenTelemetry traces with `traceparent` propagated into `_meta` as the 2026-07-28 revision specifies
- [ ] An agent loop with a human-approval gate for the first write tool
- [ ] Tool-calling eval harness: golden scenarios scored for tool choice, argument validity and refusal
