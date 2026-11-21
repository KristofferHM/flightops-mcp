# The demo video

One take of the board, recorded unattended by a script and replayed from a committed recording. Nothing
is burned in: no title, no watermark, no edited frame. The page is the artefact.

**Status:** re-shot on 2026-11-21 with the quiet panels (`?` tooltips) and the layer switches; the GIF,
the poster and the README embed are committed. The MP4 is not yet attached to a release.

## Rebuild it

Once per clone:

```bash
uv sync
cd scripts && npm ci --ignore-scripts && npx playwright install chromium && cd ..
```

Then one command, either way:

```bash
node scripts/make_demo_video.mjs          # replay the committed recording: no network, no credentials
node scripts/make_demo_video.mjs --live   # record afresh from the live upstreams, then the same
```

`--live` is the re-shoot after a change to what the board asks for. It starts the board with
`--record` in a temporary directory outside the repository, drives the tour against NOAA and OpenSky
(about 10 OpenSky credits), compacts the recording into `docs/demo/board-recording.jsonl`, and then makes
the video from a replay of that file, so the published take is always reproducible. The board reads its
OpenSky credentials from the `.env` in `DEMO_ENV_DIR`, the repository root by default; from a worktree,
point it at the checkout that holds the `.env`. Without credentials the traffic layer is off and the tour
fails at the class chips, by name.

A plain run is enough after a change to how the board looks. If the board now asks for something the
recording does not hold, the take fails with `not in the recording: <path>`: run `--live`.

| File | What | In git |
|---|---|---|
| `docs/demo/board-recording.jsonl` | the recording a take replays | yes |
| `docs/demo/flightops-demo.gif` | 12 fps timeline, 900 px wide, 128-colour palette, at most 5 MB | yes |
| `docs/demo/poster.png` | one frame: the approaching list beside the globe | yes |
| `docs/demo/flightops-demo.mp4` | h264, yuv420p, 30 fps, 1280×720, faststart | never: attached to the release |
| `docs/demo/raw/` | the WebM take and its timeline | never |

The command prints each file's size and duration, ffmpeg's dropped and duplicated frames, and the GIF's
distinct frames, and fails when the GIF is over 5 MB. `--encode` re-encodes the last take without
recording one. `DEMO_CHANNEL=msedge` uses an installed Edge.

Attach the MP4 to the release, so that a README link to
`releases/latest/download/flightops-demo.mp4` resolves:

```bash
gh release upload <tag> docs/demo/flightops-demo.mp4
```

### Measured, 2026-11-20, Windows 10, Chromium 159

| Run | Wall time | MP4 | GIF | Poster |
|---|---|---|---|---|
| replay | 80 s | 1.88 MB, 42.57 s, 0 dropped | 3.12 MB, 41.75 s, 91 distinct frames | 0.69 MB |
| `--live` | 139 s | 1.93 MB, 42.10 s, 0 dropped | 3.27 MB, 41.75 s, 89 distinct frames | 0.74 MB |

### Measured, 2026-11-21, Windows 10, Chromium 159, after the quiet panels

| Run | Wall time | MP4 | GIF | Poster |
|---|---|---|---|---|
| replay, 1600×900 (committed) | 93 s | 1.88 MB, 45.33 s, 0 dropped, 0 duplicated | 3.24 MB, 43.83 s, 102 distinct frames | 0.64 MB, at 38.16 s |
| replay, 1280×720 (tried, not used) | 100 s | 1.69 MB, 44.60 s, 0 dropped | 2.58 MB, 43.50 s, 106 distinct frames | 0.62 MB |

At 1600×900 the map now fills about 375 of the video's 720 rows, against about 310 before the quiet panels.
At 1280×720 it sits on its floor (`min(220px, 35vh)`) and the globe is cramped beside a larger font, so the
default stays 1600×900.

The GIF merges each frame identical to the one before into that frame's delay (`mpdecimate`), which
keeps the 12 fps timing; without it the GIF was 7.62 MB. Its thresholds keep every frame of the click
ring.

## The tour

`scripts/record_demo.mjs`, about 42 s, each step held long enough to read:

1. The world view and its banner.
2. The class chips: Airline applied, then All.
3. ESGG, typed into the airport list's search and chosen: weather, aircraft heard, the approaching section.
4. The approaching list, its projected dashed lines, and its counts.
5. One approaching aircraft focused: the inspector, altitude in both units, the airline from the callsign.
6. Its observed transit, and the price on the estimate button (pointed at, never pressed).
7. The globe, centred on ESGG and zoomed in, beside the approaching list. The poster is taken here.
8. The unknown toggle: on, with its count of airports without a report, then off.

A ring in the board's own amber marks each click and fades over 400 ms. Playwright draws no cursor. The
ring is fixed, outside the layout, and deaf to the pointer, and the script checks that the point it rings
is the element it clicks.

The page is driven at 1600×900 (`DEMO_VIEWPORT`) and scaled into the 1280×720 video. On 2026-11-20 the
board left its map about 100 px of height at 1280×720; at 1600×900 the map is 944×388.

The sign-in page is never shown. The script posts the passcode through the browser context's request
client, so the cookie is set before the page opens and the passcode is never typed or drawn. When it
starts the board itself the passcode is random per run. It exits non-zero when a selector it needs is
missing, when the sign-in page appears, when a request leaves the machine, on a page error, and when a
replayed request finds no answer in the recording. A silently short video is worse than a failed run. It
stops the board's whole process tree: on Windows, stopping `uv` alone left Python serving.

## The recording

`docs/demo/board-recording.jsonl` is a board recording (`dashboard/sources.py` has the format), made by
`node scripts/make_demo_video.mjs --live` on 2026-11-20 at 20:02 UTC. The board polls every 30 s, so the
live run wrote 69 entries; replay reads only the newest answer for each call, so the 11 that are kept
replay identically (1.27 MB). Lines are copied byte for byte, so every payload hash still holds. The
compaction alone is `node scripts/record_demo.mjs --compact IN OUT`.

It holds public data only: NOAA METARs and TAFs for Europe's 508 listed airports, an OpenSky state
snapshot of Europe, ESGG's nearby traffic, one aircraft's track, and the server's `upstream_status`, which
reports the credential *mode* (`client_credentials`) and no credential. It was searched for credential
fields, tokens, passcodes and local paths before it was committed; the recorder strips `reference_data`
paths itself.

A replay's banner says `RECORDED` with the recording's time, as any replay does, not `LIVE`.

## What the 2026-11-20 take showed, read frame by frame

- The sign-in page never appears; the first frame is the board.
- No secret is on screen. The footer's "Client credentials" is the access mode.
- Unknowns are counted with their label (`Unknown 414` aircraft, `39 hidden — no report heard`), and no
  inspector row reads `0` for a value that was not reported.
- Each click ring lands on the control that changes in the same frame.
- **The banner prints the raw timestamp** in replay: `RECORDED 2026-11-20T19:31:42.203718+00:00`, while
  the badge under it reads `Recorded 2026-11-20 19:31 UTC` (`dashboard/board.py`, `banner()`).
- **Each chip click blanks the map for one frame** (33 ms in the MP4, one 83 ms frame in the GIF). The chip
  note changes height, the map's `ResizeObserver` sets `canvas.width`, which clears it, and the redraw waits
  for the next animation frame (`dashboard/static/board.js`, `resize()`).

Both are the board's behaviour, recorded as it is, and not changed on this branch.

## What the 2026-11-21 take showed, read frame by frame

- The first take of the day left the transit below the fold: Playwright counts an element inside a scroll box
  as visible, so step 6 passed with the inspector showing only the heading `Transit, estimate and
  destination`. The tour now scrolls the claims into view before pointing at the estimate button; the
  committed take shows `observed: 109 waypoints, 18:37Z–19:37Z (59 min)` with the button above it.
- The sign-in page never appears, no secret is on screen, and the unknown toggle reports `46 hidden` off
  and `46 without a report` on.
- **The banner still prints the raw timestamp** in replay: `RECORDED 2026-11-20T20:03:26.283923+00:00`.
- **Each chip click still blanks the map for one frame** (33 ms, at 4.47 s and 7.67 s in the first take,
  measured as the map's mean luma dropping from 32.8 to 26.0 and back). With the quiet panels the chip note
  now wraps onto a second line beside its `?` when Airline is pressed, which is the height change that
  clears the canvas.
- **The flat map stays on the whole world** through steps 1 to 6, so Europe is about a sixth of the map's
  width and the dashed approach lines around ESGG are too small to read until the globe zooms in at step 7.
- The globe's hover tooltip passes over the ESGG label during the zoom (about 33 s) and reads
  `ESGG: Weather VFR. VFR, METAR 21 min old`: the category is said twice. It is gone by the poster frame.
- The sort select in the aircraft panel is cut to `Distance from the ai`.

The banner, the blank frame, the doubled `VFR` and the cut select are the board's behaviour, recorded as
it is and not changed on this branch.
