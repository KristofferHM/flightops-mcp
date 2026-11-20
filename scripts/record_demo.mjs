#!/usr/bin/env node
// The demo video, recorded unattended: the real board in a real browser, driven through a
// scripted tour, captured as WebM by Playwright. scripts/make_demo_video.mjs turns it into
// the MP4, the GIF and the poster.
//
//   node scripts/record_demo.mjs                   # a take, replayed from the committed recording
//   node scripts/record_demo.mjs --live            # a new recording from the live upstreams
//   node scripts/record_demo.mjs --compact IN OUT  # keep only the answers a replay reads
//
// A take starts the board on docs/demo/board-recording.jsonl, so it needs no network, spends
// no credits and draws the same frames every time. --live is the one run that spends: it
// starts the board with --record, drives the same tour against the real upstreams, and
// compacts what was recorded into that file. The board reads its credentials from the .env
// in DEMO_ENV_DIR (default: the repository root), the way `python -m dashboard` does there.
// DEMO_BASE drives a board that is already running instead, and starts nothing.
//
//   DEMO_BASE      a running board; nothing is started
//   DEMO_PASSCODE  that board's passcode; when the script starts the board, a fresh one is made
//   DEMO_ENV_DIR   --live only: the directory whose .env holds the OpenSky credentials
//   DEMO_FIXTURE   the recording (default docs/demo/board-recording.jsonl)
//   DEMO_OUT       where the WebM and its timeline go (default docs/demo/raw)
//   DEMO_VIEWPORT  the page size, WIDTHxHEIGHT (default 1600x900); the video is 1280x720
//   DEMO_CHANNEL   msedge, to use an installed Edge instead of Playwright's Chromium
//
// Exits non-zero when a selector the tour needs is missing, when the sign-in page appears,
// when a request leaves this machine, and when a replayed answer was not in the recording:
// a silently short or wrong video is worse than a failed run.

import { spawn, spawnSync } from "node:child_process";
import { randomBytes } from "node:crypto";
import { copyFileSync, mkdirSync, mkdtempSync, readFileSync, readdirSync, rmSync, writeFileSync } from "node:fs";
import { createServer } from "node:net";
import { tmpdir } from "node:os";
import { dirname, join, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright";

const REPO_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const LOCAL_HOSTS = new Set(["127.0.0.1", "localhost", "[::1]"]);
// The board gives its map whatever height the controls and legend leave. At 1280 x 720 that
// was about 100 px (2026-11-20), so the page is driven at 1600 x 900 and scaled into a
// 1280 x 720 video: the same 16:9, every element at 0.8, and a map of 944 x 388.
function viewport() {
  const match = /^(\d+)x(\d+)$/.exec(process.env.DEMO_VIEWPORT || "1600x900");
  if (!match) throw new Error("DEMO_VIEWPORT must look like 1600x900");
  return { width: Number(match[1]), height: Number(match[2]) };
}
const SIZE = { width: 1280, height: 720 };
const STARTUP_MS = 90_000;
// Generous, because the recording run waits on live upstreams; a replay answers at once.
const STEP_MS = 60_000;
const RING_MS = 400;
// Longer than the board's 700 ms view debounce, so every view the tour passes through is
// asked for on its own, in the recording run and in every replay alike.
const VIEW_SETTLE_MS = 1_100;

const FIXTURE = resolve(REPO_ROOT, process.env.DEMO_FIXTURE || "docs/demo/board-recording.jsonl");
const OUT = resolve(REPO_ROOT, process.env.DEMO_OUT || "docs/demo/raw");

class TourError extends Error {}

function freePort() {
  return new Promise((done, reject) => {
    const probe = createServer();
    probe.once("error", reject);
    probe.listen(0, "127.0.0.1", () => {
      const { port } = probe.address();
      probe.close(() => done(port));
    });
  });
}

// `uv` starts Python as a child of its own. On Windows, killing `uv` leaves that child
// serving, so the whole tree is stopped.
function stopTree(child) {
  if (child.exitCode !== null) return;
  if (process.platform === "win32") {
    spawnSync("taskkill", ["/PID", `${child.pid}`, "/T", "/F"], { stdio: "ignore" });
  } else {
    child.kill();
  }
}

// The board, on the recording (`--replay FILE`) or recording the live tools (`--record DIR`).
async function startBoard(mode, passcode, cwd = REPO_ROOT) {
  const port = await freePort();
  const child = spawn(
    "uv",
    ["run", "--frozen", "--project", REPO_ROOT, "python", "-m", "dashboard", ...mode, "--port", `${port}`],
    {
      cwd,
      env: { ...process.env, FLIGHTOPS_DASHBOARD_PASSCODE: passcode, PYTHONPATH: REPO_ROOT },
      stdio: ["ignore", "inherit", "inherit"],
    },
  );
  const base = `http://127.0.0.1:${port}`;
  const stop = () => stopTree(child);
  const deadline = Date.now() + STARTUP_MS;
  while (Date.now() < deadline) {
    if (child.exitCode !== null) throw new TourError(`the board exited with ${child.exitCode}`);
    try {
      if ((await fetch(`${base}/healthz`)).ok) return { base, stop };
    } catch {
      // Not listening yet.
    }
    await new Promise((done) => setTimeout(done, 250));
  }
  stop();
  throw new TourError(`the board did not answer /healthz within ${STARTUP_MS} ms`);
}

// The sign-in form is never shown: the cookie is fetched through the context's own request
// client, so the passcode is never typed, never rendered and never in a frame.
async function seedCookie(context, base, passcode) {
  if (!passcode) return;
  const answer = await context.request.post(`${base}/login`, {
    form: { passcode },
    maxRedirects: 0,
  });
  if (answer.status() !== 303) {
    throw new TourError(`signing in answered ${answer.status()}, not 303: is DEMO_PASSCODE right?`);
  }
  const cookies = await context.cookies(base);
  if (!cookies.some((cookie) => cookie.name === "flightops_board")) {
    throw new TourError("signing in set no board cookie");
  }
}

// A ring where each click lands, fading over RING_MS. Fixed, outside the layout, and deaf to
// the pointer, so it moves nothing and catches no click. Built through the CSSOM and the Web
// Animations API, which the board's CSP (no inline styles) allows.
async function ring(page, x, y) {
  await page.evaluate(
    ({ x, y, ms }) => {
      const accent =
        getComputedStyle(document.documentElement).getPropertyValue("--amber").trim() || "#E8A133";
      const mark = document.createElement("div");
      mark.setAttribute("aria-hidden", "true");
      Object.assign(mark.style, {
        position: "fixed",
        left: `${x - 17}px`,
        top: `${y - 17}px`,
        width: "34px",
        height: "34px",
        boxSizing: "border-box",
        border: `3px solid ${accent}`,
        borderRadius: "50%",
        pointerEvents: "none",
        zIndex: "2147483647",
      });
      document.documentElement.append(mark);
      mark
        .animate(
          [
            { opacity: 1, transform: "scale(0.6)" },
            { opacity: 0, transform: "scale(1.6)" },
          ],
          { duration: ms, easing: "ease-out" },
        )
        .finished.then(() => mark.remove());
    },
    { x, y, ms: RING_MS },
  );
}

class Tour {
  constructor(page, started) {
    this.page = page;
    this.started = started;
    this.marks = [];
  }

  mark(name) {
    this.marks.push({ name, at_s: (Date.now() - this.started) / 1000 });
    console.log(`  ${((Date.now() - this.started) / 1000).toFixed(1).padStart(5)} s  ${name}`);
  }

  async hold(ms) {
    await this.page.waitForTimeout(ms);
  }

  // Every selector the tour needs goes through here, so a missing one stops the run by name.
  async need(selector, what, options = {}) {
    const locator = typeof selector === "string" ? this.page.locator(selector, options) : selector;
    try {
      await locator.first().waitFor({ state: "visible", timeout: STEP_MS });
    } catch {
      throw new TourError(`missing: ${what} (${typeof selector === "string" ? selector : "locator"})`);
    }
    return locator.first();
  }

  async pointAt(locator, what) {
    await locator.scrollIntoViewIfNeeded();
    const box = await locator.boundingBox();
    if (!box) throw new TourError(`missing: ${what} has no box on screen`);
    const x = Math.round(box.x + box.width / 2);
    const y = Math.round(box.y + box.height / 2);
    // The point must reach the element itself, or the ring would mark one thing and the
    // click hit another.
    const hits = await locator.evaluate(
      (element, [px, py]) => {
        const top = document.elementFromPoint(px, py);
        return Boolean(top && (top === element || element.contains(top) || top.contains(element)));
      },
      [x, y],
    );
    if (!hits) throw new TourError(`${what} is covered at (${x}, ${y})`);
    await this.page.mouse.move(x, y, { steps: 14 });
    return { x, y };
  }

  async click(selector, what, options) {
    const locator = await this.need(selector, what, options);
    const { x, y } = await this.pointAt(locator, what);
    await ring(this.page, x, y);
    await this.page.mouse.click(x, y);
  }

  async hover(selector, what) {
    const locator = await this.need(selector, what);
    await this.pointAt(locator, what);
  }
}

async function tour(page, started) {
  const t = new Tour(page, started);

  // 1. The world view and its banner.
  await t.need("#strips button", "the airport list");
  await t.need('#banner[data-mode="LIVE"], #banner[data-mode="RECORDED"]', "a live or recorded banner");
  await t.need("#class-chips button", "the class chips (the traffic layer)");
  await t.need("#unknown-note", "the unknown note");
  // Let the first paint of the layer settle before the tour starts; the video is cut here.
  await t.hold(1_500);
  t.mark("start");
  await t.hold(4_000);

  // 2. The class chips: one applied and released.
  t.mark("chips");
  const airline = page.locator('#class-chips button[data-class="airline"]');
  await t.click(airline, "the Airline chip");
  await t.need('#class-chips button[data-class="airline"][aria-pressed="true"]', "the Airline chip, pressed");
  await t.hold(2_800);
  await t.click(page.locator('#class-chips button[data-class=""]'), "the All chip");
  await t.need('#class-chips button[data-class=""][aria-pressed="true"]', "the All chip, pressed");
  await t.hold(1_500);

  // 3. ESGG from the airport list. The search box is the list's own; it takes no credential.
  t.mark("select");
  await t.click("#search", "the airport search");
  await page.locator("#search").pressSequentially("ESGG", { delay: 140 });
  await t.click('#strips button[data-icao="ESGG"]', "the ESGG strip");
  await t.need("#d-title", "the airport panel", { hasText: "ESGG" });
  await t.need("#d-weather", "the airport's weather", { hasText: /\S/ });
  await t.hold(1_500);

  // 4. What approaches it.
  t.mark("inbound");
  await t.need("#i-summary", "the approaching summary", { hasText: /approaching/ });
  const first = await t.need("#i-list button", "an approaching aircraft");
  await t.pointAt(page.locator("#inbound"), "the approaching section");
  await t.hover(first, "the first approaching aircraft");
  await t.hold(4_500);

  // 5. One aircraft, focused.
  t.mark("focus");
  await t.click(first, "the first approaching aircraft");
  await t.need("#aircraft-panel", "the aircraft inspector");
  await t.need("#a-facts dd", "the aircraft's facts");
  await t.hold(4_500);

  // 6. Its observed track, and the price on the button that buys more.
  t.mark("track");
  await t.need("#a-claims dd", "the transit line", { hasText: /observed|unavailable|no /i });
  await t.hover("#a-estimate", "the estimate button and its price");
  await t.hold(3_500);

  // 7. The globe: the same data, centred on the airport by choosing it again.
  t.mark("globe");
  await t.click(page.locator(".segmented label", { hasText: "Globe" }), "the Globe choice");
  await t.hold(VIEW_SETTLE_MS);
  await t.click('#strips button[data-icao="ESGG"]', "the ESGG strip, again");
  await t.hold(VIEW_SETTLE_MS);
  const canvas = await t.need("#map", "the map canvas");
  await t.pointAt(canvas, "the map canvas");
  for (let notch = 0; notch < 4; notch += 1) {
    await page.mouse.wheel(0, -240);
    await t.hold(VIEW_SETTLE_MS);
  }
  // The inspector above pushed the approaching list out of the panel. Pointing at it scrolls
  // it back beside the globe, and leaving the canvas clears the globe's tooltip. A hover
  // only lights a line: nothing is asked of the server, so the replay stays exact.
  await t.hover("#i-list button", "the approaching list, beside the globe");
  t.mark("poster");
  await t.hold(3_000);

  // 8. The unknown toggle: what it hides, and the count it reports.
  t.mark("unknown");
  const note = page.locator("#unknown-note");
  const before = (await note.textContent()) || "";
  await t.click(page.locator("label.check", { has: page.locator("#unknown-on") }), "the unknown toggle");
  await page
    .waitForFunction((was) => document.getElementById("unknown-note").textContent !== was, before, {
      timeout: STEP_MS,
    })
    .catch(() => {
      throw new TourError("the unknown note did not change when the toggle was pressed");
    });
  await t.hold(3_000);
  await t.click(page.locator("label.check", { has: page.locator("#unknown-on") }), "the unknown toggle, again");
  await t.hold(2_000);
  t.mark("end");
  return t.marks;
}

async function main(live) {
  const wall = Date.now();
  const given = process.env.DEMO_BASE;
  const passcode = process.env.DEMO_PASSCODE || (given ? "" : randomBytes(18).toString("hex"));
  // A live recording is written outside the repository, as the board insists, and only its
  // compacted copy is kept.
  const recordings = live && !given ? mkdtempSync(join(tmpdir(), "flightops-demo-")) : null;
  let board;
  if (given) board = { base: given.replace(/\/$/, ""), stop: () => {} };
  else if (live) {
    board = await startBoard(["--record", recordings], passcode, resolve(process.env.DEMO_ENV_DIR || REPO_ROOT));
  } else board = await startBoard(["--replay", FIXTURE], passcode);
  const replaying = !given && !live;
  // The live take only feeds the recording; the published video is always a replay.
  const out = live ? join(recordings || mkdtempSync(join(tmpdir(), "flightops-demo-")), "take") : OUT;
  const problems = [];
  let browser = null;
  try {
    const channel = process.env.DEMO_CHANNEL;
    browser = await chromium.launch(channel ? { channel } : {});
    console.log(`${replaying ? "replaying" : "driving"} ${replaying ? relative(REPO_ROOT, FIXTURE) : board.base}, Chromium ${browser.version()}`);
    rmSync(join(out, "video"), { recursive: true, force: true });
    mkdirSync(out, { recursive: true });
    const context = await browser.newContext({
      viewport: viewport(),
      deviceScaleFactor: 1,
      colorScheme: "dark",
      recordVideo: { dir: join(out, "video"), size: SIZE },
    });
    await context.route("**/*", (route) => {
      const url = new URL(route.request().url());
      if (LOCAL_HOSTS.has(url.hostname)) return route.continue();
      problems.push(`a request left this machine: ${url.origin}`);
      return route.abort();
    });
    await seedCookie(context, board.base, passcode);

    const page = await context.newPage();
    // The video starts with the page; every mark is measured from here.
    const started = Date.now();
    page.on("pageerror", (error) => problems.push(`page error: ${error.message}`));
    page.on("framenavigated", (frame) => {
      if (frame === page.mainFrame() && new URL(frame.url()).pathname === "/login") {
        problems.push("the sign-in page appeared");
      }
    });
    page.on("response", async (response) => {
      if (!replaying || !new URL(response.url()).pathname.startsWith("/api/")) return;
      const body = await response.text().catch(() => "");
      if (body.includes("not_recorded")) {
        problems.push(`not in the recording: ${new URL(response.url()).pathname}`);
      }
    });

    await page.goto(board.base);
    let marks = [];
    try {
      marks = await tour(page, started);
    } finally {
      await context.close();
    }
    const video = await page.video().path();
    const webm = join(out, "flightops-demo.webm");
    copyFileSync(video, webm);
    rmSync(join(out, "video"), { recursive: true, force: true });
    if (problems.length) throw new TourError(problems.join("; "));
    const timeline = {
      size: SIZE,
      viewport: viewport(),
      source: replaying ? relative(REPO_ROOT, FIXTURE).replaceAll("\\", "/") : live ? "live upstreams" : "a running board",
      marks,
    };
    writeFileSync(join(out, "timeline.json"), `${JSON.stringify(timeline, null, 2)}\n`);
    console.log(`wrote ${webm} and timeline.json in ${((Date.now() - wall) / 1000).toFixed(1)} s`);
  } finally {
    if (browser) await browser.close();
    board.stop();
  }
  if (recordings) {
    const made = readdirSync(recordings).filter((name) => /^board-.*\.jsonl$/.test(name));
    if (made.length !== 1) throw new TourError(`expected one recording in ${recordings}, found ${made.length}`);
    compact(join(recordings, made[0]), FIXTURE);
    rmSync(recordings, { recursive: true, force: true });
  }
}

// Replay keeps only the newest answer for each call, so the older ones are dead weight: a
// recording's polls repeat the same calls every 30 s. Lines are copied byte for byte, so
// every payload hash still holds.
function compact(input, output) {
  const lines = readFileSync(input, "utf8").split("\n").filter((line) => line.trim());
  const canonical = (value) =>
    Array.isArray(value)
      ? `[${value.map(canonical).join(",")}]`
      : value && typeof value === "object"
        ? `{${Object.keys(value).sort().map((key) => `${JSON.stringify(key)}:${canonical(value[key])}`).join(",")}}`
        : JSON.stringify(value);
  const newest = new Map();
  for (const [index, line] of lines.slice(1).entries()) {
    const entry = JSON.parse(line);
    newest.set(`${entry.tool} ${canonical(entry.arguments)}`, index);
  }
  const wanted = new Set(newest.values());
  const kept = lines.slice(1).filter((_, index) => wanted.has(index));
  writeFileSync(output, `${[lines[0], ...kept].join("\n")}\n`);
  console.log(`kept ${kept.length} of ${lines.length - 1} entries in ${relative(process.cwd(), output)}`);
}

const run =
  process.argv[2] === "--compact"
    ? async () => {
        const [input, output] = process.argv.slice(3);
        if (!input || !output) throw new TourError("usage: --compact RECORDING OUT");
        compact(resolve(input), resolve(output));
      }
    : () => main(process.argv[2] === "--live");

run().catch((error) => {
  console.error(`FAILED: ${error.message}`);
  process.exitCode = 1;
});
