# The airport board

A viewer for the MCP server's tools. It calls the same tool functions an agent calls, on one port, with short
polls. There is no build step, and every asset is in `dashboard/static/`, so it runs from a clone with no
network beyond the upstreams themselves.

```bash
uv sync
uv run python -m dashboard                      # http://127.0.0.1:8765
uv run python -m dashboard --record             # also save every tool response
uv run python -m dashboard --replay FILE        # serve a recording, no upstream calls
```

The reference database is built from `data/airports_seed.csv` on first start if it is missing.

## Settings

All are environment variables, or lines in `.env`. Changing one needs a restart and nothing else.

| Variable | Default | What it does |
|---|---|---|
| `FLIGHTOPS_OPENSKY_CLIENT_ID`, `_SECRET` | unset | OpenSky credentials. With both set the mode is `client_credentials`. |
| `FLIGHTOPS_OPENSKY_ANONYMOUS` | `false` | Opt in to OpenSky's anonymous tier. Ignored once credentials are set. |
| `FLIGHTOPS_DASHBOARD_PASSCODE` | unset | Required to serve anything but localhost. |
| `FLIGHTOPS_DASHBOARD_DAILY_CREDIT_BUDGET` | `1000` | Rolling 24 h ceiling on board and `airspace_snapshot` spend. |
| `FLIGHTOPS_DASHBOARD_RECORDINGS_DIR` | `~/.flightops/recordings` | Where `--record` writes. Refused inside the repository. |

## OpenSky access modes

| `credentials` | How it is reached | Traffic | Movements |
|---|---|---|---|
| `client_credentials` | Bearer token | 5 s resolution | Fetched |
| `anonymous` | No token, opt-in | 10 s resolution, most recent vectors only, `time` ignored; stated in `freshness` and `unknowns` | Not fetched, and triage stays `unknown` |
| `none` | Nothing is sent | `ok: false`, `credentials_missing` | Reported as unavailable |

The mode appears in `upstream_status`, in the `opensky_mode` startup log line and in every traffic payload.

## What the board promises

- Unknown is a hollow hatched grey marker with the word "Unknown". It is not a colour on the weather scale.
- A marker describes weather, never the airport's status. Each category also has its own shape.
- Zero aircraft reads "none heard, coverage not proven". Zero movements reads "not available yet today".
- The banner (`LIVE`, `STALE`, `RECORDED <time>`, `NO DATA`) is computed from the payloads.
- Aircraft stay where they were last reported and fade with age, measured from the moment of the answer.
  They are never animated between polls.

These decisions are made in `dashboard/board.py`, so they are tested in Python.

## Exposure

Set a passcode before putting the board behind a tunnel: whoever reaches it can spend credits. Without one it
binds to `127.0.0.1` and refuses any other `Host`, so a tunnel pointed at it gets a 403. Sessions are signed
with a key generated at startup, so a restart signs everyone out. Ten failed sign-ins in a minute lock the
form for everyone until the window passes.

The daily ledger lives in memory and **does not survive a restart**. OpenSky's own count does, which is why the
strip shows the last `X-Rate-Limit-Remaining` next to it.

## Measured against OpenSky

Anonymous calls through this client on 2026-11-18. Each figure is one observation of the change in
`X-Rate-Limit-Remaining`.

| Call | Credits debited | Note |
|---|---|---|
| `/states/all`, 40 nm box | 1 | matches the tier table |
| `/states/all`, whole world | 4 | matches the tier table; 6,123 aircraft |
| `/flights/arrival`, EDDF, last hour | 18 | `404`, no flights listed |
| `/flights/departure`, EDDF, last hour | 30 | 27 flights |
| `/flights/*`, EDDF, a window one day back | not reported | `403` for anonymous callers |

Two things follow. A busy airport can list no arrivals for the last hour, so an empty list is shown as "not
available yet today". And flights cost far more than the 4 credits this client charges for them, at least
anonymously, so anonymous mode does not call them. **Whether authenticated calls are debited the same has not
been measured.** Compare the strip's "OpenSky credits left" before and after a triage once credentials exist.

The board judges freshness against this machine's clock. A clock that runs fast makes current data read as old,
which the board reports as a snapshot that is not current.
