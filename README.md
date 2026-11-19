# flightops-mcp

[![ci](https://github.com/KristofferHM/flightops-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/KristofferHM/flightops-mcp/actions/workflows/ci.yml)

A governed [Model Context Protocol](https://modelcontextprotocol.io) server that gives an AI agent scoped,
audited, rate-limited access to airport operations data, takes one action only with a person's
signed approval, and says "unknown" when its evidence is thin.

The tools are the easy part. This repository is about the layer around them: OAuth 2.1 resource-server
auth with on-behalf-of exchange, a credit ledger in front of every metered upstream, a result cache that
honours the protocol's `ttlMs`/`cacheScope` without leaking across users, logs that never hold a
credential or a passenger, and an evaluation harness that measures all of it.

MCP revision `2026-07-28` (stateless), Python SDK `mcp==2.3.0`, Python 3.12+.

---

## How often it is right

The golden scenarios in [`evals/scenarios/`](evals/scenarios) run end to end against the real server
(auth, token exchange, approval gate, tools, upstream clients) with the upstreams described per scenario.
Each run is scored on five axes, reported separately because they fail for different reasons. CI re-runs
the scenarios on every push and fails on a regression of more than 2 points on any line, or on any one
scenario that fails a check it passed in the baseline. The table between the markers is written by the
harness from `evals/baseline.json`; nobody edits it.

<!-- evals:begin -->
<!-- Written by the harness from evals/baseline.json. Do not edit: CI fails if this block and the baseline disagree. -->

**Model:** `offline` (a scripted policy, not a model); **Date:** 2026-11-19; **Scenarios:** 65

| Axis | Passed | Applies to | Score |
|---|---:|---:|---:|
| Tool selection | 65 | 65 | 100.00 % |
| Argument validity | 61 | 61 | 100.00 % |
| Refusal correctness | 22 | 22 | 100.00 % |
| No hallucination | 24 | 24 | 100.00 % |
| Outcome (answer class, writes) | 65 | 65 | 100.00 % |
| **Scenarios passing every check that applies** | 65 | 65 | 100.00 % |

Absent-evidence group (answering confidently when the evidence is not there): **24 scenarios** over 9 kinds: `empty_result` 5, `malformed_payload` 3, `partial_coverage` 3, `rate_limited` 2, `stale_data` 2, `unauthenticated_read` 3, `unknown_airport` 2, `unknown_flight` 1, `upstream_error` 3.

No scenario fails in this run.

Regenerate: `uv run python -m evals.run --offline --update-baseline`. What these numbers measure, and what they do not: [`docs/EVALS.md`](docs/EVALS.md).
<!-- evals:end -->

**Read it with one thing in mind.** The model is `offline`: a scripted policy that is told which plan to
follow. So 100 % means the *server's* contract held on every scenario: right tool, valid arguments,
refusals for the right reason, no confident answer from absent evidence. It does not mean an agent
planned well. The first baseline scored 47 of 54, and all seven failures were server defects (stale data
passed as current, unreadable rows dropped silently, OpenSky's movements response unreadable). They were
fixed in the server with no expectation edited ([`docs/EVALS.md`](docs/EVALS.md#what-failed-in-the-first-baseline-and-what-was-done)).
Twenty-one scenarios target the failure this project exists to prevent: answering confidently when the
evidence is not there.

## Under load

50 requests per second for 60 seconds, open loop, authenticated, over HTTP, against the mock upstream.
Measured on an **Intel Core i7-7600U laptop (2 cores, 4 threads, 16 GB, Windows 10)**, with the load
generator, server, IdP and mock all on that one machine and 25 ms of simulated upstream latency.
Latency is measured from each request's scheduled send time, so queueing is not hidden.

| | Shipped (cache on) | Cache off |
|---|---:|---:|
| Requests completed | 3000 / 3000 | 3000 / 3000 |
| Error rate | **0.00 %** | 27.87 % (refused by the credit ledger) |
| p50 / p95 / p99 latency | **10.1 / 41.1 / 67.7 ms** | 30.1 / 109.1 / 146.2 ms |
| OpenSky credits spent | **160** | 400, the hourly budget, then refused |
| Schiphol requests | 2 | 600 |

```bash
bash scripts/load_test.sh        # starts the stack, runs the check and the load, stops the stack
```

The second column is the credit ledger working: without the cache the same questions spend the
400-credit hourly budget in under a minute, and every further credit-spending call is refused at once,
as data. Method, per-tool numbers and what the run does not show: [`docs/LOAD-TEST.md`](docs/LOAD-TEST.md).

## Try it in two minutes

```bash
git clone https://github.com/KristofferHM/flightops-mcp.git && cd flightops-mcp && uv sync
mkdir -p agent-runs
uv run python -m agent run --scenario rebook-approved --backend offline --out agent-runs/approved.json
```

One JSON line ending in `"ops_api": {"rebooking_requests": 1, "rebookings_written": 1}`. An agent read
passenger counts as a user, proposed a rebooking, a different person approved that exact proposal, and
the operations API wrote it once. No API key, no Docker, no network. The full script, with the refusals:
[`docs/DEMO.md`](docs/DEMO.md).

---

## What is in the repository, and how each part is proven

| Capability | Where it lives | Proven by |
|---|---|---|
| OAuth 2.1 resource server: RS256, JWKS, audience, `403` scope challenge | `auth/verifier.py`, `auth/jwks.py`, `auth/challenge.py` | `test_auth_verifier.py`, `test_scopes.py`, Phase 1 matrix in `docker compose` (CI) |
| One scope per data domain, enforced twice, fail closed | `auth/scopes.py`, `scoped` in `server.py` | a tool with no scope cannot register; `test_every_tool_runs_with_exactly_its_own_scopes` |
| On-behalf-of (RFC 8693), narrowing only, single flight per identity | `auth/obo.py` | `test_obo.py`: fifty concurrent calls, one exchange per user |
| No token passthrough, inbound or outbound | `auth/passthrough.py` | a hook on every HTTP client, and a test that scans the source for one without it |
| Human approval of the only write, as a signed grant for one payload | `auth/approval.py`, `propose_rebooking` | ADR 0003; refused without approval, on self-approval, on a changed payload; one write per idempotency key |
| OAuth2 client credentials, shared by both authenticated upstreams | `opensky_token.py` | single flight; refresh before expiry; one retry on `401` |
| `429` as a contract: short waits honoured, long ones returned as data | `http.authorized_get` | the same policy object for OpenSky and Schiphol |
| A credit ledger in front of every metered upstream | `credits.py` | property tests, mutation-tested, and the load test above |
| **Result cache under `ttlMs`/`cacheScope`**, private results partitioned by identity | `cache.py` | ADR 0004; `test_cache.py`; five users × ten concurrent calls, one downstream read each |
| **Deterministic tool order** for prompt caching | `create_server` | the published list is byte-identical across builds and a reversed registration |
| **Second authenticated upstream (Schiphol, OAuth2 with `audience`)**, **exercised only against a mock of its documented API; no live Schiphol key has been used** | `schiphol.py`, `services/mock_upstream` | ADR 0005; `test_schiphol.py`; a scope refusal that spends nothing and a debit the mock agrees with, in CI through `docker compose` |
| Absent evidence is never "all clear" | every tool's `unknowns` | 21 absent-evidence eval scenarios |
| PII- and secret-safe logs | `telemetry.py` | canary tests over the emitted log lines |
| Traces, metrics, liveness vs readiness, circuit breaker | `otel.py`, `/metrics`, `/readyz`, `resilience.py` | `test_otel.py`, `test_health.py`, `test_resilience.py` |
| An agent loop with a deterministic offline backend | `agent/` | byte-identical transcripts, checked in CI |
| Quality gates | `pyproject.toml`, `ci.yml` | branch coverage ≥ 90 %, ≤ 100 surviving mutants on `auth/`, the ledger and the board's display rules, an accessibility gate, zero skipped tests |
| Supply chain | `ci.yml`, `Dockerfile`, `uv.lock` | gitleaks (tree and history), pip-audit, trivy image scan, actions pinned to commits ([`docs/SECURITY.md`](docs/SECURITY.md)) |

## Architecture

```
        MCP host (Claude Desktop, Inspector, the agent in agent/)
                     │  stdio, or Streamable HTTP with a bearer token
                     ▼
   ┌──────────────────────────────────────────────────────────────┐
   │ flightops-mcp                                                │
   │                                                              │
   │  bearer check ─► scope check ─► result cache ─► tool         │
   │  (RS256, JWKS)   (403 + challenge) (ttlMs, cacheScope)       │
   │                                                  │           │
   │        credit ledger · 429 policy · circuit breaker ·        │
   │        passthrough guard · OAuth2 token manager              │
   └───┬────────────┬──────────────┬────────────┬────────────┬────┘
       │ OAuth2 M2M │ OAuth2 M2M   │ no auth    │ RFC 8693   │ local
       │            │ + audience   │            │ OBO        │
       ▼            ▼              ▼            ▼            ▼
   OpenSky      Schiphol v4    AviationWeather  ops API     OurAirports
   (credits,    (mock-verified (NOAA METAR,     (mock,      (SQLite,
    429)         only)          TAF)             PII-shaped) bundled)
```

Five upstreams, four auth models. The tools do not care which; the governance layer is the same for all.

## The tools

| Tool | Scope | Upstream | Cost | Cache |
|---|---|---|---|---|
| `resolve_airport` | `reference.read` | OurAirports, bundled | free, offline | public, 1 h |
| `airport_weather` | `weather.read` | AviationWeather.gov | free | public, 60 s |
| `weather_overview` | `weather.read` | AviationWeather.gov, up to 100 airports per request | free | public, 60 s |
| `airport_live_traffic` | `traffic.read` | OpenSky `/states/all` | 1–4 credits | public, 10 s |
| `airport_movements` | `traffic.read` | OpenSky `/flights/*` | 4 credits per direction | public, 60 s |
| `airspace_snapshot` | `traffic.read` | OpenSky `/states/all`, fixed regions | 4 credits | public, 60 s |
| `disruption_triage` | `weather.read` + `traffic.read` | the three above, composed | ~9 credits, less from cache | never (its parts are) |
| `schiphol_flights` | `schedule.read` | Schiphol v4 (**mock-verified only**) | 1 request per page | public, 60 s |
| `flight_passenger_impact` | `passengers.read` | ops API, as the user | free | **private**, 30 s |
| `propose_rebooking` | `rebooking.write` | ops API, as the user, after approval | free | never |
| `upstream_status` | `status.read` | process state | free | never |

Every scope is prefixed `flightops.`. The tool list carries `ttlMs: 300000, cacheScope: "public"` and
is sorted by name.

Every tool that reads an upstream reports `upstream_time_epoch`: the `Date` header of the upstream's response, in
epoch seconds, or `null` when it sent none. Ages (`latest_metar.age_s`, `snapshot_current`) are measured against it
and never against this machine's clock; with `null` the age is unknown and `unknowns` says why. A composite reports
the newest of its parts' times. `flight_passenger_impact` and `propose_rebooking` keep `as_of`, the time this server
answered, because they report an action and a record rather than an observation with an age.

## Running it

```bash
uv sync
uv run python scripts/build_airports_db.py --csv data/airports_world.csv
uv run flightops-mcp                                    # stdio, for Claude Desktop or mcp dev
```

Credentials are environment variables, never files in the repository: `FLIGHTOPS_OPENSKY_CLIENT_ID` /
`_SECRET` for traffic and movements (a free OpenSky account), `FLIGHTOPS_SCHIPHOL_CLIENT_ID` / `_SECRET`
for the schedule. Without them those tools answer `credentials_missing` and the rest keep working.
Adding the OpenSky pair later is an edit to `.env` and a restart. `FLIGHTOPS_OPENSKY_ANONYMOUS=true` opts in
to OpenSky's anonymous tier until then: 10-second positions, most recent vectors only, and every traffic
payload says so. `upstream_status` and each traffic payload report the mode as `credentials`.

Remote, as an OAuth 2.1 resource server (there is no unauthenticated HTTP mode; without an issuer the
process exits):

```bash
export FLIGHTOPS_AUTH_ISSUER=https://idp.example.com
export FLIGHTOPS_AUTH_JWKS_URL=https://idp.example.com/.well-known/jwks.json
export FLIGHTOPS_RESOURCE_SERVER_URL=https://flightops.example.com/mcp
uv run flightops-mcp --transport streamable-http --host 0.0.0.0 --port 8000
```

The whole stack (server, local IdP, mock ops API) with no external account:

```bash
echo "DEV_AGENT_CLIENT_SECRET=$(openssl rand -hex 24)" >> .env
echo "DEV_MCP_CLIENT_SECRET=$(openssl rand -hex 24)" >> .env
docker compose up --build                       # add --profile observability for Jaeger, Prometheus, Grafana
```

| Request | Answer |
|---|---|
| No token, expired, bad signature, wrong issuer, **wrong audience** | `401` + `WWW-Authenticate: Bearer resource_metadata="..."` |
| Valid token without the tool's scope | `403` + `error="insufficient_scope", scope="..."`, and a body naming the scope |
| `GET /.well-known/oauth-protected-resource/mcp` | RFC 9728 metadata |
| `/healthz`, `/readyz`, `/metrics` | liveness, readiness (never fails on an upstream outage), Prometheus |

Operating it (alerts, playbooks per failure code, secret rotation): [`docs/runbook.md`](docs/runbook.md).

## Design decisions worth defending

1. **A `429` is a contract, not noise.** "Retry in 3 s" is waited out once; "retry in 900 s" is returned
   as a structured error carrying the number. Sleeping fifteen minutes inside a tool call is a hung agent.
2. **Credits are charged before the request**, because a call that times out still spends upstream budget.
3. **Absence of data is never "all clear".** A `204` from NOAA, an empty movements list and a missing
   credential are three different `unknowns`, and triage degrades to `unknown`, never to `low`.
4. **Caching is a per-tool decision in the protocol's words**, inside the scope check, private by identity,
   and a hit says it is a hit (ADR 0004).
5. **A second upstream is a parameter, not an interface.** Adding Schiphol needed two things made
   configurable and one policy moved out of OpenSky's client, and no new abstraction (ADR 0005).
6. **An approval is a signed grant for one payload, not an answer.** The agent carries the approval, so it
   must not be able to forge one (ADR 0003).
7. **Pin what can be rewritten, scan what cannot be pinned** (ADR 0006).

Decision records: [0001 local IdP modelled on Entra](docs/adr/0001-local-idp-modelled-on-entra.md) ·
[0002 revocation is enforced at the IdP](docs/adr/0002-token-revocation-is-enforced-at-the-idp.md) ·
[0003 an approval is a signed grant](docs/adr/0003-an-approval-is-a-signed-grant-not-an-answer.md) ·
[0004 caching inside the scope check](docs/adr/0004-tool-results-are-cached-inside-the-scope-check.md) ·
[0005 the second adapter](docs/adr/0005-a-second-adapter-shares-the-governance-layer-by-parameters.md) ·
[0006 pinning and scanning](docs/adr/0006-pin-what-can-be-rewritten-scan-what-cannot-be-pinned.md).

## The test suite

**1149 tests, 0 skipped** (CI fails on a skip), ruff and `mypy --strict` clean over `src`, `services`,
`agent` and `evals`. Branch coverage 90.42 % against a floor of 90. Mutation testing on `auth/`, the
credit ledger and the board's display rules with at most 100 surviving mutants, each one judged in
[`docs/MUTATION-SURVIVORS.md`](docs/MUTATION-SURVIVORS.md). No network, no sleeps and no real clock in the
unit suite: transports, sleep and clock are injected. Method and history: [`docs/QUALITY.md`](docs/QUALITY.md).

Running it found real defects that reading would not have: NOAA rejects `hours` on `/taf`; a monotonic
clock asked OpenSky for traffic in 1970; live METARs carry `"visib": "6+"`, a string; two server spans per
request from a doubled middleware; and the seven server defects the first eval baseline exposed.

## What this still does not do

- **No live Schiphol run.** The adapter is built to Schiphol's documented API and tested against a mock of
  it, in the suite and in CI. No key was available, and none is in the repository. Field names, the
  local-time window format and the public tier's rate limit are unverified against the live service.
- **No real model in the scored evals.** The table is the offline scripted policy. A model run is
  supported (`--backend anthropic`) but is not in CI and is not the published number.
- **No answer-quality measure beyond a lexicon.** Answers are classified by a small fixed vocabulary.
  That is exact for the offline policy's templates and only a heuristic for a model's prose.
- **No shared state across replicas.** The cache, the credit ledger, the circuit breakers and the
  single-flight locks are per process. Two replicas have two budgets and two caches.
- **No persistence.** The ops API's rebookings and the approval ledger live in memory and die with the
  process. Idempotency holds within one process lifetime.
- **No production identity provider.** `services/idp` is a fixture modelled on Entra
  ([ADR 0001](docs/adr/0001-local-idp-modelled-on-entra.md) lists the differences). Nothing has been run
  against a real Entra tenant.
- **No load numbers beyond one laptop.** 50 rps held on a 2-core machine with everything co-located; the
  rate at which it stops holding was not measured, and nothing ran for longer than a minute.
- **No cache-hostile load test.** Eight airports repeat inside every TTL, so the shipped run is a
  favourable case. The cache-off column is the other bound.
- **No private caching under load.** Partitioning by identity is proven by tests, not by the load run.
- **No digest-pinned base image**, on purpose (ADR 0006), and the trivy hash is pinned on first use,
  not from a second source ([`docs/SECURITY.md`](docs/SECURITY.md)). The image still carries 47 HIGH
  findings Debian has not fixed. The optional observability images are old and unscanned.
- **No terminal recording of the demo.** The demo is a script with expected output, and its transcript is
  the record.
- **One write, deliberately.** `propose_rebooking` moves a cohort, never a named passenger. There is no
  cancel, no hotel, no diversion; the eval set checks that the server refuses those.
- **ADS-B is not a schedule, and a schedule is not an observation.** OpenSky's origin and destination are
  estimates; Schiphol's states are what the airport published. Both caveats are in the tool descriptions
  the model reads.
- **No inbound rate limit per caller.** The credit ledger protects the upstreams' budgets, not this
  server: one caller with a valid token can send as many requests as the process will take.
- **A revoked access token works until it expires** for tools that need no exchange. Revocation is
  enforced where tokens are exchanged, not checked on every request
  ([ADR 0002](docs/adr/0002-token-revocation-is-enforced-at-the-idp.md)).
- **No tamper-evident audit store.** Who approved what is a structured log line on stdout, redacted and
  complete, and only as durable as wherever the logs are shipped.
- **One tenant.** There is no tenant isolation beyond what the issuer's tokens say.
- **No Kubernetes manifests, no frontend, no vector database.** Each would be a week of work that shows
  nothing about integration engineering.
- **No release tag.** `v1.0.0` is the owner's decision ([`ROADMAP.md`](ROADMAP.md)).

## Data sources and licences

| Source | Data | Licence / terms |
|---|---|---|
| [OpenSky Network](https://opensky-network.org) | state vectors, movements | Free for personal and non-profit use; commercial use needs OpenSky's consent |
| [AviationWeather.gov](https://aviationweather.gov/data/api/) (NOAA) | METAR, TAF | US Government open data, public domain |
| [OurAirports](https://ourairports.com/data/) | airports | Public domain |
| [Schiphol Public Flight API](https://developer.schiphol.nl) | scheduled flights at AMS | Schiphol developer terms; requires a registered application. Not exercised live by this project |

This project is the personal-use case: no commercial deployment, no redistribution of upstream data.
The plan and what each phase proved: [`ROADMAP.md`](ROADMAP.md). What changed when: [`CHANGELOG.md`](CHANGELOG.md).
