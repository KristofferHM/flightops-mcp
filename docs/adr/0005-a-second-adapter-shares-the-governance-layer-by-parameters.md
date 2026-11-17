# ADR 0005: The second upstream shares the governance layer by parameters, and is proven against a mock

- Status: accepted
- Date: 2026-11-17
- Phase: 5 (`v1.0.0` candidate)

## Context

The claim this project makes is that the governance layer (scopes, credit ledger, passthrough guard,
error taxonomy, rate-limit contract) is independent of the upstream. With one authenticated upstream
that claim is untested. The roadmap asked for a second adapter with a *different auth shape* and "no
new abstractions": if proving the claim needed a new abstraction, the claim was wrong.

Schiphol's Public Flight API was chosen over Amadeus. Its current documented auth shape
(developer.schiphol.nl, "API Consumer Migration Guide", 2026-07-20):

- `POST https://api.auth.schiphol.nl/oauth/token`, form-encoded, `grant_type=client_credentials`,
  `client_id`, `client_secret` and a required **`audience`** (`https://api.schiphol.nl/public` for the
  public tier). A wrong audience is rejected with 401 or 403. `expires_in` is 1800.
- Refresh **60 s before expiry**, not after a 401.
- `GET https://api.schiphol.nl/public/public-flights/v4/flights`, `Authorization: Bearer`. The version
  is in the path. The old `app_id`/`app_key`/`ResourceVersion` headers are retired; the legacy
  platform was decommissioned in October 2026.

The brief for this phase described the mock's auth shape as "an API-key header plus an OAuth2
client-credentials token with an audience". The vendor's guide says the API-key headers were
*replaced* by the token. The adapter implements the documented shape, so it sends no API-key header,
and the mock counts any such header as a client error. This divergence from the brief is deliberate.

No Schiphol key was available, and none may be committed.

## Decision

### Shared, not copied

| Governance piece | How Schiphol gets it | What changed to allow it |
|---|---|---|
| Token lifecycle: cache, skew, single flight, invalidate | `ClientCredentialsTokenManager` | The OpenSky manager read its URL and credentials from OpenSky settings. Those became constructor parameters, plus `audience` and `skew_s`. `OpenSkyTokenManager` is now that class with OpenSky's settings. |
| 401 retry-once, 429 wait-or-surface, error statuses | `http.authorized_get` | This policy lived inside `OpenSkyClient._authorized_get`. It moved to `http.py` as a function both adapters call. A rate-limit rule inside one adapter is a rule the next adapter does not follow. |
| Retries, backoff, circuit breaker, passthrough guard, spans | `http.UpstreamHttp` | Nothing. It already took an upstream name and base URL. |
| Credit ledger | `credits.CreditLedger(budget)`, charged before each request | Nothing in the ledger. A labelled counter, `flightops_upstream_credits_spent_total{upstream}`, was added: the existing metric was OpenSky-named. |
| Error taxonomy | `errors.py` | `missing_credentials` took the upstream's name and variables as parameters. Every other failure was already parameterised. |
| Scope | `flightops.schedule.read` in `Scopes` | One row. The two enforcement points are untouched. |

**No new interface, base class or registry was needed.** Two hard-coded things had to become
parameters (the token manager's settings and one error's wording) and one policy had to move out of
the class that happened to have it first. That is the honest finding: the layer was shared in
intent, and two pieces were OpenSky-bound in fact.

Schiphol's ledger is its own instance with its own budget (`schiphol_request_budget_per_hour`,
1200), counted in requests, not credits. One shared instance would let a Schiphol burst spend
OpenSky's budget, and the two units are not the same.

### Proven against a mock of the documentation

`services/mock_upstream` implements the documented shape strictly. It refuses a token request
without `audience` (400) or with the wrong one (403), refuses an unknown or expired bearer or one for
another audience (401), pages with a `Link: rel="next"` header, and counts every one of those, plus
any retired header. It also serves OpenSky and AviationWeather shapes for the load test. Secrets
arrive by environment; with none set it refuses every client.

Where it runs: `tests/test_schiphol.py` (26 tests), four golden eval scenarios, `scripts/second_upstream.py`
through the compose matrix in CI, and through local processes in `scripts/load_test.sh`.

Labelled everywhere: the adapter's docstring, the tool's description (which the model reads), every
payload's `source.verification`, the README capability table, `docker-compose.yml`, and this ADR.

## What this proves, and what it does not

It proves that a second upstream with a different auth shape goes through the **same** scope check,
ledger, passthrough guard, retry and rate-limit policy and error taxonomy, with no code written for
it in any of them. A scope refusal reaches nothing and spends nothing; a call debits the ledger by
the number of requests the mock served; the ledger stops it with OpenSky's own error code; the
caller's token never reaches the mock; the token is refreshed before it expires and never used after.

It does **not** prove that Schiphol behaves as documented, that the v4 field names this adapter reads
are still current, that the public tier's rate limit (100 requests a minute, from a secondary source)
is right, or that `fromDateTime` in local time is how the live API wants it. Those need one run with
a real key, which arrives through `FLIGHTOPS_SCHIPHOL_CLIENT_ID` and `FLIGHTOPS_SCHIPHOL_CLIENT_SECRET`.

## Consequences

- `amsterdam_offset` is a hand-written EU summer-time rule, because Windows Python has no tz database
  and adding `tzdata` for one timezone was not worth a dependency. It is tested at both boundaries.
- The eval set grew to 58 scenarios. `refusal-credential-schiphol-audience` makes a wrong audience a
  credential failure, not an empty schedule.
