# ADR 0004: Tool results are cached under `ttlMs`/`cacheScope`, inside the scope check

- Status: accepted
- Date: 2026-11-17
- Phase: 5 (`v1.0.0` candidate)

## Context

Revision 2026-07-28 gives a server two words for freshness: `ttlMs` (how long a result stays fresh)
and `cacheScope` (`public`: may be shared across authorization contexts; `private`: only within the
one that produced it). The SDK puts them on list and read results (`tools/list`, `resources/read`, …)
for the *client* to honour. It does not put them on `tools/call`, which is where this server spends
money: every traffic or movements call costs OpenSky credits, and fifty agents asking about the
same airport inside ten seconds would spend fifty credits for one answer.

Three things a cache in this position could get wrong, each worse than having no cache:

1. Serve one user's passenger counts to another user.
2. Serve a cached answer to a caller who lacks the scope for it.
3. Pin a transient failure, so a five-second outage looks like a minute-long one.

## Decision

**Use the protocol's two words for tool results too, inside the server, and make each tool's
choice a line of policy in code.**

- `TOOL_CACHE_HINTS` in `src/flightops_mcp/cache.py` gives every tool a `CacheHint(ttl_ms, scope)`,
  the SDK's own type. Registration fails for a tool with no entry, exactly as it does for a tool with
  no scope (`Scopes.for_tool`). Caching is decided per tool, never inherited by default.
- **`ttl_ms == 0` is not stored**, not even briefly: `propose_rebooking` (a write; idempotency is the
  ops API's job), `upstream_status` (process state) and `disruption_triage` (a composite whose
  severity must not outlive the evidence; its parts are cached). A zero-TTL tool is not even wrapped.
- **`private` is keyed by the authorization context**: subject, client and the sorted granted
  scopes. The same user with a narrower token is a different context. A private result with no
  verified caller (stdio) is not cached at all. Only `flight_passenger_impact` is private.
- **The cache sits inside the scope check.** `tool()` builds `guarded(cached(fn))`, and the HTTP
  server wraps `scoped(...)` around that. A refused caller is refused before the cache is consulted.
- **Only `ok: true` payloads are kept.** A failure payload or a raised failure goes to the caller and
  is forgotten.
- **A hit is labelled**: a `cache` block (`hit`, `ttl_ms`, `scope`), its original `upstream_time_epoch`
  (the upstream's clock when the data was fetched; `as_of` until 2026-11-19), and `credits_spent: 0`, so a composite that sums its parts' spend does not count
  a credit twice.
- **Concurrent identical calls are coalesced**: the first runs, the rest await its answer.
- **Observable**: `flightops_cache_requests_total{tool,outcome}` (hit, miss, coalesced, bypass) and
  `flightops_cache_entries` on `/metrics`.
- The cache belongs to the `Runtime`, so each test and each eval scenario has its own.

For the protocol's own surfaces, `tools/list` and `server/discover` carry `ttlMs: 300000,
cacheScope: "public"`. Public is correct because the list does not depend on the caller: scopes are
enforced when a tool is called, not by hiding it. **Tools are registered sorted by name**, so the
published list is byte-identical however the server was built. A host puts the tool list near the
top of the model's prompt, and a prompt cache hits only on an identical prefix.

## Consequences

- The load test's shipped configuration answers 3000 calls with 160 OpenSky credits and 0 errors;
  the same load with the cache off spends the 400-credit hourly budget in under a minute and has
  27.87 % of calls refused by the ledger (docs/LOAD-TEST.md).
- A cached traffic snapshot is up to 10 s older than it would have been. The payload says so
  (`cache.hit`, `upstream_time_epoch`), and the tool description tells the model what a hit means.
- One cache per process. Replicas do not share entries, and coalescing works within one process.
- Tests that count downstream calls per request (`tests/test_obo.py`) run with the cache off and
  say why; two tests run the same stack with it on and show one downstream read per identity.

## Proven by

`tests/test_cache.py` (25 tests: zero TTL, expiry at exactly the TTL, LRU bound, failures not kept,
single flight, partitioning by subject, client and scopes, no-identity bypass, scope before cache,
`/metrics`, list hints over the 2026-07-28 client, byte-identical order across builds and a reversed
registration), `tests/test_obo.py::test_a_private_result_is_never_served_to_another_identity` and
`::test_a_cached_private_result_is_not_served_to_a_narrower_token_of_the_same_user`.

## Not decided here

A shared cache across replicas (Redis or similar). It would need the same partitioning, plus a story
for a private entry at rest, and nothing measured so far asks for it.
