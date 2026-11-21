# Architecture: how a request travels, and where each guard sits

Ten minutes for a reviewer who will not open the code. This page says where things are and what refuses.
It does not repeat the detail: [`PROTOCOL.md`](PROTOCOL.md) owns the MCP surface, [`QUALITY.md`](QUALITY.md)
the gates, [`EVALS.md`](EVALS.md) the numbers, [`DASHBOARD.md`](DASHBOARD.md) the display rules. Paths are
relative to the repository root; `server.py` means `src/flightops_mcp/server.py`.

## One request, end to end: an agent over HTTP

`airport_live_traffic(airport="ESGG")`, because it reaches a metered upstream and so meets every guard.

```
  MCP client --- POST /mcp, Authorization: Bearer <token> ---+
                                                             v
  [1] bearer check          auth/verifier.py, auth/jwks.py   401, no tool runs
  [2] scope challenge       auth/challenge.py                403 + WWW-Authenticate scope=...
  [3] scoped                server.py                        {"ok": false} insufficient_scope
  [4] guarded               server.py                        any raise becomes {"ok": false}
  [5] cached                cache.py                         hit: answered here, 0 credits
  [6] the tool body         server.py
  [7] credit ledger         credits.py, opensky.py _charge   {"ok": false} local_budget_exhausted
  [8] upstream client       http.py, opensky_token.py        passthrough hook aborts the request
                                                             |
                                                  OpenSky /states/all
                                                             |
  [9] payload: ok, unknowns, upstream_time_epoch, source{name, licence}, bookkeeping.credits_spent
```

1. **Transport and bearer check.** The streamable-HTTP server (`create_http_server`) will not start without
   an issuer and a JWKS URL (`build_resource_server_auth` in `auth/__init__.py`). The SDK's middleware calls
   `JwtVerifier`: RS256 only, issuer, audience equal to this server, expiry with no leeway. The SDK then
   checks the audience a second time (`validate_token_resource=True`). Stdio has no token and no step 1-3:
   it is the local, single-user mode.
2. **Scope challenge.** `ScopeChallengeMiddleware` reads the JSON-RPC body before the protocol layer, so a
   `tools/call` without the tool's scope gets HTTP 403 with `WWW-Authenticate` naming the scope, and the
   client can step up. Batches are inspected too.
3. **`scoped`.** The same decision again, next to the tool, for any request shape step 2 did not parse. It
   fails closed: no verified token in context is `authentication_required`, not a pass.
4. **`guarded`.** One span per tool. A `ToolFailure` becomes its payload; any other exception becomes
   `internal_error`. Nothing reaches the caller as a raise.
5. **`cached`.** The order is `scoped(guarded(cached(tool)))` (`tool()` and `_published` in `server.py`), so
   the cache is consulted only after the scope check ([ADR 0004](adr/0004-tool-results-are-cached-inside-the-scope-check.md)).
   Each tool has a `ttlMs`/`cacheScope` entry in `TOOL_CACHE_HINTS`; this one is 10 s, `public`. A
   `private` result (`flight_passenger_impact`) is keyed by subject, client and granted scopes, and with no
   verified caller it is not cached at all. Only `ok: true` is kept. Concurrent identical calls make one
   upstream call. A hit carries `cache.hit: true` and `credits_spent: 0`.
6. **The tool body** resolves the airport from the bundled reference and builds a bounding box. The box
   prices itself (1 to 4 credits, `BBox.credit_cost` in `opensky.py`).
7. **Credit ledger.** `OpenSkyClient._charge` charges before the request, because a timed-out call still
   costs upstream. The hourly `CreditLedger` refuses a charge that would cross its budget, whole. Inside
   `daily_metered()` (the board, and `airspace_snapshot`) a 24-hour ledger is checked too, and a refusal
   by either charges neither.
8. **Upstream client.** `UpstreamHttp` in `http.py` holds this server's own OAuth2 client-credentials token
   (`opensky_token.py`, single flight), the retry and `429` policy, and the circuit breaker. Its `httpx.AsyncClient`
   carries `refuse_inbound_token`, which aborts any request containing the caller's token.
9. **Payload.** `upstream_time_epoch` is OpenSky's `Date` header, the clock every age is measured against.
   `unknowns` says what could not be observed. `source` names the upstream and its licence terms. The SDK
   returns it as structured content, held by tests to the output schema `schemas.py` defines for it.

## The same tool, from the board

The board (`dashboard/`) is a FastAPI app in its own process. It calls the tool functions in-process, not
over MCP, so steps 1-3 do not apply; the passcode gate and a fixed tool list stand in for them.

```
  browser --- GET /api/traffic/{region} ---+
                                           v
  passcode gate        dashboard/app.py, gate.py     403 (non-local Host, cross-origin) / 401 / login
  BoardService         dashboard/service.py          TrafficSpend.refusal(): hourly layer ceiling
  LiveSource           dashboard/sources.py          LIVE_TOOLS: eight read tools, named one by one
                       inside daily_metered()        the 24-hour ledger applies (step 7)
  airspace_snapshot    guarded(cached(...))          steps 4-9 above, unchanged
                                           |
  board.airspace_view / board.banner / board.last_update    -> JSON shaped for drawing
```

Without a passcode the board binds to `127.0.0.1` and refuses any other `Host`, so a tunnel cannot reach it.
With one, every route but the login needs an HMAC-signed cookie whose key is generated at startup and never
stored. The board has no route to `flight_passenger_impact` or `propose_rebooking`. Its display rules are
in `dashboard/board.py`; the page draws what that module decided.

## Where each guard lives

| Guard | File | Proven by | When it refuses |
|---|---|---|---|
| Scope table | `src/flightops_mcp/auth/scopes.py` | `test_scopes.py::test_a_tool_without_a_scope_entry_cannot_be_registered`, `test_every_tool_runs_with_exactly_its_own_scopes`, `test_a_batch_cannot_carry_a_tool_call_past_the_scope_check` | Unlisted tool: import fails. Missing scope: 403 challenge, then `insufficient_scope` payload |
| Annotation table | `src/flightops_mcp/annotations.py` | `test_protocol_surface.py::test_a_tool_without_annotations_cannot_be_registered`, `test_the_write_is_destructive_and_idempotent` | Unannotated tool: import fails |
| Licence registry | Not in this repository (README: the commercial edition holds it). Here: a `source.licence` on every payload, constants in `server.py` | `test_protocol_surface.py::test_every_payload_validates_against_its_own_schema` (`source` is a closed object in `schemas.py`) | Nothing refuses on licence grounds; the terms are carried, not enforced |
| Passthrough scan | `src/flightops_mcp/auth/passthrough.py` | `test_no_passthrough.py::test_every_outbound_http_client_installs_the_guard`, `test_an_attempt_to_forward_the_token_is_blocked_and_the_reason_is_logged` | Request aborted before sending; `token_passthrough_blocked` payload; the token is not logged |
| Cache partitioning | `src/flightops_mcp/cache.py` | `test_cache.py::test_private_results_are_partitioned_by_subject_client_and_scopes`, `test_a_private_result_with_no_verified_caller_is_never_stored`, `test_a_caller_without_the_scope_never_reaches_the_cache` | No refusal: a private result with no caller bypasses the cache |
| Ledger ceiling (hourly, daily) | `src/flightops_mcp/credits.py`, `OpenSkyClient._charge` in `opensky.py` | `test_properties_credits.py::test_the_ledger_agrees_with_a_model_of_the_window`, `test_opensky_modes.py::test_the_daily_guard_refuses_and_charges_neither_ledger` | `local_budget_exhausted` with the numbers; nothing sent, nothing charged |
| Board traffic ceiling | `TrafficSpend` in `dashboard/service.py` | `test_board_world.py::test_the_ceiling_refuses_with_the_numbers_and_when_it_frees` | Auto-refresh stops; the badge says why and when the next refresh fits |
| Approval gate | `src/flightops_mcp/auth/approval.py`, `propose_rebooking` in `server.py` | `test_rebooking_gate.py::test_saying_yes_is_not_an_approval`, `test_the_proposer_cannot_approve_their_own_proposal`, `test_changing_the_proposal_after_it_was_approved_is_refused` | `input_required` asking for a signed grant; nothing written until one matches this exact proposal ([ADR 0003](adr/0003-an-approval-is-a-signed-grant-not-an-answer.md)) |
| Credit floor (track purchase) | `remaining_credits`, `tracks_refusal` in `dashboard/inbound.py` | `test_inbound.py::test_the_fewest_credits_any_guard_has_left`, `test_too_few_credits_is_a_refusal_in_the_answer_and_nothing_is_asked` | The lowest of the hourly and daily remainders decides; refusal in the answer, no upstream call |
| Passcode gate | `dashboard/gate.py`, `guard` in `dashboard/app.py` | `test_dashboard.py::test_without_a_passcode_a_tunnel_hostname_is_refused`, `test_a_passcode_gates_every_route_but_the_login`, `test_forged_and_expired_cookies_are_refused` | 403, 401, or redirect to `/login`; lockout after 10 failures a minute |
| a11y gate | `scripts/a11y.mjs` (CI job `a11y` in `.github/workflows/ci.yml`), fixture `scripts/a11y_server.py` | `test_a11y_server.py::test_the_fixture_keeps_the_real_gate`, `test_a_tool_the_fixture_does_not_cover_fails_instead_of_inventing` | CI fails on any serious or critical axe-core violation; detail in [`QUALITY.md`](QUALITY.md) |

## The board's modes

`board.banner` in `dashboard/board.py` computes the mode from the payloads, never from a setting.

- **`LIVE`**: at least one airport has a current METAR, meaning the tool judged it no more than 90 minutes
  from NOAA's `Date` header (`METAR_STALE_AFTER_S`). Exists so a coloured marker always means a current
  observation.
- **`STALE`**: METARs came back with ages, and none is current. Exists so old weather is never drawn as
  conditions now.
- **`RECORDED <time>`**: the source is a recording (`RecordedSource` in `dashboard/sources.py`). It shows when
  the recording was made and no age, because an age measured during playback would describe the playback.
- The code has a fourth, **`NO DATA`**: no METAR with an age at all, including no upstream `Date`.

**Why upstream time.** Every age is `upstream_time_epoch` minus the data's own time. Without an upstream
`Date` the age is unknown and the payload says so; the host clock is never the fallback. The host this was
built on ran 21 min 27 s fast and turned all live data `STALE`. Proven by
`test_dashboard.py::test_weather_is_live_against_noaas_clock_with_the_host_21_min_fast`,
`test_traffic_is_stale_by_openskys_clock_whatever_the_host_says`, `test_the_board_reads_no_clock_of_its_own`
and `test_banner_modes_are_computed_from_the_views`. The banner is about weather; traffic has its own line
on the last-update badge (`board.last_update`), so neither age stands in for the other.

## What is deliberately not there

- **No message queue.** A tool call is request and response with a model waiting on it. A queued answer
  would arrive older than its data, and a refusal would reach nobody who could act on it.
- **No database in the request path.** No database server and no writable store. The one file the server reads per request is the
  airport reference, a SQLite file built offline and opened read-only (`airports.py`, `mode=ro`). Ledgers
  and cache are in memory, so a restart resets them; the board's strip says so
  (`daily_ledger_survives_restart`) next to OpenSky's own remaining count.
- **No background workers.** Nothing polls an upstream on its own. Every credit is spent because a caller
  asked, so spend maps one-to-one to requests and an idle server costs nothing. The board refreshes because
  the browser asks.
- **No model in the hot path.** Triage severity is code (`_severity` and `_why_not` in `server.py`), so the
  same evidence gives the same answer and every step can be tested. The model is the caller; the agent loop
  in `agent/` is a client.

## The shape of a failure

Failure is data. A wrong identifier, from `resolve_airport("ZZZZ")`, as the server returns it:

```json
{
  "ok": false,
  "error": {
    "code": "not_found",
    "message": "No airport matched 'ZZZZ'.",
    "hint": "Resolve the identifier first with resolve_airport, then retry with the ICAO code. Report the miss to the user rather than substituting a similar airport."
  }
}
```

A success that observed nothing. `airport_weather("ESGG")` when NOAA answers `204`:

```json
{
  "ok": true,
  "metar": [],
  "latest_metar": {"age_s": null, "current": false},
  "impact_flags": [],
  "unknowns": [
    "No METAR returned. That means NOAA holds no current observation for this station, not that conditions are good."
  ],
  "source": {"name": "AviationWeather.gov (NOAA)", "licence": "US Government open data, public domain"}
}
```

(Abridged: `upstream_time_epoch`, `airport` and `taf` omitted.) Fed the same empty weather and no OpenSky
credentials, `disruption_triage` returns `severity: "unknown"`, not `low`, with `"No positive evidence from
any source (weather, traffic, movements). Nothing here shows that operations are normal."` among its
`unknowns`.

## Where the code and the docs disagree

`README.md` says the passthrough guard is "a hook on every HTTP client", and `CLAUDE.md` says every
`httpx.AsyncClient` takes it. One does not: the token-exchange client in `src/flightops_mcp/auth/obo.py`.
That is correct. Its job is to present the caller's token to the issuer as the subject of an RFC 8693
exchange, and the hook would block exactly that. The scan test exempts the file by name
(`exempt = {SRC / "auth" / "obo.py"}`) and scans only `src/flightops_mcp/`. The guarantee is therefore:
every client that talks to a data upstream carries the hook, and the one client that talks to the issuer
is exempt on purpose. A new client added to `obo.py` would not be scanned.
