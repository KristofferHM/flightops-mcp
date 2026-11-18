# Changelog

All notable changes to this project. Format based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- **Airport board** (`dashboard/`, docs/DASHBOARD.md): one port, polling only, vendored assets, a 2D map and an
  orthographic globe on canvas, a passcode gate, and recording with replay that makes no upstream calls.
- **`airspace_snapshot`**: fixed regions, 4 credits, cached 60 s, charged to a rolling 24 h ledger as well.
- **OpenSky credential modes** `client_credentials`, opt-in `anonymous` and `none`, reported in `upstream_status`,
  at startup and in every traffic payload. Credentials are a `.env` edit and a restart.

### Fixed
- `disruption_triage` no longer answers `low` with no evidence. It needs a current METAR, an aircraft in a current
  snapshot or a recorded flight, and names the legs that saw nothing.
- A call refused for missing credentials no longer spends local credits.

## [1.0.0] - 2026-11-18

Phase 5: product hardening and docs. Everything below was measured on a 2-core laptop, and what each
run does **not** show is stated in `docs/`.


### Added
- **Result cache under the protocol's `ttlMs`/`cacheScope`** (`cache.py`, ADR 0004). One hint per tool, required
  at registration like a scope. Zero TTL is never stored; private results are partitioned by subject, client and
  granted scopes; only successes are kept; the cache sits inside the scope check; hits are labelled and spend
  nothing; concurrent identical calls are coalesced. `flightops_cache_requests_total` and
  `flightops_cache_entries` on `/metrics`.
- `tools/list` and `server/discover` carry `ttlMs: 300000, cacheScope: "public"`, and **tools are published
  sorted by name**, byte-identical across builds, so a host's prompt cache can hit.
- **Second upstream adapter: Schiphol Public Flight API v4** (`schiphol.py`, `schiphol_flights`, scope
  `flightops.schedule.read`), OAuth2 client credentials with an `audience`, refreshed 60 s before expiry, per
  the vendor's 2026-07-20 migration guide. **Exercised only against a mock of that documented API; no live key
  was used** (ADR 0005).
- `services/mock_upstream`: Schiphol, OpenSky and AviationWeather in their documented shapes, strict where the
  vendors are strict, counting what a client gets wrong. In the compose matrix in CI.
- `scripts/second_upstream.py`: the adapter through a live stack (scope refusal that spends nothing, a debit the
  mock agrees with, no wrong audience, no expired token). Runs in the `compose-matrix` job.
- **Load test** (`scripts/load_test.sh`, docs/LOAD-TEST.md): 50 rps for 60 s, open loop, on a 2-core laptop.
  3000/3000 ok, p50 10.1 ms, p95 41.1 ms, p99 67.7 ms, 160 OpenSky credits. With the cache off the credit ledger
  stops OpenSky at exactly its 400-credit budget and refuses 27.87 % of calls. Both result files committed.
- Four golden scenarios for the new tool (58 in all), baseline and README table regenerated.
- **Image scan** in CI: trivy v0.69.3, the binary pinned by sha256 (not `trivy-action`; CVE-2026-33634), gating on
  fixable HIGH and CRITICAL, with a planted finding to show the gate can fail.
- docs: README rewritten around the eval table; ADRs 0004 (caching), 0005 (second adapter), 0006 (pinning and
  scanning); docs/DEMO.md (a clone to an approved rebooking, every step with its expected output);
  docs/SECURITY.md (pinning review, history audit).

### Changed
- The OAuth2 client-credentials token manager is shared: `ClientCredentialsTokenManager` takes the token URL,
  credentials, `audience` and refresh margin; `OpenSkyTokenManager` is its OpenSky configuration.
- The 401/429 policy moved from `OpenSkyClient` into `http.authorized_get`, used by both adapters.
- `missing_credentials` names the upstream and its variables. `flightops_upstream_credits_spent_total{upstream}`
  counts every metered upstream.
- Every GitHub Action is pinned to a commit SHA. The shipped image runs `apt-get upgrade` at build.

### Security
- History audit over all 45 commits on every ref: **no findings** (docs/SECURITY.md). `pip-audit`: no known
  vulnerabilities.
- The first image scan failed on a real fixable HIGH, zlib CVE-2026-85091 (DSA-6613-1), because the upgrade step
  had not reached that commit. 47 other HIGH findings have no Debian fix and are reported, not gated.

## [0.6.0] - 2026-11-18

Phase 4b of [ROADMAP.md](ROADMAP.md): quality gates. The suite was already large; this release is the
evidence that it would catch a bug, and the fixes that evidence produced.

### Added
- **Coverage ratchet**: branch coverage enforced at **89 %** (`fail_under`), raised only with new tests, with a
  short documented exclusion list rather than a growing one.
- **Property tests** (`hypothesis`) over the scope parsers and the subset rule, the credit-ledger arithmetic,
  `Retry-After`/backoff parsing, payload decoding with unreadable rows, and the airport CSV loader. A CI profile
  runs more examples than a developer machine.
- **Mutation testing** on `auth/`, the credit ledger and `agent/approval.py`: 1324 mutants, 1234 killed, a
  **ceiling of 90 survivors** enforced by the build. Every survivor is judged in
  [`docs/MUTATION-SURVIVORS.md`](docs/MUTATION-SURVIVORS.md) - 58 are message wording that a test already covers
  by asserting a refusal has both code and hint, 32 are behaviour-identical and each is explained. None is a
  missing test.
- **A self-testing gate**: CI reruns the mutation gate with `ceiling - 1` and fails the job unless the gate
  fails, so a gate that silently stops working is caught on every push.
- **Secret and dependency scanning**: `gitleaks` over the tree and over every commit on every ref, and
  `pip-audit` over the exported lock with hashes required.
- `docs/QUALITY.md`: the runs, the gaps that were real, and the three tests that could not fail.

### Fixed
The first mutation run (932 of 1324 killed) found thirteen gaps that review had not, and each is now closed:

- **`issuer=None` survived in both token builders**, which switches PyJWT's issuer check off entirely: no test
  had ever built a verifier from `Settings` and shown it a token from another issuer.
- **`event_hooks=None` survived in the JWKS client** - the outbound passthrough guard was only asserted by
  scanning source text, never by a request.
- **`continue` -> `break` survived in JWKS parsing**, where one malformed key entry would have dropped every key
  behind it.
- **The OBO cache key survived losing its issuer**; a held lock could be pruned (`and` -> `or`) allowing a second
  concurrent exchange for one identity; and all users could share one lock, so one slow exchange blocked every
  other user. The last two are pinned with a gated transport.
- The approval audit log was asserted for 2 of its 7 reasons; `verify_nbf`/`verify_iat` could be switched on
  without a test noticing; the scope challenge's body reader, headers and pass-through paths were unasserted; and
  the credit ledger's window edge (`<` vs `<=`) survived because a random draw never lands on the boundary -
  now pinned with an explicit example.

### Changed
- Three tests that could never fail were replaced by assertions that can (see `docs/QUALITY.md`).


Phase 4b of [ROADMAP.md](ROADMAP.md): quality gates that measure whether the suite would catch a bug.
Details and every number in [docs/QUALITY.md](docs/QUALITY.md).

### Added
- Coverage ratchet: branch coverage enforced in CI, floor 89 (measured 88.26 % at the start, 89.08 % now).
- 65 `hypothesis` properties over scopes, the credit ledger, Retry-After and backoff, OpenSky decoding and
  the airport loader; a derandomized 500-example `ci` profile.
- Mutation testing (`mutmut`) on `auth/`, the credit ledger and `agent/approval.py` in CI, with a survivor
  ceiling of 90 enforced by `scripts/mutation_gate.py` and every survivor judged in
  `docs/MUTATION-SURVIVORS.md`. 391 survivors on the first run.
- `scans` job: `gitleaks` over the tree and every commit, `pip-audit` over the lock.

### Changed
- The credit ledger is `credits.CreditLedger`; OpenSky decoding is `decode_states` / `decode_movements`;
  backoff is `http.backoff_delay`. One strict Retry-After parser replaces two that disagreed.

### Fixed
- `build_airports_db.py --limit N` loaded the whole file for any N below 5000; the loader counted repeated
  idents twice, stored a blank ident, crashed on an elevation of `inf`, and left its database open.
- A non-numeric `last_contact` in an OpenSky row crashed the most-recent-first sort; `on_ground: "false"` was
  read as true; a `states` field of `0`, `""` or `{}` was read as an empty sky.
- `Retry-After` of `inf`, `nan`, `1_000` or `-5` was read as a wait; OpenSky's parser ignored HTTP-dates.
- `roles: [1, null, ""]` became grants.

## [0.5.0] - 2026-11-17

Phase 4 of [ROADMAP.md](ROADMAP.md): the credibility gate — an eval harness that publishes how often the
thing is right, including when it is not.

### Added
- `evals/`: 54 golden scenarios as data, scored on four axes reported **separately** (tool selection,
  argument validity, refusal correctness, no hallucination) plus the answer class and writes, never
  averaged into one number that hides the weak axis. `answer_class` (`confirmed` / `unknowns` / `refused` /
  `error`) is what makes a fluent wrong answer scoreable as wrong.
- A committed baseline (`evals/baseline.json`) and a regression rule counted two ways, stricter decides:
  more than 2 points on any line, **or** more than 0 scenarios failing a check they passed in the baseline.
  With 54 scenarios one scenario is 1.85 points, so points alone would miss a single regression.
- `evals (offline)` CI job: no model, no network, 30 s including setup, with the 60 s cap enforced twice.
- The README results table is generated from the baseline between markers; CI fails if the two disagree.
- `docs/EVALS.md`: what is measured, what is deliberately not, and how to add a scenario.

### Changed
- `Runtime.data_clock`: a second clock for judging **data** staleness, distinct from the wall clock that
  token expiry must use. Fixtures set it to the scenario's start.
- Movement windows now end at the data clock rather than the wall clock.

### Fixed
- A METAR older than 90 minutes, or one with no readable time, is reported in `unknowns` instead of being
  presented as current.
- A states snapshot older than 5 minutes, or one with aircraft but no time, is reported in `unknowns`.
- Unreadable rows in a states or movements payload are counted and the count is labelled a lower bound,
  instead of being silently dropped and the readable remainder presented as the total.
- `airport_movements` no longer returns `upstream_unavailable` on a documented array body; this had made
  `disruption_triage` report severity unknown with one source down.

## [0.4.0] - 2026-11-16

Phase 3 of [ROADMAP.md](ROADMAP.md): an agent that proposes, never commits.

### Added
- `agent/`: a thin plan → tool calls → reflect loop over a provider interface, with `anthropic`,
  `openai-compatible` and a deterministic `offline` backend. The offline policy runs the real code path —
  real dispatch, real approval gate, real transcript — and is what CI runs.
- `propose_rebooking`, a write gated by a **signed approval grant for one payload** (`docs/adr/0003`): an
  approval for one proposal cannot authorise a different one later.
- An idempotency key on the write, so a retry returns the original result and cannot double-book; counted
  at the mock, and still true when a different person approves the replay or the clock moves on.
- Agent budget accounting, separate from the server's upstream credit ledger; a run over budget stops with a
  stated reason.
- JSON transcripts with a content hash, shaped for the Phase 4 eval harness.
- Eight scenarios as data, including self-approved, no-approval-channel, unanswered, declined, replayed and
  over-budget.
- A CI job asserting offline determinism: one scenario twice with equal hashes, then a different scenario
  whose hash differs.

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
