# Phase 1 acceptance matrix, proven without Docker

`docker: command not found` on this host, so the sixth Phase 1 criterion (the whole matrix runs in
`docker compose up`) could not be checked. This page records the same matrix run against the same
three services as local processes. It is evidence for the auth behaviour. It is not evidence that the
containers work; see [what this does not prove](#what-this-does-not-prove).

Result: **12 of 12 checks pass; line 11 was reworded rather than adjusted** — the measurement showed the
line asserted something the server cannot promise, and the decision is recorded in
[ADR 0002](adr/0002-token-revocation-is-enforced-at-the-idp.md). The first run (below) failed the original
wording; the script was then changed to assert the reworded guarantee and re-run: 12 of 12, local
processes, 2026-11-15.

The same matrix is wired to run against `docker compose` in CI; see
[the compose path](#the-compose-path-ci-job-compose-matrix). That job has not been executed yet.

## Run it

Needs `uv`, `openssl` and a POSIX shell (git-bash is enough). Nothing is installed globally and no
external account is used.

```bash
uv sync

# Once. .env is gitignored. The secrets are never printed, logged or committed.
echo "DEV_AGENT_CLIENT_SECRET=$(openssl rand -hex 24)" >> .env
echo "DEV_MCP_CLIENT_SECRET=$(openssl rand -hex 24)" >> .env

set -a; . ./.env; set +a
bash scripts/local_matrix.sh
```

`scripts/local_matrix.sh` starts these four processes, runs `scripts/local_matrix.py` against them and
stops all four on exit. The commands and environment are the ones in `docker-compose.yml` with the host
name changed from `idp`/`ops-api`/`localhost` to `127.0.0.1`:

| Process | Command | Port | Environment |
|---|---|---|---|
| idp | `uv run uvicorn --factory services.idp.app:create_app --host 127.0.0.1 --port 9000` | 9000 | `IDP_ISSUER=http://127.0.0.1:9000`, `IDP_CLIENTS` (JSON: `flightops-agent` with app roles on the server URL, `flightops-mcp` with `resource` = server URL and `exchange_targets` for `api://flightops-ops`; secrets from the two variables above) |
| other idp | same command, `--port 9001` | 9001 | `IDP_ISSUER=http://127.0.0.1:9001`, no clients. A second issuer with its own signing key, for line 3 |
| ops-api | `uv run uvicorn --factory services.ops_api.app:create_app --host 127.0.0.1 --port 9100` | 9100 | `OPS_ISSUER=http://127.0.0.1:9000`, `OPS_JWKS_URL=http://127.0.0.1:9000/.well-known/jwks.json`, `OPS_AUDIENCE=api://flightops-ops` |
| server | `uv run flightops-mcp --transport streamable-http --host 127.0.0.1 --port 8001` | 8001 | `FLIGHTOPS_AUTH_ISSUER=http://127.0.0.1:9000`, `FLIGHTOPS_AUTH_JWKS_URL=http://127.0.0.1:9000/.well-known/jwks.json`, `FLIGHTOPS_RESOURCE_SERVER_URL=http://127.0.0.1:8001/mcp`, `FLIGHTOPS_AUTH_TOKEN_URL=http://127.0.0.1:9000/token`, `FLIGHTOPS_AUTH_CLIENT_ID=flightops-mcp`, `FLIGHTOPS_AUTH_CLIENT_SECRET` (from `DEV_MCP_CLIENT_SECRET`), `FLIGHTOPS_OPS_API_BASE_URL=http://127.0.0.1:9100`, `FLIGHTOPS_OPS_API_AUDIENCE=api://flightops-ops` |

Deviations from the brief, and why:

- **The server ran on 8001, not 8000.** Another process (PID 7180, not started by this work and not
  inspected) was already listening on 8000 with live connections. It was left alone. The server URL is
  the token audience, so it is `http://127.0.0.1:8001/mcp` in both the server and the IdP config. The
  logic under test does not depend on the number. Set `MATRIX_SERVER_PORT=8000` on a clear host.
- **A fourth process on 9001** is the second issuer for line 3.
- The ports are bound to `127.0.0.1` only.

All four processes were stopped afterwards; ports 8001, 9000, 9001 and 9100 were confirmed free with
`netstat`. The listener on 8000 is not ours and was not touched.

## Results

Each line is an observed result from a real socket. Lines 4, 8, 9, 10 and 11 read the services' own
logs, not the client's view. Tokens and secrets are not shown here, and line 12 checks that none of the
16 tokens minted during the run appears in either service log.

| # | Check | Result | Observed |
|---|---|---|---|
| 1 | no token | **PASS** `401` | `WWW-Authenticate: Bearer error="invalid_token", ..., resource_metadata=".../.well-known/oauth-protected-resource/mcp"`; no tool output in the body |
| 2 | expired token | **PASS** `401` | correctly signed token whose `exp` is 60 s before `iat`; server logged `token_rejected` reason `expired` |
| 3 | token from a different issuer | **PASS** `401` | token `iss=http://127.0.0.1:9001`, trusted issuer is `:9000`; server logged reason `unknown_key_id` (see note) |
| 4 | `aud` is not this server | **PASS** `401` | token for `api://flightops-ops`; server logged `wrong_audience` with `aud` and `expected`; the token string is not in the server log |
| 5 | valid token, insufficient scope | **PASS** `403` | token has `flightops.reference.read`, `airport_weather` needs `flightops.weather.read`; header `Bearer error="insufficient_scope", scope="flightops.weather.read", resource_metadata=...`; server logged `scope_challenge` for `airport_weather` |
| 6 | valid token, sufficient scope | **PASS** `200` | `resolve_airport('ESGG')` returned `ok=true`, Göteborg Landvetter, for a user token and for a `client_credentials` service principal |
| 7 | exchange narrows scope | **PASS** `200` | subject `scp` = `passengers.read weather.read reference.read`; exchanged `scp` = `flightops.passengers.read`, `aud=api://flightops-ops`, `act.sub=flightops-mcp`, 300 s lifetime against 3600 s |
| 8 | exchange escalation | **PASS** `400` ×3 | asking for `weather.read` the target does not allow: `invalid_scope`; asking for `passengers.read` with a token that lacks it: `invalid_scope`; an audience the client may not target: `invalid_target`. IdP access log across the three: 0 issued, 3 refused |
| 9 | passthrough attempt | **PASS** `401` ×3 | a token for the ops API presented to the server, and the exchanged token replayed at the server: both `401`, both logged `wrong_audience`. A server-audience token carried straight to the ops API: `401`. Control: the exchanged token at the ops API: `200` |
| 10 | 50 concurrent calls, one identity | **PASS** | 50 of 50 answered `200`. **IdP access log: 1 `POST /token` in the burst, 1 issued**, still 1 after a 51st call. Server log agrees (1 `obo_exchanged`) |
| 11 | revoked token cannot be exchanged (reworded, ADR 0002) | **PASS** | first run against the original wording: **FAIL**, see below. Re-run against the reworded line: `obo_exchange_rejected`, 0 issued, 1 refused by the IdP |
| 12 | extra: tokens in logs | **PASS** | 16 tokens checked, 0 found in either log |

Line 3 note: the second IdP signs with a different key, so the server rejects the token at the
signing-key lookup (`unknown_key_id`) before it reaches the issuer comparison. That is a correct `401`
for a token from another issuer, but the `wrong_issuer` branch is not what fired here. It is covered by
`tests/test_auth_verifier.py` only, because a live token with the right key and the wrong `iss` cannot be
made without the IdP's private key, which exists only in its process memory.

### Line 11: a revoked token is not a 401 — reworded, see ADR 0002

**Resolution 2026-11-15:** the line asserted something an offline-validating resource server cannot
promise. The behaviour was not changed and the criterion was not quietly relaxed — it was reworded to the
guarantee that exists, and the reasoning, the options rejected and the revisit trigger are in
[ADR 0002](adr/0002-token-revocation-is-enforced-at-the-idp.md). In one line:

> A revoked token cannot be exchanged, and a token cached by the exchange expires within its own
> lifetime. Revocation is not noticed by tools that need no exchange until the token expires.

Brief: *revoked user token → 401 and a re-exchange does not happen.* Observed:

| Case | HTTP | Result |
|---|---|---|
| (a) token revoked before its first call | `200` | tool returned `ok=false`, code `obo_exchange_rejected`. The IdP refused the exchange (`invalid_grant`): 0 tokens issued, 1 refused. **No re-exchange happened.** |
| (b) token revoked after one successful call | `200` | tool answered `ok=true` from the cached exchanged token. 0 requests reached the IdP. |
| (c) token revoked, then a tool that needs no exchange | `200` | `resolve_airport` answered `ok=true`. 0 requests reached the IdP. |

So the second half holds (nothing is re-issued for a revoked token) and the first half does not. The
cause is the design, not a bug in a branch: the server verifies tokens offline against the JWKS and never
asks the IdP whether a token was revoked, so revocation is only noticed when the server has to exchange.
The consequences:

- A revoked token keeps working on every tool that needs no exchange, until its own `exp` (the IdP's
  access token lifetime, 3600 s here).
- On `flight_passenger_impact`, a user whose exchanged token is already cached keeps access for up to
  that token's remaining life (300 s minus the expiry skew). `src/flightops_mcp/auth/obo.py` states this
  bound; it is accepted there.
- A user whose token is revoked before the first exchange gets a failure payload with HTTP `200`, not
  `401`. That matches `tests/test_obo.py::test_a_revoked_or_expired_user_token_cannot_be_exchanged`,
  which asserts the payload and not a status code.

**Decided 2026-11-15:** accept the bound and reword the line, rather than change the behaviour or shorten
the fixture's token lifetime to make the test pass. The full reasoning, the rejected alternatives
(introspection per request, introspection with a 60 s cache) and the revisit trigger are in
[ADR 0002](adr/0002-token-revocation-is-enforced-at-the-idp.md). None of the five roadmap boxes depend on
this line.

## What this does not prove

This is the residual risk for the sixth criterion. None of it was exercised.

- **The container images build.** `services/Dockerfile` and the root `Dockerfile` were never built: the
  `uv sync --frozen --group services` step, the uv image tag, the non-root user, the `ENTRYPOINT`/`CMD`
  split and `.dockerignore` are all unverified. The matrix ran from the working tree, not from images.
- **Compose networking by service name.** Inside compose the issuer is `http://idp:9000` and the server
  audience is `http://localhost:8000/mcp`. Here every URL was `127.0.0.1`. That the names resolve, that
  `iss` equals `FLIGHTOPS_AUTH_ISSUER` under those names, and that the published ports reach the right
  container are untested. The ops API is meant to be unpublished; that was not checked either.
- **Healthcheck gating.** `depends_on: condition: service_healthy` and the three `healthcheck` blocks
  never ran. This run waited for readiness with its own polling loop.
- **Hardening.** `read_only: true`, `tmpfs: /tmp` and `no-new-privileges` were not applied. Whether the
  services start on a read-only root filesystem, for instance whether anything writes outside `/tmp`, is
  unknown. So is the non-root user's access to `/app`.
- **The outbound passthrough guard, live.** Nothing in the running server forwards the caller's token, so
  the `refuse_inbound_token` hook cannot be triggered without changing the code. Line 9 shows the two
  things reachable from outside: another service's token is refused at the front door, and a
  server-audience token is refused by the ops API. The hook itself is asserted by
  `tests/test_no_passthrough.py` (including the logged reason) and is not seen firing here.
- **More than one identity.** Line 10 is one user. Per-identity separation is covered by
  `tests/test_obo.py::test_fifty_concurrent_calls_make_exactly_one_exchange_per_identity`, not by this run.
- **A real identity provider.** The IdP is the repo's test fixture; `docs/adr/0001` lists where it
  differs from Entra. The matrix says nothing about Entra behaviour.
- **Outside the host.** Everything ran on one machine over loopback, with TLS off.

The criterion stays unticked in `ROADMAP.md` until `docker compose up` has been run somewhere that has
Docker, with this matrix pointed at it. CI is the natural place, and the next section is that job.

## The compose path: CI job `compose-matrix`

**Status: written, not executed.** The job is `compose-matrix` ("compose matrix (phase 1 acceptance)") in
`.github/workflows/ci.yml`. It has **not been run on the development host** (no Docker here), and no CI run
of it has been seen. Nothing on this page claims it is green. Neither the job's YAML nor
`docker-compose.ci.yml` could be validated with `docker compose config` here; the first run is that
validation.

What it does, in order: generates the two client secrets into `.env` (masked in the log, never printed),
checks that `docker-compose.yml` alone still publishes no port for `ops-api`, validates the merged config,
runs `docker compose up --build --wait`, runs this matrix, prints every service's logs if anything failed,
and always runs `docker compose down -v`. A failing line fails the job; the 12 lines go to the job summary.

How it closes the gaps between this page's local run and compose:

| Gap | How the job handles it |
|---|---|
| `ops-api` is not published, but line 9 calls it directly | `docker-compose.ci.yml` publishes it on `127.0.0.1:9100`. Only the override does; `docker-compose.yml` is untouched |
| No second issuer for line 3 | `idp-other` in the override: `http://idp-other:9001`, its own per-process signing key, no clients |
| The matrix counted exchanges from a log file | `MATRIX_IDP_LOG_CMD` / `MATRIX_SERVER_LOG_CMD` run `docker compose logs --no-color --no-log-prefix <service>`. The count is still read from the service, not the client. A failing or empty log command stops the run instead of reading as empty, which would make lines 4, 9 and 12 pass vacuously |
| The audience is `http://localhost:8000/mcp` | `MATRIX_SERVER=http://localhost:8000` (no `/mcp`, which the script adds, and `localhost`, not `127.0.0.1`, so minted tokens carry the audience compose configures) |

Things the job does not and cannot prove, even when green:

- **Line 3 still hits `unknown_key_id`**, not the `wrong_issuer` branch, for the reason in the note above.
- **The outbound passthrough guard is still not seen firing**; `tests/test_no_passthrough.py` covers it.
- **Compose log timing.** The counting lines sleep 0.3-0.5 s and then read `docker compose logs`. Whether
  the json-file log driver keeps up under 50 concurrent calls is untested; a flaky line 10 would mean that.
- **Healthchecks gate `idp`, `idp-other` and `ops-api` only.** `server` has none in `docker-compose.yml`, so
  `--wait` accepts it as soon as it is running and the matrix's own readiness poll covers the rest.
- **Hardening is exercised, not asserted.** `read_only` and `no-new-privileges` are on, so a service that
  writes outside `/tmp` would fail to start; the job has no step that checks the settings themselves.
- It is still one runner over loopback, with TLS off, against the repo's test IdP and not Entra.
