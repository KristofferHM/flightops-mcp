# Roadmap and shipping plan

This is the plan for taking `flightops-mcp` from a verified `v0.1.0` skeleton to a `v1.0` that stands on
its own as a product: a governed MCP server an operations team could actually run, with the security,
observability and evaluation evidence to prove it.

Assumed budget: **~8-10 hours per week** (evenings and weekends). Every estimate below is in weeks at
that rate, not in full-time days.

---

## 1. What the product is

**One sentence:** a governed MCP server that gives AI agents scoped, auditable, rate-limited access to
airport operations data, and refuses to answer when its evidence is thin.

**Who it is for:** an airline, airport operator or ground handler that wants an agent to answer
"what is happening at ESGG right now" without handing the model an unrestricted API key.

**Non-goals, stated so scope cannot creep:**

| Not building | Why |
|---|---|
| A booking or rebooking engine | A write path is in scope as *one* demonstrable tool, not as a product surface |
| An LLM app or chat UI | The valuable, hard, defensible part is the governed tool layer |
| A data warehouse of flight history | OpenSky's own Trino interface serves that better |
| Multi-tenant SaaS billing | Out of scope until there is a user base to bill |

## 2. Where it stands today (`v0.3.0`, shipped)

| Fact | Evidence |
|---|---|
| 6 typed MCP tools, revision 2026-07-28 | `tests/test_server_tools.py::test_all_tools_are_published_with_schemas` |
| OAuth2 client credentials, single-flight refresh | `test_fifty_concurrent_callers_make_exactly_one_token_request` |
| OAuth 2.1 resource server: JWKS, scopes, no passthrough, OBO | Phase 1 matrix, **12/12**, locally and in `docker compose` |
| `429` contract honoured, not absorbed | `test_long_rate_limit_wait_becomes_a_tool_error_not_a_blocked_call` |
| Local credit ledger | `test_local_credit_budget_stops_a_runaway_loop` |
| PII/secret redaction, canary-tested | `tests/test_redaction.py` |
| One trace per request, client `traceparent` parenting | `tests/test_otel.py::test_one_triage_call_is_one_trace_with_nested_spans` |
| Circuit breaker, fail fast | `test_the_circuit_fails_fast_rather_than_sleeping` |
| Liveness, readiness, metrics endpoints | `tests/test_health.py` |
| **300 tests, 0 skipped**; ruff + mypy strict clean | CI |

**What it is not yet:** an agent loop, an evaluation set, or a write path. Those are Phases 3–5.

## 3. Release plan

| Version | Theme | The single question it answers | Est. | State |
|---|---|---|---|---|
| `v0.2.0` | Auth-hardened remote server | "Can this run as a service without leaking credentials?" | 2 weeks | **shipped** |
| `v0.3.0` | Observability and operations | "Can an SRE run and debug it?" | 1.5 weeks | **shipped** |
| `v0.4.0` | Agent loop with a human approval gate | "Can an agent use it, and stop when it should?" | 2 weeks | next |
| `v0.5.0` | Evaluation harness | "Is it actually good, measurably?" | 1.5 weeks | |
| `v1.0.0` | Product hardening + docs | "Would I deploy this for a real operator?" | 1 week | |

Total to `v1.0`: **~8 weeks** at 8-10 h/week, with the `v0.5` evidence gate being the one that must not
be skipped.

---

## Phase 1 — `v0.2.0` Auth-hardened remote server (2 weeks)

The 2026-07-28 revision made MCP stateless, so the interesting work is no longer session plumbing: it
is being a correct OAuth 2.1 **resource server** while calling a downstream API as itself.

### Deliverables

1. **Streamable HTTP transport** via `mcp.run(transport="streamable-http")` behind uvicorn, with an
   `AuthSettings`/`token_verifier` that validates RS256 JWTs against a JWKS endpoint.
2. **Local IdP for the whole test matrix** — a small FastAPI service in `services/idp/` that mints
   tokens and exposes JWKS, so no cloud tenant is needed to test any of this:
   - `client_credentials` (service principal, M2M)
   - `urn:ietf:params:oauth:grant-type:token-exchange` (RFC 8693) for **on-behalf-of**: exchange an
     incoming user token for a narrower downstream token
   - revoke/expire endpoints to test failure paths on demand
3. **Scope model in code, not comments.** `Scopes.for_tool(name)` maps each tool to required scopes; a
   missing scope returns `WWW-Authenticate` with the needed scope and a tool error naming it.
4. **No token passthrough.** The server must reject a token whose `aud` is not itself, and must never
   forward an inbound token upstream — the spec forbids it and it is the confused-deputy bug.
5. **OBO wired to the mock ops API** — a second, deliberately PII-bearing service that accepts only
   exchanged tokens, so the on-behalf-of path is demonstrated end to end.

### Acceptance criteria (all automated)

- [x] `401` for absent/expired/wrong-`aud` tokens; `403` + scope challenge for insufficient scope
      (live, local processes: 401 ×4 incl. other issuer, 403 with `scope="flightops.weather.read"`;
      `docs/LOCAL-MATRIX.md` lines 1-6)
- [x] Token exchange narrows scope and cannot widen it (test asserts escalation fails)
      (live: `scp` narrowed to `flightops.passengers.read`; three escalation attempts all `400`, none issued;
      lines 7-8)
- [x] Passthrough attempt is rejected, with the reason logged
      (live: wrong-audience and replayed exchanged tokens `401` with `wrong_audience` logged; the outbound
      guard itself is pytest-only, see "What this does not prove"; line 9)
- [x] 50 concurrent authenticated calls produce exactly one exchange per identity
      (live: counted in the IdP's access log, 1 `POST /token` for 50 answered calls; line 10)
- [ ] The whole matrix runs in `docker compose up` with no external account
      **unverified on this host (no Docker)**: `docker: command not found`, so no image was built and
      compose was never run. The same three services were proven as local processes instead.
      The CI job that will run it exists (`compose-matrix` in `.github/workflows/ci.yml`) and **has not
      run yet**. Tick this box when that job is green on a pushed commit, and cite the run.

### Risks

| Risk | Mitigation |
|---|---|
| SDK auth APIs shift under us | Pin `mcp==2.3.0`; keep authn behind one `auth/` module so a version bump is one file |
| IdP stub drifts from real Entra behaviour | Model it on the documented Entra claims (`aud`, `scp`, `roles`, `tid`); document the differences in an ADR |

---

## Phase 2 — `v0.3.0` Observability and operations (1.5 weeks)

### Deliverables

1. **OpenTelemetry traces** across the boundaries: client → MCP server → upstream, with `traceparent`
   propagated in MCP `_meta` exactly as the 2026-07-28 revision specifies.
2. **`/metrics`, `/healthz`, `/readyz`** on the HTTP transport: tool call rate and latency, upstream
   status classes, credit spend, token refreshes, circuit-breaker state.
3. **Circuit breaker per upstream**, so a dead NOAA does not turn every triage call into a 15-second
   timeout.
4. **Compose stack**: server + IdP + mock ops API + Jaeger + Prometheus, one command up.
5. **Operator runbook** (`docs/runbook.md`): what each alert means, how to rotate the OpenSky client,
   what to do when credits are exhausted.

### Acceptance criteria

- [ ] One triage call produces one trace with ≥4 spans and correct parent/child ordering, asserted in a test
- [ ] A forced upstream failure opens the breaker and fails fast (test measures elapsed time)
- [ ] `/healthz` and `/readyz` distinguish "process alive" from "dependencies usable"
- [ ] Dashboards: one Grafana JSON committed, not screenshotted

---

## Phase 3 — `v0.4.0` Agent loop with a human approval gate (2 weeks)

The agent is deliberately thin: the server is the product, the agent is the proof it is usable.

### Deliverables

1. **`agent/` loop**: plan → tool calls → reflect, with a provider interface and three backends —
   `anthropic`, `openai-compatible`, and **`offline`** (deterministic scripted policy, no model, used in
   CI and for demos without any API key).
2. **One write tool** (`propose_rebooking`) against the mock ops API, with:
   - an **idempotency key** so a retried call cannot double-book
   - a **human approval gate** using MRTR / `InputRequiredResult` (`resultType: "input_required"`), so
     the agent cannot commit an action on its own
   - an audit record: who approved, what the model proposed, what executed
3. **Budget accounting end to end** — the agent's own token/credit budget, surfaced per run.

### Acceptance criteria

- [x] Offline mode runs the same scenario deterministically in CI, byte-identical transcript
      (CI run [44028182429](https://github.com/KristofferHM/flightops-mcp/actions/runs/44028182429), job
      `agent offline determinism (phase 3 acceptance)`, green on `feat/phase-3-agent-loop`: an approved scenario
      run twice with equal hashes, then a declined scenario whose hash differs. Locally the same property is
      `tests/test_agent_offline.py`, including `test_the_transcript_carries_no_wall_clock_time` and
      `test_the_loop_reads_no_clock_and_no_entropy_of_its_own`)
- [x] The write tool refuses without an approval response, and the refusal is testable
      (`tests/test_agent_offline.py::test_without_a_valid_approval_the_write_is_refused_and_the_api_is_never_asked`
      — the refusal is a payload, and the mock is asserted never to have been asked; the approval itself is a
      signed grant for one payload, `docs/adr/0003`)
- [x] Replaying the same idempotency key returns the original result, not a second write
      (`tests/test_ops_rebookings.py`: replayed key returns the original bytes and writes nothing, still does so
      when a different person approves the replay, and survives the clock moving on — counted at the mock, not
      the client)
- [x] Agent run writes a JSON transcript that the eval harness consumes
      (`tests/test_agent_transcript.py`: a sealed transcript reads back as itself, one edited after writing is
      refused, a document that is not a transcript is refused — the loader Phase 4 builds on)

**Verified 2026-11-16:** 637 tests pass locally, ruff and mypy clean over `src`, `services` and `agent`
(46 files). The CI run above is green on the branch. Not yet done: the branch is not merged and `v0.4.0` is not
tagged — those are the owner's decisions.

---

## Phase 4 — `v0.5.0` Evaluation harness (1.5 weeks) ← the credibility gate

A demo shows it working once. An eval shows how often it works, which is the difference between a
portfolio piece and an engineering claim.

### Deliverables

1. **≥40 golden scenarios** in `evals/scenarios/*.yaml`: question, fixture state, expected tool choice,
   expected argument shape, and the acceptable answer class.
2. **Scoring**: tool-selection accuracy, argument validity (schema + domain constraints), refusal
   correctness on missing scope/credentials, and **no-hallucination** when the upstream returns nothing.
3. **`uv run python -m evals.run --offline`** in CI, with a committed baseline table and a fail threshold
   on regression.
4. **Results in the README** as a table with the model used and the date — regenerated by the harness,
   never hand-edited.

### Acceptance criteria

- [ ] Harness runs offline in CI in under 60 s
      — **not ticked: no CI run of this branch has been observed yet.** Locally the full offline run of
      54 scenarios takes 9.5–11.8 s (10.11 s on `de1f6c9`), and the CI job enforces the limit twice
      (`--max-seconds 60` in the harness, `jq '.duration_s < 60'` after it). Tick it when the
      `evals (offline)` job on this branch reports its duration.
- [x] Baseline committed; a regression of >2 points fails the build
      — `evals/baseline.json`, 54 of 54, recorded 2026-11-17 (`69d43a5`), README table generated from it.
      The regression rule is counted two ways, and the stricter decides: more than 2 points on any line,
      **or** more than 0 scenarios failing a check they passed in the baseline. With 54 scenarios one is
      1.85 points, so points alone would miss a single regression on the wide lines. Shown failing on the
      CI command (`de1f6c9`): one expected answer broken → outcome and overall 100.00 → 98.15, exit 1,
      caught by the scenario rule (points: inside the limit, a note); one server check loosened with no
      scenario touched → no hallucination 100.00 → 95.00, exit 1, caught by both rules. Restored → 100.00,
      exit 0. CI's own "broken scenarios" step checks both rules on every push.
- [x] At least 8 scenarios specifically target the failure mode this project exists to prevent:
      answering confidently when evidence is absent
      — 20 `absent-*` scenarios over 9 kinds: `empty_result` 3, `malformed_payload` 3, `partial_coverage` 2,
      `rate_limited` 2, `stale_data` 2, `unauthenticated_read` 2, `unknown_airport` 2, `unknown_flight` 1,
      `upstream_error` 3. All 20 pass no hallucination. Three of them failed in the first baseline because
      the server stayed silent about stale and unreadable data; that was fixed in the server (`69d43a5`).

---

## Phase 5 — `v1.0.0` Product hardening and docs (1 week)

1. **Caching** using the revision's `ttlMs` / `cacheScope` hints on list and read results; deterministic
   tool ordering for prompt-cache friendliness.
2. **A second upstream adapter** to prove the architecture generalises — Schiphol (OAuth2 with an
   `audience` parameter) or Amadeus test env. Same governance layer, different auth shape, no new
   abstractions.
3. **Load test**: 50 rps for 60 s against the mock upstream, with the numbers in the README (p50/p95,
   error rate, credit spend).
4. **Docs**: README rewritten around the eval table, `docs/adr/` with 4-6 decision records, a 2-minute
   asciinema/GIF demo, `CHANGELOG.md`, tagged release with generated notes.
5. **Security pass**: `pip-audit`, image scan in CI, dependency pinning reviewed, secrets audit of the
   git history (`git log -p | grep -i token` as a sanity check).

## Phase 4b — Quality gates: mutation, coverage ratchet, properties (1 week)

Runs after Phase 4, because these gates **measure the suite** — there has to be an eval harness and a test
suite worth measuring before you ask whether they would catch a bug. Before `v1.0`, not after it.

1. **Coverage ratchet.** `--cov --cov-branch --cov-fail-under=N` in CI, `N` set to today's branch coverage
   rounded down, raised in the same commit as any new test. A floor that only goes up; never raised by adding
   exclusions.
2. **Property tests over the arithmetic.** `hypothesis` on `auth/scopes.py` (scope parsing, subset rules),
   the credit ledger, `429`/`Retry-After` arithmetic, and the airport reference loader's CSV parsing.
   `max_examples` raised in a CI profile.
3. **Mutation testing on the money paths.** `auth/` and the credit ledger — the code where a wrong answer is a
   security answer. A ceiling on surviving mutants, the report committed. Read the survivors: each one is
   either a missing test or an assertion that can never fail, and the second kind is the interesting one.
4. **Secret and dependency scanning**, which `CLAUDE.md` rule 5 already promises: `gitleaks` over the working
   tree *and* the history, `pip-audit` in CI. The repository is public; this is overdue rather than optional.

Run mutation testing in CI on Linux — it is slow and file-locking makes it flaky on a developer's Windows
machine. Gate it to those modules; a full-repo run is an hour nobody looks at.

### `v1.0` Definition of Done

- [ ] All five phases' acceptance criteria green in CI
- [ ] Coverage ratchet enforced with `fail_under`, branch coverage, no new exclusions
- [ ] Mutation ceiling met on `auth/` and the credit ledger, report committed
- [ ] `gitleaks` and `pip-audit` green in CI
- [ ] Eval table in the README with a date and a model name
- [ ] `docker compose up` gives a working stack with no external account
- [ ] Runbook, ADRs, changelog present
- [ ] One honest "what this still does not do" section, longer than the feature list

---

## Cross-cutting standards (apply to every PR)

1. **A test or it did not happen.** New behaviour lands with the test that would fail without it.
2. **Zero skipped tests.** CI fails on `skipped`; a skip must be removed or replaced by a fixture.
3. **Execution before commit.** Every release is exercised through its public entry point, not just
   imported. Both of the defects found in `v0.1.0` were discovered this way.
4. **Failures are data.** Nothing user-facing raises; `code` + `hint` + `retry_after_s` or it is a bug.
5. **No secrets in the repo, ever.** `.env.example` only; `gitleaks` before the first public push.
6. **Document the caveat next to the feature**, in the tool description the model reads — not in a wiki.

## Risk register

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Upstream terms change (OpenSky commercial clause) | Medium | High | Nothing commercial rides on it; swap adapter behind the same interface |
| MCP specification moves again mid-build | Medium | Medium | Pin the SDK; isolate protocol code in `server.py` + `auth/`; ADR records the revision |
| Scope creep into an LLM app | High | High | The non-goals table above is the contract; the agent stays ~200 lines |
| Evaluation set is too small to mean anything | Medium | High | 40 scenarios minimum before any results are published |
| Time: 8 weeks of evenings collides with the job hunt | High | Medium | Phases 1-4 each end in a shippable artifact; stopping after any phase still leaves a stronger repo than the last |

## What to show, in what order

For a hiring conversation, the evidence that matters is stacked in this order:

1. The eval table (how often it is right, and when it refuses)
2. The single-flight and `429` tests (engineering judgement made executable)
3. The canary redaction test (security taken seriously, demonstrated)
4. The ADRs and runbook (thinks like an owner, not a demo author)

The code is the fourth thing to show, not the first.
