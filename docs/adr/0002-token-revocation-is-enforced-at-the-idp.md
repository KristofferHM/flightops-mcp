# ADR 0002: Tokens are revoked at the IdP, not checked on every request

- Status: accepted
- Date: 2026-11-15
- Phase: 1 (`v0.2.0`)

## Context

The Phase 1 matrix asserted, on line 11: *"revoked user token → `401` and a re-exchange does not happen."*
The second half held. The first did not, and the measurement (`docs/LOCAL-MATRIX.md`) showed why:

| Case | HTTP | What happened |
|---|---|---|
| revoked before the first call | `200` | the IdP refused the exchange (`invalid_grant`); the tool returned `ok=false`, code `obo_exchange_rejected`; 0 tokens issued |
| revoked after one successful call | `200` | answered from the **cached** exchanged token; 0 requests reached the IdP |
| revoked, then a tool needing no exchange | `200` | answered normally; 0 requests reached the IdP |

The server validates tokens **offline against the JWKS**. It never asks the IdP whether a token is still
alive, so revocation is noticed only when the server has to talk to the IdP anyway. This is not a bug in
a branch: it is what offline validation means, and Entra behaves the same way — it has no access-token
revocation endpoint, and relies on short lifetimes plus Continuous Access Evaluation.

## Decision

**Revocation is enforced at the IdP and at the boundaries the server already crosses.** Concretely, the
guarantees the server actually provides are:

1. A revoked token **cannot be exchanged** — the IdP refuses, the tool returns
   `obo_exchange_rejected`, and nothing downstream is called.
2. An exchanged token **cannot outlive its subject**, and the cached copy expires within its own
   lifetime (300 s here; `src/flightops_mcp/auth/obo.py` states the bound).
3. Everything else about a token (`exp`, `iss`, `aud`, scope) is checked on every request, offline.

The acceptance line is reworded to what is true:

> **A revoked token cannot be exchanged, and a token cached by the exchange expires within its own
> lifetime. Revocation is not noticed by tools that need no exchange until the token expires.**

**Deployment guidance, not code:** access-token lifetime is the revocation window. The IdP fixture issues
3,600 s tokens because that makes the cache behaviour observable; a real deployment should issue 5–15
minute access tokens, which is also what makes CAE meaningful.

## Options considered and rejected

- **RFC 7662 introspection on every request.** Gives immediate revocation, at the cost of a network hop
  and a hard availability dependency on the IdP for *every* tool call — the opposite of the resilience
  property the JWKS cache was built for (stale keys served through an issuer outage). It also is not how
  Entra treats access tokens, so it would test a product that does not exist.
- **Introspection with a 60-second cache.** A middle option: bounded staleness for one extra call per
  minute per token. Rejected for now because it adds state and a second failure mode for a window that
  a 15-minute token already makes small.
- **Shortening the fixture's token lifetime to hide the window.** Rejected: it would make the test pass
  by changing the experiment.

## Consequences

- The revocation window equals the access-token lifetime. With the fixture that is up to 3,600 s; with a
  sane deployment it is whatever the IdP issues.
- A user whose token is revoked keeps access to tools that need no exchange until `exp`, then fails.
- `tests/test_obo.py::test_a_revoked_or_expired_user_token_cannot_be_exchanged` asserts the payload, not
  a status code, and that assertion is correct rather than convenient — it pins guarantee 1.
- **Revisit if** a customer requires revocation on a timescale shorter than the token lifetime, or a real
  tenant issues tokens longer than 15 minutes. Either would justify introspection with a short cache.
