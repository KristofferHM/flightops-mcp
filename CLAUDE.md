# Working notes for AI coding agents

Brief context for Claude Code (or any agent) working in this repository. Keep this file short: it is
read on every session and costs context.

## What this project is

A governed MCP server exposing airport-operations tools to AI agents. The interesting part is the
governance layer — OAuth2 token lifecycle, credit-metered rate limiting, PII-safe logging — not the
tools themselves.

## Commands

```bash
uv sync                                                              # install
uv run python scripts/build_airports_db.py --csv data/airports_world.csv  # reference data (3,270)
uv run pytest                                                        # suite (1752 tests, must stay 0 skipped)
uv run ruff check . && uv run ruff format --check .                   # lint
uv run mypy src services agent evals dashboard                       # strict types
uv run flightops-mcp                                                 # stdio server
```

## Rules that are not negotiable

1. **Run it, do not describe it.** A change is finished when `uv run pytest` is green and the public
   entry point has been executed. Two of the defects in this repo were found only by running against a
   live upstream.
2. **No skipped tests.** CI fails on `skipped`. If a test cannot run, replace the dependency, do not
   skip it.
3. **Failures return data.** Tools never raise at the caller: expected failures come back as
   `{"ok": false, "error": {"code", "message", "hint"}}` so the model can self-correct.
4. **Never invent a data result.** If an upstream returns nothing, the payload says `unknowns` — empty
   output is not evidence of normality.
5. **No secrets in code, tests, fixtures or commit messages.** Redaction is tested with canaries in
   `tests/test_redaction.py`; new log fields with PII need a canary too.
6. **Every tool has a scope.** A new tool needs an entry in `auth/scopes.py` or registration fails.
   A composite tool lists everything its parts need, because it calls them directly.
7. **Never forward the caller's token.** Every `httpx.AsyncClient` takes
   `event_hooks={"request": [refuse_inbound_token]}`; a test scans the source for one that does not.
8. **Pin the interpreter.** Bare `python` on a developer machine may be older than the 3.12 this
   project requires; always go through `uv run`.

## Where things live

| Path | Purpose |
|---|---|
| `src/flightops_mcp/server.py` | MCP tools, the `guarded` wrapper, triage logic |
| `src/flightops_mcp/auth/` | inbound auth: JWKS cache, RS256 verifier, `Scopes.for_tool`, the 403 scope challenge, the passthrough guard; the only code that touches the SDK auth API |
| `src/flightops_mcp/opensky_token.py` | OAuth2 client credentials, single-flight refresh |
| `src/flightops_mcp/opensky.py` | `429` policy, response decoding (`decode_states`, `decode_movements`) |
| `src/flightops_mcp/credits.py` | the per-hour credit ledger; mutation-tested (`docs/QUALITY.md`) |
| `src/flightops_mcp/http.py` | retry/backoff/`Retry-After` plumbing |
| `src/flightops_mcp/telemetry.py` | redaction pipeline, Prometheus metrics |
| `services/idp/` | local IdP for tests and compose; a fixture, never deployed. Differences from Entra: `docs/adr/0001` |
| `src/flightops_mcp/{annotations,schemas,surface,progress}.py` | protocol surface: tool annotations, output schemas, resources/prompts/completions, progress (`docs/PROTOCOL.md`) |
| `dashboard/` | the airport board; display rules live in `board.py` (`docs/DASHBOARD.md`) |
| `ROADMAP.md` | phased plan to `v1.0`; check the current phase before starting work |

## Style

Short declarative sentences in comments, explaining *why* a non-obvious decision was made. No comment
that restates the code. Prefer stdlib, then one well-known dependency, then write it yourself.
