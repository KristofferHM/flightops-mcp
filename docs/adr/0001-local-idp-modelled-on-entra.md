# ADR 0001: A local IdP modelled on Entra ID, and where it differs

- Status: accepted
- Date: 2026-11-15
- Phase: 1 (`v0.2.0`)

## Context

The resource-server behaviour of this project (JWT validation, scope checks, on-behalf-of) has to be
tested on every commit. Doing that against a real Entra tenant needs an account, network access from
CI, and secrets in the pipeline. It also makes the failure paths, an expired or revoked token at an
exact moment, slow or impossible to produce on demand.

## Decision

`services/idp/` is a small authorization server that runs in process under test and as a container in
`docker compose`. It signs RS256 tokens with a key generated at start-up, publishes that key as a
JWKS, and supports exactly the grants the matrix needs: `client_credentials` and RFC 8693 token
exchange, plus revoke and expire controls.

Token claims follow the Entra v2 access-token shape so that the server is developed against what it
will meet in production:

| Claim | Meaning here | Same as Entra |
|---|---|---|
| `aud` | the one API the token is for | yes |
| `scp` | delegated permissions, space-delimited (user tokens) | yes |
| `roles` | application permissions, a list (`client_credentials` tokens) | yes |
| `tid` | tenant id; a fixed dummy GUID | shape only |
| `azp` | the client the token was issued to | yes |
| `oid`, `sub` | the user; both carry the same value | Entra's `sub` is pairwise per app |
| `idtyp` | `"app"` on application tokens | yes (optional claim in Entra) |

## Where the stub differs from Entra

These are deliberate. Each one is a place where passing against the stub does **not** prove the code
works against Entra.

1. **On-behalf-of uses RFC 8693, not Entra's grant.** Entra's OBO flow is
   `grant_type=urn:ietf:params:oauth:grant-type:jwt-bearer` with `assertion` and
   `requested_token_use=on_behalf_of`. The stub implements
   `urn:ietf:params:oauth:grant-type:token-exchange` with `subject_token`, as the roadmap specifies.
   Moving to Entra means changing the request built in the exchange client; the cache, single-flight
   and failure handling around it do not change.
2. **Exchange narrows by subset.** The stub issues a scope only if the subject token already carries
   it and the target accepts it, so an exchanged token is provably no wider than its source. Entra
   decides by admin or user consent for the downstream API, and the downstream scopes live in a
   different namespace from the inbound ones. The subset rule is stricter and is what the
   escalation test asserts.
3. **Exchanged tokens carry `act`.** The stub adds RFC 8693's actor claim and the mock ops API
   requires it. Entra's OBO tokens have no `act`; a real downstream would look at `azp` instead.
4. **The exchanged token cannot outlive its subject.** The stub caps `exp` at the subject token's
   `exp`. Entra issues a full-lifetime token.
5. **Audiences.** The MCP server's audience is its resource URL (RFC 8707, as the MCP
   authorization spec requires). Entra audiences are an application ID URI (`api://...`) or a client
   id GUID unless the app registration is configured otherwise.
6. **Revocation.** The stub has an RFC 7009 endpoint and a `/dev/expire` control. Entra has no
   access-token revocation endpoint; it relies on short lifetimes and Continuous Access Evaluation.
   Revocation here is visible to the IdP itself (a revoked token can no longer be exchanged). A
   resource server validating offline against the JWKS does not see it, in the stub or in Entra.
7. **No interactive flows.** There is no authorization-code flow, PKCE, consent, refresh token or
   dynamic client registration. `/dev/user-token` mints a delegated token for any `sub` on request,
   with no authentication. That endpoint is why this service must never be reachable beyond
   localhost or a compose network.
8. **Keys.** One signing key per process, never rotated, never persisted. Restarting the IdP
   invalidates every token. Rotation is tested at the JWKS cache with a mock endpoint instead.
9. **Issuer.** A plain URL. Entra's is `https://login.microsoftonline.com/{tid}/v2.0` and
   multi-tenant apps must validate `tid` against an allow-list; this project is single-tenant and
   does not.

## Consequences

- The whole auth matrix runs offline, in under ten seconds, with no secret in the repository: client
  secrets are generated per test run and passed by environment in compose.
- A separate, manual pass against a real tenant is still needed before claiming Entra support. The
  list above is the checklist for that pass.
- The stub is ~500 lines that have to be maintained. It is kept out of the server package and out of
  the server image.
