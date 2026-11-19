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

- Unknown is a hollow dashed ring with the word "Unknown". It is not a colour on the weather scale.
- A marker describes weather, never the airport's status. Each category also has its own shape.
- Zero aircraft reads "none heard, coverage not proven". Zero movements reads "not available yet today".
- The banner (`LIVE`, `STALE`, `RECORDED <time>`, `NO DATA`) is computed from the payloads.
- Aircraft stay where they were last reported and fade with age, measured from the moment of the answer.
  They are never animated between polls.

These decisions are made in `dashboard/board.py`, so they are tested in Python.

## Identity

The board uses the radar scope from `docs/design/palette-samples/b-radar.html`. All tokens are in one commented
block at the top of `dashboard/static/board.css`, and the canvas reads the same block.

| Token | Value | What it means |
|---|---|---|
| `--void` | `#070C11` | Page, open water, text halo |
| `--panel` | `#0A1117` | Side panels, governance strip, badge, label plate |
| `--raised` | `#101B23` | METAR block, hover, tooltip |
| `--land` | `#0C181F` | Land, the lightest map surface |
| `--amber-tint` | `#1E1B14` | Selected airport's row |
| `--grid` | `#16242E` | Graticule and row dividers |
| `--line` | `#1E313D` | Panel borders |
| `--control` | `#35505F` | Control borders |
| `--ring` | `#21404E` | Range rings |
| `--tick` | `#3E6172` | Bearing ring ticks |
| `--coast` | `#2F8F70` | Coastline and the globe's limb |
| `--type` | `#DCE7EC` | Primary text, aircraft |
| `--muted` | `#8FA5B0` | Labels and secondary text |
| `--phosphor` | `#46E0A8` | Live banner, and VFR |
| `--amber` | `#E8A133` | Selection, focus and warnings. Never a weather category |
| `--vfr` | `#46E0A8` | VFR, a circle |
| `--mvfr` | `#4FA8FF` | MVFR, a square. The sample's derived blue |
| `--ifr` | `#FF5C6C` | IFR, a diamond |
| `--lifr` | `#E9E2FF` | LIFR, a triangle. The sample's derived pale violet |
| `--unknown` | `#DCE7EC` | Unknown, a hollow dashed ring in the text colour |

Contrast of each marker against the two surfaces it sits on. The floor is 6:1, and a test enforces it.

| Marker | On `--void` | On `--land` |
|---|---|---|
| VFR | 11.67 | 10.71 |
| MVFR | 7.83 | 7.18 |
| IFR | 6.54 | 6.00 |
| LIFR | 15.69 | 14.40 |
| Unknown | 15.60 | 14.31 |

The sample's land, `#0D1820`, gave IFR 5.99:1. Land moved to `#0C181F` because IFR is the fixed colour.

What differs from the sample, on purpose:

- **Sans for names and sentences, mono for machine facts.** Airport names, panel sentences and buttons use the system
  sans. ICAO codes, METAR strings, timestamps, the banner, the badge and the governance figures use the system mono.
  An all-mono list truncates airport names the most, and names are read while codes are matched.
- **System fonts only.** The sans chain starts at Segoe UI and the mono chain at Cascadia Mono, and each ends in a
  generic family. The board downloads no font.
- **The whole globe stays in view,** because here it can be turned. The sample crops the south for one fixed picture.
- **Aircraft are white, not phosphor,** because green means VFR on this board.

Range rings are drawn every 600 NM around the selected airport, on the globe only. The bearing ring reads true
bearings from the centre of the view, which is the selected airport until the globe is dragged.

### The last-update badge

The badge sits at the top right of the map, moves to the top left when the selected airport or its label would be
under it, and becomes a strip above the map at phone width. `last_update` in `dashboard/board.py` decides every word.
The page only displays them, and it does no date arithmetic.

| Situation | Line |
|---|---|
| Before the first answer, or no report has a time | `Weather no data yet` |
| Newest METAR within 90 min | `Weather Last update 4 min ago` |
| Newest METAR older than 90 min | `Weather stale: last update 2 h 5 min ago` |
| Traffic layer on, snapshot within 5 min | `Traffic Last update under 1 min ago` |
| Traffic layer on, snapshot older than 5 min | `Traffic stale: last update 21 min ago` |
| A report or snapshot dated beyond the limit ahead of the clock | `stale: dated in the future` |
| Traffic layer on, no usable snapshot time | `Traffic snapshot time unknown` |
| The upstream's answer carried no `Date` header | `Weather freshness unknown — no upstream time`, or the same for `Traffic` |
| Replaying a recording | `Recorded 2026-11-14 23:30 UTC`, and `Traffic snapshot 2026-11-14 23:29 UTC` |
| The board server does not answer | `Board no answer from the server` |

- It measures the age of the data, not the time since the page last polled. The weather line is the freshest METAR
  among the listed airports, so one recent report does not show that the others are recent.
- Weather and traffic always get separate lines, so neither age can stand in for the other.
- The limits are the tools' own, `METAR_STALE_AFTER_S` and `STATES_STALE_AFTER_S`. Exactly at the limit is still
  current. A stale badge turns amber and also says `stale`, so colour is not the only signal.
- `freshness unknown — no upstream time` is neither live nor stale: a dotted border and an italic line, in the text
  colour. It never shows a number, because the only number available would come from this machine's clock.
- The page sends the traffic layer's snapshot time and OpenSky's `Date` back to `/api/board`, exactly as the server
  gave them, and the server works out the age from those two. This costs no credits.
- It says nothing about the aircraft drawn around a selected airport. Those fade by their own age.
- Ages change when a poll lands. Nothing counts up between polls.

### Accessibility, measured

On 2026-11-18, axe-core 4.14.1 in Microsoft Edge ran against the real app, at 1440 x 900 and 390 x 844, over map,
globe, airport selected, no data, traffic on, stale and recorded. It found 0 violations of any impact. No state
scrolled sideways at 390 px, and the badge did not touch the legend or, on a phone, the map. The lowest text contrast
axe measured was 6.69:1, `--muted` on `--amber-tint`. The colour floors are: `tests/test_dashboard.py` holds
markers at 6:1 and text at 4.5:1 on every surface.

The CI job `a11y` now enforces this. `node scripts/a11y.mjs` (setup in its header) starts the real app over
synthetic tool answers, signs in, and walks map, globe, airport selected and no data yet at both sizes, plus the
sign-in page. It fails on a `serious` or `critical` axe-core violation, on sideways scroll or an element wider than
the viewport at 390 px, on a page error, and on any request that leaves the machine. axe-core and Playwright are
pinned in `scripts/package-lock.json`. A third fixture answers with no upstream time, so the walk also covers `freshness unknown — no upstream time` at
both sizes. The traffic, stale and recorded states above are not part of the walk.

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

## Which clock judges freshness

Every age is measured against the upstream's own clock: the `Date` header of the HTTP response that carried the data,
which the tools report as `upstream_time_epoch`, to the second. A METAR's age is NOAA's `Date` minus its observation
time. A traffic snapshot's age is OpenSky's `Date` minus the snapshot's `time`. Aircraft fade by OpenSky's `Date`
minus their last contact.

When a response carries no `Date`, the tool says so in `unknowns`, reports no age and does not call the data current,
and the badge reads `freshness unknown — no upstream time`. Nothing falls back to this machine's clock.

This machine's clock is not trusted because it was measured 21 min 27 s fast on 2026-11-19 and cannot be resynced
from a non-administrator account (`w32tm` reports a free-running system clock). Against it, every METAR read 21
minutes older than it was and every traffic snapshot read stale. `tests/test_dashboard.py` runs the real tools and
the board with the host clock 21 min 27 s fast and asserts each verdict is the one a correct clock gives.

Two things still read the local clock, on purpose. The windows sent upstream (`airport_movements`' look-back,
`schiphol_flights`' look-ahead) are request parameters, chosen before any upstream has answered. The credit ledgers,
the `429` waits and token expiry measure this machine's own behaviour.

A recording keeps each answer's `upstream_time_epoch`, so playback ages are the upstream's too. The time a recording
shows in the badge, `created_at` and each entry's `t`, is the recording machine's clock: when the data arrived
there, which on a skewed machine is off by the skew.
