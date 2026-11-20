# The demo video

One take of the board, recorded unattended by a script and replayed from a committed recording. Nothing
is burned in: no title, no watermark, no edited frame. The page is the artefact.

**Status:** the pipeline and the recording are committed; the GIF, the poster and the README embed are
held for the re-shoot after the board's descriptive text moves into `?` tooltips. That change frees
height for the map, so try `DEMO_VIEWPORT=1280x720` then (below).

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
| `docs/demo/flightops-demo.gif` | 12 fps timeline, 900 px wide, 128-colour palette, at most 5 MB | after the re-shoot |
| `docs/demo/poster.png` | one frame: the approaching list beside the globe | after the re-shoot |
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
