# Load test: 50 requests per second for 60 seconds

Two runs on one laptop, 2026-11-17, against the mock upstream. Both result files are committed
unedited: [`load/2026-11-17-50rps-60s.json`](load/2026-11-17-50rps-60s.json) (as shipped, cache on)
and [`load/2026-11-17-50rps-60s-no-cache.json`](load/2026-11-17-50rps-60s-no-cache.json) (the
same load with the result cache switched off).

## Reproduce it

```bash
bash scripts/load_test.sh                                   # the shipped configuration
FLIGHTOPS_CACHE_ENABLED=false bash scripts/load_test.sh     # the counterfactual
LOAD_RPS=20 LOAD_DURATION=30 bash scripts/load_test.sh      # anything else
```

The script starts the local IdP, the mock upstream and the server as local processes, runs the
second-adapter check (`scripts/second_upstream.py`), then `scripts/load_test.py`, and stops
everything. It needs no account, no key and no Docker, and spends no real credit. Secrets are
generated for the run. The result is printed and written to `$LOAD_OUT`.

A small version of the same load (40 requests through the real server, IdP and mock, in process)
runs in the test suite on every push: `tests/test_mock_upstream.py`.

## The machine

| | |
|---|---|
| CPU | Intel Core i7-7600U @ 2.80 GHz: **2 cores, 4 threads** (a 2017 laptop part) |
| Memory | 16 GB |
| OS | Windows 10 Education 10.0.19044, git-bash |
| Python | 3.12.12, through `uv run` |
| Everything on it | load generator, server, local IdP and mock upstream: four processes on two cores |

The generator competes with the server for the same two cores, so these numbers are a floor for
the server, not a ceiling for the design. They are also the only numbers in this repository: nothing
here was measured anywhere else.

## Method

- **Open loop.** Request *i* is sent at `start + i/50` whether or not earlier ones have answered,
  and its latency is measured **from that scheduled instant**. A closed loop slows down with the
  server and hides queueing; this one does not. Both runs held the rate: 3000 sent in 59.97 s,
  worst send lag 8.7 ms and 10.0 ms.
- **Authenticated, over HTTP.** Every request carries a bearer token from the local IdP and goes
  through the real Streamable HTTP app: JWT verification, the scope middleware, the scope check
  next to the tool, the tool, the upstream client. One user token for the whole run.
- **A fixed mix**, ten calls per cycle over eight airports: `resolve_airport` ×2, `airport_weather`
  ×2, `airport_live_traffic` ×2 (40 nm and 60 nm), `airport_movements`, `schiphol_flights`,
  `disruption_triage`, `upstream_status`. No writes: a rebooking needs a person's approval per call.
- **Upstream latency 25 ms** per data request, simulated by the mock (`MOCK_LATENCY_MS`).
- **Spend is the server's own count**, read from `/metrics` before and after; upstream requests
  are the mock's own count from `/stats`. Neither is the client's estimate.
- An **error** is anything that is not an `ok: true` answer: a transport failure, an HTTP status
  other than 200, a JSON-RPC error, or a tool failure payload. The last is still an answer, and is
  broken out separately.

## Results

| | Shipped (cache on) | Cache off |
|---|---:|---:|
| Requests completed | **3000** of 3000 | 3000 of 3000 |
| Error rate | **0.00 %** | 27.87 % (836 `local_budget_exhausted`) |
| Latency p50 (from schedule) | **10.1 ms** | 30.1 ms |
| Latency p95 | **41.1 ms** | 109.1 ms |
| Latency p99 | **67.7 ms** | 146.2 ms |
| Latency max | 149.9 ms | 258.3 ms |
| OpenSky credits spent | **160** | 400 (the local budget, then refused) |
| Schiphol requests spent | **2** | 600 |
| Upstream requests served by the mock | 131 | 2549 |
| Cache: hit / miss / coalesced | 3166 / 121 / 13 | off |

Hits outnumber requests because `disruption_triage` calls three cached tools itself.

**What the second column shows.** Without the cache, the same questions drain the local OpenSky
budget (400 credits an hour, `FLIGHTOPS_CREDIT_BUDGET_PER_HOUR`) in under a minute. The ledger then
refuses every credit-spending call with `local_budget_exhausted`, as data and at once. It stopped
at exactly 400 under concurrent load, which is the property the ledger's tests claim. The cache is
what keeps fifty agents asking about eight airports at a hundred and sixty credits a minute instead
of an hour's budget.

Per tool, cache on (p50 / p95 from schedule, ms): `airport_live_traffic` 14.6 / 58.5,
`airport_movements` 10.3 / 35.0, `airport_weather` 7.9 / 34.7, `disruption_triage` 8.9 / 38.7,
`resolve_airport` 9.2 / 32.4, `schiphol_flights` 10.0 / 40.7, `upstream_status` 8.9 / 30.2.

## What this does not show

- **A cold, cache-hostile workload.** Eight airports repeat every 1.6 s, well inside every TTL. An
  operator whose agents ask about hundreds of airports gets numbers closer to the second column,
  and a smaller hit rate.
- **Private caching under load.** One user, and no private-scope tool in the mix. Partitioning by
  identity is proven by tests (`tests/test_obo.py`, five users, fifty concurrent calls), not here.
- **Real upstreams.** 25 ms is a stand-in; OpenSky from a home connection is slower and its own
  rate limit would bind long before ours.
- **Multiple replicas or a long soak.** One process for one minute. The cache and the ledger are
  per process, so two replicas have two caches and two budgets.
- **Throughput limits.** 50 rps was the target and it held. The rate at which this machine stops
  holding it was not measured.
