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

The reference database is built from `data/airports_world.csv` on first start if it is missing: 3,270 large and
medium airports with scheduled service, worldwide (`docs/REFERENCE-DATA.md`). A database built before continents
existed is refused with the command that rebuilds it; the board never overwrites one it did not make.

## Regions and the list

The region control offers Europe (508), North America (851), South America (274), Africa (342), Asia (1,054),
Oceania (241) and World (3,270), each with the number of airports it will draw. The choice is remembered in the
browser. The map draws the airports nearest the centre of the view (below); the list beside it holds the whole
region, 25 at a time, searchable by code, name or city. The server filters and pages the list (`/api/airports?region=&q=&offset=&limit=`), so a phone never
holds more than one page of rows.

Weather arrives through `weather_overview`, a hundred airports per call, at most four calls at a time, cached for
60 s, and only for the airports in view, at most 600 (next section). The client asks NOAA 50 airports per request,
so a poll is 12 requests at most, where a World poll used to be 66. An airport
NOAA did not answer for is Unknown with its own reason; the others in its request keep their reports. Why 50, and
what NOAA does with more, is under "Weather requests, measured".

Under the region control, in so many words: **the airport data is worldwide; live traffic is not.** OpenSky's
receivers are dense over Europe and North America and sparse over Africa, South America and much of Asia, so an
empty patch of sky means no receiver heard anything there, not that no aircraft are flying.

## Reading the board

The Documentation tab holds a glossary of every abbreviation on the board, and the legend carries a one-line
version of it, so the map can be read without leaving it.

| Term | Meaning | Limits | Marker |
|---|---|---|---|
| VFR | Visual Flight Rules | ceiling over 3000 ft and visibility over 5 mi | green circle |
| MVFR | Marginal VFR | 1000-3000 ft or 3-5 mi | blue square |
| IFR | Instrument Flight Rules | 500-1000 ft or 1-3 mi | red diamond |
| LIFR | Low IFR | below 500 ft or under 1 mi | pale violet triangle |
| Unknown | no report heard, or one too old or unreadable | not a fifth category, and not good news | hollow dashed ring |
| ICAO / IATA | four-letter location code / three-letter airport code | | |
| METAR / TAF | the current observation / the forecast | | |
| nm | nautical miles; 1 nm = 1.852 km | | |
| credit | OpenSky's unit of API cost; the strip shows what is left | | |

A colour describes the weather category at that airport, never the airport's status. The tab says that first.

**A neighbour's report, labelled as one.** When NOAA holds no observation for an airport, its line may name the
nearest airport that does have a current one, within 25 nm:

```
No report at ESGT — ESGG (18 nm away) reports VFR, 12 min old
```

The marker stays Unknown, and the banner keeps counting airports with their own report. A neighbour is never used
for severity or the triage, and `airport_weather` for that airport still says no METAR was returned: a tool that
reported a neighbour's weather as the airport's own would mislead an agent. "Current" is the tool's 90-minute rule.
Ties go to the lower ICAO code. Only an airport NOAA answered with nothing gets one, not one whose fetch failed or
whose own report is old. The 25 nm cap is fixed: 5 % of the airports without a report have a reporting airport within
25 nm, 18 % within 50 and 48 % within 100, and at 100 nm it is not that airport's weather.

## Traffic

With OpenSky credentials configured the traffic layer starts on, for the selected region. Without them it starts
off, and the note beside the control says why: anonymous access is opt-in and coarser, and with no access at all
the box is disabled.

Next to `Show traffic — 4 credits per refresh` the control shows what the layer has spent since the board started,
what it has spent in the last hour against its own ceiling (`FLIGHTOPS_DASHBOARD_TRAFFIC_CREDITS_PER_HOUR`, 60 by
default), and the refresh interval that ceiling allows: 60 credits an hour is one refresh every 4 minutes, and never
faster than every 2. When the ceiling is reached the server refuses the refresh without asking OpenSky, auto-refresh
stops, and the badge gets a `Refresh` line saying how much was spent and when the next refresh fits. A refusal by
the tools' own hourly or daily guard, or by OpenSky's `429`, stops it the same way. An upstream failure is not a
refusal: the layer says `Unknown: traffic unavailable (code)` and keeps trying. Turning the layer off and on again
asks once more.

## The triage panel

`Run triage` shows the severity, its reasons (each once, with how many METARs said it), then:

- **Why not higher** and **why not lower**: the measured values below each flag's threshold, the missing legs, or
  the rung the ladder ends at. These come from the tool, word for word.
- **Counts**: arrivals and departures in the window, aircraft heard within the search radius, and how many rows of
  each answer were unreadable. A count that was never made reads `not counted`, never `0`.
- **Weather, as values**: category, the METAR's age by NOAA's clock, ceiling (and what it was read from), visibility
  (with `or more` when NOAA reported a floor), wind with gusts.
- **Each source**: every leg's state, the age of its data by its own upstream's clock, and the endpoint.

## The wide view: what is asked, what is drawn

Measured on the running board at `region=world` before this change: a 900 KB answer in 6.6 s, 3,270 airports asked
about, 1,888 of them (58 %) drawn as grey "Unknown" rings that carried no information. Four rules together, in
`dashboard/board.py`:

1. **Weather is asked for the view, not the region.** The page sends the centre of what it shows and its angular
   radius (`/api/board?lat=&lon=&radius=`). The server asks about the airports within that radius plus 5°, nearest
   the centre first, at most `WEATHER_LOOKUP_CAP` = 600, and says so: `Weather showing 600 of 3,270 nearest the
   centre · 600 lookups in 6 calls`. The view is a circle, not a latitude/longitude box, because a box breaks at the
   antimeridian and over a pole, and the globe's view is a circle anyway. The airports are sent to NOAA in code
   order, so a view held still sends the same hundreds and the tools' 60 s cache answers the repeats. A moved view
   asks again 0.7 s after it stops moving.
2. **An airport without a report arrives trimmed**: code, name and coordinates. Its reason is still in the list and
   in its detail. One exception: an airport with a neighbour's line keeps that line, because it is the only thing
   the map can say about it.
3. **Clusters below zoom 4.** Airports are grouped in 10° cells below zoom 2, 5° below 3 and 2.5° below 4. A group
   is drawn in its most common category's shape (ties to the worse), sized by its count, with the count beside it
   and a small badge in the worst category when that is worse, so one IFR airport is not lost in a cell of VFR. Its
   tooltip reads `◆ 1 IFR · ● 2 VFR`. Selecting a group zooms in on it.
4. **`Show airports without a report`**, off by default below zoom 4 and on from it, until the reader chooses; then
   the choice is kept in the browser. Beside it the count is always shown: `167 hidden — no report heard`, or
   `167 without a report, drawn as dashed rings`. It is a render filter. The page never sends it, no route takes
   it, and two polls of one view are identical (`test_the_unknown_toggle_changes_no_payload_field`).

Airport codes, from zoom 2.5, are placed worst category first and a code that would overlap one already placed is
left out; its marker and tooltip stay.

Measured on the live board, 2026-11-19, World at the page's first view:

| | Before (brief) | After |
|---|---|---|
| Answer | 900 KB | 173 KB, 24 KB gzipped |
| Airports asked | 3,270 | 600 |
| Markers drawn at zoom 1 | 3,270 | 30–56 (10° groups, over three views) |
| Hidden, and said so | none | 167 at the first view |
| Poll, weather cached | 6.6 s | 0.3 s |
| Poll, cold | | 2.1–3.3 s; 20–22 s when NOAA stalls a request past the 15 s timeout (2 of 5 cold polls) |

**The 2-second target is met only when the weather is cached.** A cold poll is twelve NOAA requests at four at a
time, and NOAA's stalls ("Weather requests, measured") are still there. Raising the concurrency would meet it more
often, at the cost of the "not one burst" rule, so it was not changed here. Twenty-degree cells drew 11 to 19 groups
at zoom 1, too coarse to read a continent; 5° drew up to 151.

## Aircraft

With traffic on, the aircraft are drawn on both projections, at the position OpenSky last heard and faded by age.
**Nothing moves between observations**, and an aircraft with no position is not drawn.

- **Map**: an arrow along the true track, a little larger the higher it is; hollow when no altitude was reported; a
  square on the ground; a dot when no track was reported.
- **Globe**: the same marks, each lifted radially by its altitude, `radius × (1 + min(alt, 12 km) / 12 km × 0.035)`,
  with a thin stem down to where it is over the ground. A missing altitude is not lifted and is drawn hollow, so it
  cannot pass for 0. Lifting a point radially is, once projected orthographically, scaling its ground point outward
  from the centre (`aircraftPoint` in `board.js`). It is culled by where it is over the ground, by the same `place`
  test as the airports. Both run in the page each frame; the lift itself is `board.globe_lift`.

What a reader sees on the globe: over a busy region the arrows stand slightly proud of the coastline on short
stems; near the limb the lift shows most, so aircraft over Scandinavia, seen from 20° N, fan visibly outward from
their ground points, while one near the centre of the disc sits almost on its own ground point.

**Choosing what is drawn** (`dashboard/aircraft.py`). Sort first: distance from the selected airport (the default),
altitude (lowest first), callsign, last heard (most recent first), speed (fastest first). An unknown value sorts
after every known one, and ties break by ICAO24, so the same snapshot and choices always draw the same aircraft.
Then filters: within 25–500 nm of the selected airport, airline, altitude band (below 3 km, 3–9 km, above 9 km; an
unknown altitude is in none), airborne or on the ground, callsign or ICAO24 search. Then the cap, 50 to 1,000,
**default 200**. The panel says which control did what, `2 within 100 nm of ESSA · sorted by distance · showing 2`,
and what the cap left out, `showing 200 of 412 — raise the cap or narrow the filter`. Distance without an airport
falls back to last heard, and says so. The layer is held on the server, so changing a choice asks no upstream.

**Focus**: select an aircraft on the map or in the Focus list. The others dim; it is labelled; the positions heard
since the page opened are drawn as dots with their times, never joined; and its transit is asked for (4 credits).
**Follow** keeps it centred, moving only when a new observation lands. It lets go by itself when the aircraft goes
stale (unheard for 120 s) or leaves the snapshot, and says which; Escape, the button or moving the view releases
it. Following is a view setting: it changes nothing that is fetched.

### Observed, estimated, no source

A transponder broadcasts where an aircraft is, not where it is going. The inspector keeps three lines for three
kinds of claim:

| Line | Kind | Source |
|---|---|---|
| Transit | **Observation** | OpenSky `/tracks/all?time=0`: waypoints, each with its time, drawn along great circles and never past the last one |
| Estimated arrival | **Inference** | OpenSky `/flights/aircraft` for the last day: `estArrivalAirport`, from the first and last positions it heard. Labelled "estimated from ADS-B, not a schedule" |
| Destination | **No source** | Needs a schedule provider (Schiphol is stubbed; AeroDataBox or AviationStack for global, FlightAware paid). None is configured |

OpenSky lists a flight **once it has ended**, so an aircraft still flying usually has no record. An estimate is given
only for a record whose first sighting is within 15 minutes of the track's start; any other record is shown as the
last listed flight, not as this one's arrival. The estimate is behind a button because it costs 30 credits.

Measured against OpenSky with client credentials, 2026-11-19, by `X-Rate-Limit-Remaining`:

| Call | Credits | Note |
|---|---|---|
| `/tracks/all?icao24=…&time=0` | 4 per call | its own bucket (4000 → 3996 → 3992 → 3988 while `/states` stood at 3892). 58 waypoints over 49 min for one aircraft |
| `/flights/aircraft`, 12 h or 1 day | 30 per call | the main bucket. A 2-day window was refused. The aircraft's current flight was not listed |

On the live board an aircraft heard 7 s earlier had a track of 2 waypoints ending an hour before. OpenSky documents
`/tracks` as experimental; the inspector prints the span (`22:42Z–22:49Z (7 min)`), and nothing is drawn in the gap.

### Airlines, derived

`dashboard/data/airlines.csv` holds 163 ICAO designators, compiled by hand; source, licence and how to add one are in
`dashboard/data/README.md`. A callsign is an airline's when it is three letters and a flight number starting with a
digit (`NOZ1260`, `BAW791G`, `FIN7LP`) or the three letters alone (`RYR`); `N12345` is a registration. Over the
two live snapshots used to choose the table (Europe and North America, 2026-11-19, about 23:35 UTC):

| | Aircraft | Airline-shaped | Named by the table |
|---|---|---|---|
| Europe | 1,335 | 1,268 (95.0 %) | 1,105 (87.1 %) |
| North America | 8,281 | 5,111 (61.7 %) | 4,122 (80.6 %) |
| Both | 9,616 | 6,379 | 5,227 (81.9 %) |

The rest show their three letters: mostly business, cargo, charter and military operators.

### Clutter and frame time, measured

`node scripts/measure_frames.mjs` serves the real page over 1,202 synthetic aircraft, sets the cap, and drags each
view for about two seconds in headless Chromium 159 on this laptop. The page reads no clock, so the script times
each `draw` from outside by wrapping `requestAnimationFrame`. Draws per second (17–21) are set by how fast the
script sends pointer events, so the draw time is the measure. Two runs, 2026-11-20:

| Viewport | View | Cap 200: p50 / p95 | Cap 500: p50 / p95 | Cap 1,000: p50 / p95 |
|---|---|---|---|---|
| 1440 × 900 | map | 9.5 / 28.1 ms; 9.3 / 29.6 ms | 10.6 / 29.5 ms; 7.3 / 17.7 ms | 11.4 / 27.2 ms; 7.8 / 22.9 ms |
| 1440 × 900 | globe | 12.0 / 29.3 ms; 13.5 / 36.4 ms | 15.1 / 41.7 ms; 13.4 / 37.2 ms | 17.0 / 41.5 ms; 15.5 / 32.5 ms |
| 390 × 844 | map | 7.6 / 18.8 ms; 7.0 / 22.6 ms | 9.5 / 26.8 ms; 7.0 / 15.5 ms | 10.4 / 25.9 ms; 10.3 / 27.3 ms |
| 390 × 844 | globe | 14.6 / 34.5 ms; 10.6 / 25.7 ms | 12.9 / 33.6 ms; 10.0 / 29.6 ms | 17.3 / 39.6 ms; 14.8 / 40.5 ms |

- **The globe does not hold 30 fps at the 95th percentile while dragging at 500 aircraft** on this machine
  (p95 33.6–41.7 ms against 33.3 ms), and not reliably at 200 either (25.7–36.4 ms). The median holds everywhere.
- **The cap is not what costs.** Reprojecting the coastline and graticule each frame does; above 400 aircraft at a
  wide zoom they are drawn as small squares, which are cheaper than arrows. A lower default would not buy 30 fps,
  so the default stays at 200: past that, the picture is clutter (at 1,000 a continent is a speckle).
- What each looks like: at 200, separate arrows with room between them; at 500, Europe's airways read as bands of
  squares; at 1,000, a solid speckle over Europe in which no single aircraft can be followed without focus.

## Settings

All are environment variables, or lines in `.env`. Changing one needs a restart and nothing else.

| Variable | Default | What it does |
|---|---|---|
| `FLIGHTOPS_OPENSKY_CLIENT_ID`, `_SECRET` | unset | OpenSky credentials. With both set the mode is `client_credentials`. |
| `FLIGHTOPS_OPENSKY_ANONYMOUS` | `false` | Opt in to OpenSky's anonymous tier. Ignored once credentials are set. |
| `FLIGHTOPS_DASHBOARD_PASSCODE` | unset | Required to serve anything but localhost. |
| `FLIGHTOPS_DASHBOARD_DAILY_CREDIT_BUDGET` | `1000` | Rolling 24 h ceiling on board and `airspace_snapshot` spend. |
| `FLIGHTOPS_DASHBOARD_TRAFFIC_CREDITS_PER_HOUR` | `60` | The traffic layer's own hourly ceiling. Auto-refresh stops, with the reason on the badge, when it is reached. |
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
  They are never animated between polls, and a follower moves only when a new observation lands.
- A missing altitude is never drawn or sorted as 0. A transit is never drawn past its last waypoint.
- `transit`, `estimated arrival` and `destination` are three lines, because they are three kinds of claim.
- The count of what is hidden or left out is always on screen: airports without a report, and aircraft past the cap.

These decisions are made in `dashboard/board.py` and `dashboard/aircraft.py`, so they are tested in Python. Two
run in the page each frame, because the view changes between polls: the far-side cull and the zoom at which the
toggle's default flips. Python holds the same rules with their tests, the server sends the thresholds, and a test
reads `board.js` to check that aircraft pass through the airports' `place`.

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
| The newest traffic refresh was refused | `Refresh Auto-refresh stopped: 60 of 60 traffic credits used in the last hour; ...` |
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
both sizes. The fixture lists the 3,270 world airports and reports client credentials, so the walk also covers the
traffic layer on by default, a triage with every panel field filled, a search, and the Documentation tab.
`A11Y_CHANNEL=msedge` runs it in an installed Edge instead of Playwright's Chromium. On 2026-11-19, in Edge
157.0.4341.60 with axe-core 4.14.1: 33 checks, 0 serious or critical violations, no overflow at 390 px. The stale,
refused and recorded states are not part of the walk.

Since the aircraft layer the walk also turns on `Show airports without a report`, focuses an aircraft from the
Focus list (the keyboard's way to an aircraft, since the canvas is not focusable), follows it and releases it with
Escape, and checks that Tab reaches every new control. On 2026-11-20, in Playwright's Chromium 159 with
axe-core 4.14.1: 41 checks, 0 serious or critical violations, no overflow at 390 px.

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

## Weather requests, measured

NOAA documents none of these limits. Each was measured live on 2026-11-19, against `/api/data/metar` at `hours=2`:

| What was sent | What NOAA did |
|---|---|
| 1 to 400 ids | `200`, but **the answer stops at 400 records**, silently. At `hours=2` a station sends about 2.4 records, so 150 ids already lost stations: 400 ids returned 268 stations, the same ids asked 25 at a time returned 355 |
| 401 ids or more | `200` with `{"status": "error", "error": "Maximum number of stations exceeded (400)"}` instead of data |
| about 1,200 ids (8 KB of URL) | `414 Request-URI Too Large` from nginx |
| any id outside `[A-Z0-9]`, such as `CN-0062` | `400 "Must specify station IDs or bounding box..."` for the **whole request**. Codes with digits (`K61B`, `5A8`) pass |
| `/taf` with a hyphenated id | the same `400` |

What the client does about it (`src/flightops_mcp/weather.py`):

- **50 ids per request.** Over a World poll the largest answer at 50 was 223 records, 56 % of the cap; at 100 it
  was 388, 97 % of the cap, one dense hour away from losing stations without a word.
- An answer of exactly 400 records is treated as cut and asked again in halves. So is the `200` error object,
  which the parser used to read as one report with no station, so every airport in the request read "no METAR".
- A refused request (`400`, `413`, `414`) is halved until the refused part is one id. That id alone is Unknown
  (`station_rejected`) with NOAA's words.
- A request NOAA fails to answer (`5xx` after retries, `429`, a timeout, an open circuit) is asked once more as two
  halves, then its airports are Unknown with the reason. A struggling upstream gets one second chance, not a
  bisection's worth of requests.
- The 43 world airports whose OurAirports code is a local one with a hyphen are never sent. Each is Unknown with
  that reason. The world file has no `icao_code` column to map them, so one that has a METAR under a real code
  (Western Sydney is `AU-0539` here) is not shown it.

A World view (3,270 airports) on this machine, through `scripts/measure_world_weather.py`, which builds the board
as `python -m dashboard` does and counts what reaches NOAA:

| | requests | wall time | own report | no observation | upstream failures | id not sent | neighbour line |
|---|---|---|---|---|---|---|---|
| before (`main`, 100 per request), 3 polls | 33-34 | 13.3, 16.8, 22.7 s | 1,366-1,371 | 665-673 | **1,200** | 0 | 0 |
| after (50 per request), 3 polls | 66-67 | 10.5, 29.3, 44.0 s | 2,136-2,169 | 1,036-1,039 | **0** | 43 | 58 |

- **All 1,200 failures were twelve requests of 100 that NOAA refused with `400`,** each because it held one
  hyphenated code. The brief this work came from blamed one request per airport (about 6,540); the board had already
  batched by 100 since v1.7.0, which is why the failures came in hundreds. 1,157 of those airports are now answered;
  the other 43 are the hyphenated codes themselves.
- **The request count doubled and the wall time did not improve.** Wall time is dominated by NOAA stalling a
  request past the 15 s timeout. Stalls were seen in 1 of 3 polls before, 3 of 4 at 50 per request, and in one poll
  of the running board (`python -m dashboard`, `/api/board?region=world`) that met 8 stalls and took 115.8 s. Every
  stalled request succeeded on retry, so none became a failure, but twice the requests meet a stall twice as often.
  Two polls at 100 per request with these fixes took 9.4 and 12.3 s with no stall and no failure, but at 97 % of
  the record cap.
- "No observation" rose because the 1,157 airports are now asked at all. 58 of the 1,037 (5.6 %) gained a neighbour
  line.
- At `hours=2` NOAA leaves out a station whose newest METAR is older than two hours. 50 ids returned 35 stations at
  `hours=2` and 42 without it, so some "no observation" airports do have a report, just an old one. That is
  unchanged here; an old report would be Unknown either way.

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
