#!/usr/bin/env node
// The accessibility gate: the real board in a real browser, scanned by a pinned axe-core.
//
//   cd scripts && npm ci --ignore-scripts && npx playwright install chromium && cd ..
//   node scripts/a11y.mjs
//
// Fails on any serious or critical axe violation, on sideways overflow at 390 px, on a page
// error, on a state that did not render, and on any request that leaves this machine.

import { spawn } from "node:child_process";
import { randomBytes } from "node:crypto";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { createServer } from "node:net";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright";

const require = createRequire(import.meta.url);
const AXE_SOURCE = readFileSync(require.resolve("axe-core/axe.min.js"), "utf8");
const AXE_VERSION = require("axe-core/package.json").version;
const REPO_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const LOCAL_HOST = "127.0.0.1";
const VIEWPORTS = [
  { width: 1440, height: 900 },
  { width: 390, height: 844 },
];
const OVERFLOW_WIDTH = 390;
const BLOCKING = new Set(["serious", "critical"]);
const STARTUP_MS = 60_000;
// A fractional pixel is rounding, not overflow.
const OVERFLOW_TOLERANCE_PX = 0.5;

const failures = [];
let checks = 0;

function fail(where, message) {
  failures.push(`${where}: ${message}`);
  console.log(`FAIL ${where}: ${message}`);
}

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

async function startServer(scenario, passcode) {
  const port = await freePort();
  const child = spawn(
    "uv",
    ["run", "--frozen", "python", "scripts/a11y_server.py", "--scenario", scenario, "--port", `${port}`],
    {
      cwd: REPO_ROOT,
      // scripts/ is not a package root when run as a file, so the repository must be on the path.
      env: { ...process.env, A11Y_PASSCODE: passcode, PYTHONPATH: REPO_ROOT },
      stdio: ["pipe", "inherit", "inherit"],
    },
  );
  const base = `http://${LOCAL_HOST}:${port}`;
  const deadline = Date.now() + STARTUP_MS;
  while (Date.now() < deadline) {
    if (child.exitCode !== null) throw new Error(`the ${scenario} server exited with ${child.exitCode}`);
    try {
      if ((await fetch(`${base}/healthz`)).ok) return { base, stop: () => child.stdin.end() };
    } catch {
      await new Promise((done) => setTimeout(done, 200));
    }
  }
  child.stdin.end();
  throw new Error(`the ${scenario} server did not answer within ${STARTUP_MS} ms`);
}

async function scan(page, where) {
  // Evaluated, not added as a script tag: the board's CSP rightly refuses inline scripts.
  await page.evaluate(AXE_SOURCE);
  const violations = await page.evaluate(async () => {
    const results = await window.axe.run(document, { resultTypes: ["violations"] });
    return results.violations.map((rule) => ({
      id: rule.id,
      impact: rule.impact,
      help: rule.help,
      targets: rule.nodes.map((node) => node.target.join(" ")),
    }));
  });
  checks += 1;
  for (const rule of violations) {
    const line = `${rule.id} (${rule.impact}): ${rule.help} [${rule.targets.slice(0, 3).join(", ")}]`;
    if (BLOCKING.has(rule.impact)) fail(where, line);
    else console.log(`note ${where}: ${line}`);
  }
}

async function checkOverflow(page, where) {
  const found = await page.evaluate((tolerance) => {
    const root = document.documentElement;
    const limit = root.clientWidth;
    const describe = (element) =>
      element.id ? `#${element.id}` : `${element.tagName.toLowerCase()}.${element.classList[0] ?? ""}`;
    const wide = [...document.body.querySelectorAll("*")]
      .filter((element) => element.getBoundingClientRect().width > limit + tolerance)
      .map(describe);
    return { scrollWidth: root.scrollWidth, limit, wide };
  }, OVERFLOW_TOLERANCE_PX);
  checks += 1;
  if (found.scrollWidth > found.limit) {
    fail(where, `overflow: the page scrolls sideways (${found.scrollWidth} px in ${found.limit} px)`);
  }
  if (found.wide.length) {
    fail(where, `overflow: wider than the ${found.limit} px viewport: ${found.wide.slice(0, 5).join(", ")}`);
  }
}

async function inspect(page, viewport, state) {
  const where = `${viewport.width}x${viewport.height} ${state}`;
  await scan(page, where);
  if (viewport.width === OVERFLOW_WIDTH) await checkOverflow(page, where);
  console.log(`checked ${where}`);
}

async function open(page, base, passcode, viewport) {
  await page.goto(base);
  if (new URL(page.url()).pathname === "/login") {
    await inspect(page, viewport, "sign-in");
    await page.getByLabel("Passcode").fill(passcode);
    await page.getByRole("button", { name: "Open the board" }).click();
  }
  await page.locator("#strips button").first().waitFor();
}

async function expectBadge(page, text) {
  await page.locator("#last-update", { hasText: text }).waitFor();
}

async function walk(browser, viewport, servers, passcode) {
  const context = await browser.newContext({ viewport });
  const size = `${viewport.width}x${viewport.height}`;
  await context.route("**/*", (route) => {
    const url = new URL(route.request().url());
    if (url.hostname === LOCAL_HOST) return route.continue();
    fail(size, `a request left this machine: ${url.origin}`);
    return route.abort();
  });
  const page = await context.newPage();
  page.on("pageerror", (error) => fail(size, `page error: ${error.message}`));

  await open(page, servers.data.base, passcode, viewport);
  await expectBadge(page, "Last update");
  await page.locator('#banner[data-mode="LIVE"]').waitFor();
  await inspect(page, viewport, "map");

  await page.getByRole("radio", { name: "Globe" }).check({ force: true });
  await inspect(page, viewport, "globe");

  await page.getByRole("radio", { name: "Map" }).check({ force: true });
  const strip = page.locator("#strips button").first();
  const icao = await strip.getAttribute("data-icao");
  await strip.click();
  await page.locator("#d-title", { hasText: icao }).waitFor();
  await page.locator("#d-traffic", { hasText: "1 heard" }).waitFor();
  await inspect(page, viewport, "airport selected");

  await context.clearCookies();
  await open(page, servers.empty.base, passcode, viewport);
  await expectBadge(page, "no data yet");
  await page.locator('#banner[data-mode="NO DATA"]').waitFor();
  await inspect(page, viewport, "no data yet");

  await context.clearCookies();
  await open(page, servers.noTime.base, passcode, viewport);
  await expectBadge(page, "freshness unknown — no upstream time");
  await page.locator('#last-update[data-state="no_upstream_time"]').waitFor();
  await inspect(page, viewport, "no upstream time");

  await context.close();
}

async function main() {
  // Generated per run, so no credential exists in the repository or the job's environment.
  const passcode = randomBytes(18).toString("hex");
  const servers = {
    data: await startServer("data", passcode),
    empty: await startServer("empty", passcode),
    noTime: await startServer("no_time", passcode),
  };
  const browser = await chromium.launch();
  try {
    console.log(`axe-core ${AXE_VERSION}, Chromium ${browser.version()}`);
    for (const viewport of VIEWPORTS) await walk(browser, viewport, servers, passcode);
  } finally {
    await browser.close();
    servers.data.stop();
    servers.empty.stop();
    servers.noTime.stop();
  }
  if (failures.length) {
    console.log(`FAILED: ${failures.length} problems in ${checks} checks`);
    process.exitCode = 1;
    return;
  }
  console.log(`PASSED: ${checks} checks, 0 serious or critical violations, no overflow at 390 px`);
}

main().catch((error) => {
  console.error(`FAILED: ${error.message}`);
  process.exitCode = 1;
});
