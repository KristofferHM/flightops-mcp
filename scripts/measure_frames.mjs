#!/usr/bin/env node
// Clutter and frame time at three aircraft caps, on the real page, while dragging.
//
//   node scripts/measure_frames.mjs [--channel msedge] [--shots DIR]
//
// Serves the real board over the accessibility fixture with 1,200 extra synthetic aircraft
// (scripts/a11y_server.py --aircraft), sets the cap, drags the map and the globe for about
// two seconds each, and times each frame from outside: the page reads no clock, so this
// wraps `requestAnimationFrame` and times the callbacks named `draw`. Prints one line per
// run and a Markdown table for docs/DASHBOARD.md. No request leaves this machine.

import { spawn } from "node:child_process";
import { mkdirSync } from "node:fs";
import { createServer } from "node:net";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright";

const REPO_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const LOCAL_HOST = "127.0.0.1";
const EXTRA_AIRCRAFT = 1200;
const CAPS = [200, 500, 1000];
const VIEWPORTS = [
  { width: 1440, height: 900 },
  { width: 390, height: 844 },
];
const DRAG_MOVES = 120;
const MOVE_GAP_MS = 16;

const argument = (name) => {
  const index = process.argv.indexOf(name);
  return index > 0 ? process.argv[index + 1] : undefined;
};

function freePort() {
  return new Promise((done, reject) => {
    const probe = createServer();
    probe.once("error", reject);
    probe.listen(0, LOCAL_HOST, () => {
      const { port } = probe.address();
      probe.close(() => done(port));
    });
  });
}

async function startServer() {
  const port = await freePort();
  const child = spawn(
    "uv",
    ["run", "--frozen", "python", "scripts/a11y_server.py", "--scenario", "data", "--port", `${port}`,
      "--aircraft", `${EXTRA_AIRCRAFT}`],
    { cwd: REPO_ROOT, env: { ...process.env, PYTHONPATH: REPO_ROOT }, stdio: ["pipe", "inherit", "inherit"] },
  );
  const base = `http://${LOCAL_HOST}:${port}`;
  for (const deadline = Date.now() + 60_000; Date.now() < deadline;) {
    if (child.exitCode !== null) throw new Error(`the fixture server exited with ${child.exitCode}`);
    try {
      if ((await fetch(`${base}/healthz`)).ok) return { base, stop: () => child.stdin.end() };
    } catch {
      await new Promise((done) => setTimeout(done, 200));
    }
  }
  child.stdin.end();
  throw new Error("the fixture server did not answer");
}

const percentile = (values, p) => {
  if (!values.length) return null;
  const sorted = [...values].sort((a, b) => a - b);
  return sorted[Math.min(sorted.length - 1, Math.floor((p / 100) * sorted.length))];
};

async function drag(page) {
  const box = await page.locator("#map").boundingBox();
  const cx = box.x + box.width / 2, cy = box.y + box.height / 2;
  const reach = Math.min(box.width, box.height) / 4;
  await page.evaluate(() => {
    window.__drawTimes = [];
    window.__frames = 0;
    window.__counting = true;
    const tick = () => { if (window.__counting) { window.__frames += 1; requestAnimationFrame(tick); } };
    requestAnimationFrame(tick);
    window.__started = performance.now();
  });
  await page.mouse.move(cx, cy);
  await page.mouse.down();
  for (let n = 1; n <= DRAG_MOVES; n += 1) {
    const angle = (n / DRAG_MOVES) * Math.PI * 2;
    await page.mouse.move(cx + Math.sin(angle) * reach, cy + (1 - Math.cos(angle)) * reach * 0.5);
    await page.waitForTimeout(MOVE_GAP_MS);
  }
  await page.mouse.up();
  return page.evaluate(() => {
    window.__counting = false;
    const seconds = (performance.now() - window.__started) / 1000;
    return { seconds, frames: window.__frames, draws: [...window.__drawTimes] };
  });
}

async function run(browser, base, viewport, cap, projection, shots) {
  const context = await browser.newContext({ viewport });
  await context.route("**/*", (route) =>
    new URL(route.request().url()).hostname === LOCAL_HOST ? route.continue() : route.abort());
  await context.addInitScript(() => {
    window.__drawTimes = [];
    const original = window.requestAnimationFrame.bind(window);
    window.requestAnimationFrame = (callback) => original((time) => {
      if (callback.name !== "draw") return callback(time);
      const started = performance.now();
      callback(time);
      window.__drawTimes.push(performance.now() - started);
      return undefined;
    });
  });
  await context.addInitScript((chosen) => {
    localStorage.setItem("airport-board.region", "europe");
    localStorage.setItem("airport-board.aircraft", JSON.stringify({ sort: "last_heard", cap: String(chosen) }));
  }, cap);
  const page = await context.newPage();
  await page.goto(base);
  await page.locator("#a-selection", { hasText: "showing" }).waitFor();
  const selection = await page.locator("#a-selection").textContent();
  if (projection === "globe") await page.getByRole("radio", { name: "Globe" }).check({ force: true });
  await page.waitForTimeout(800);
  if (shots) {
    await page.locator(".stage").screenshot({ path: join(shots, `${viewport.width}-${projection}-${cap}.png`) });
  }
  const result = await drag(page);
  await context.close();
  const draws = result.draws;
  return {
    viewport: `${viewport.width}x${viewport.height}`,
    projection,
    cap,
    selection: selection.trim(),
    drawsPerSecond: draws.length / result.seconds,
    framesPerSecond: result.frames / result.seconds,
    p50: percentile(draws, 50),
    p95: percentile(draws, 95),
    max: draws.length ? Math.max(...draws) : null,
  };
}

async function main() {
  const shots = argument("--shots");
  if (shots) mkdirSync(shots, { recursive: true });
  const server = await startServer();
  const channel = argument("--channel");
  const browser = await chromium.launch(channel ? { channel } : {});
  const rows = [];
  try {
    console.log(`Chromium ${browser.version()}, ${EXTRA_AIRCRAFT + 2} aircraft in the layer`);
    for (const viewport of VIEWPORTS) {
      for (const projection of ["map", "globe"]) {
        for (const cap of CAPS) {
          const row = await run(browser, server.base, viewport, cap, projection, shots);
          rows.push(row);
          console.log(
            `${row.viewport} ${projection} cap ${cap}: ${row.drawsPerSecond.toFixed(1)} draws/s `
            + `(${row.framesPerSecond.toFixed(1)} frames/s), draw p50 ${row.p50.toFixed(1)} ms, `
            + `p95 ${row.p95.toFixed(1)} ms, max ${row.max.toFixed(1)} ms; ${row.selection}`,
          );
        }
      }
    }
  } finally {
    await browser.close();
    server.stop();
  }
  console.log("\n| Viewport | View | Cap | Drawn | Draws/s while dragging | Draw p50 | Draw p95 | Max |");
  console.log("|---|---|---|---|---|---|---|---|");
  for (const row of rows) {
    const shown = /showing ([\d,]+)/.exec(row.selection);
    console.log(
      `| ${row.viewport} | ${row.projection} | ${row.cap} | ${shown ? shown[1] : "?"} | `
      + `${row.drawsPerSecond.toFixed(0)} | ${row.p50.toFixed(1)} ms | ${row.p95.toFixed(1)} ms | ${row.max.toFixed(1)} ms |`,
    );
  }
}

main().catch((error) => {
  console.error(`FAILED: ${error.message}`);
  process.exitCode = 1;
});
