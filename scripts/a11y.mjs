#!/usr/bin/env node
// The accessibility gate: the real board in a real browser, scanned by a pinned axe-core.
//
//   cd scripts && npm ci --ignore-scripts && npx playwright install chromium && cd ..
//   node scripts/a11y.mjs
//
// Fails on any serious or critical axe violation, on sideways overflow at 390 px, on panels that
// overlap or a map squeezed to nothing, on a `?` explanation that is not a focusable button
// with a described, non-empty tooltip, on a page error, on a state that did not render, and on
// any request that leaves this machine.

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
  // The demo poster's size, where the map once collapsed under the badge and the legend.
  { width: 1280, height: 720 },
  { width: 390, height: 844 },
];
const OVERFLOW_WIDTH = 390;
const BLOCKING = new Set(["serious", "critical"]);
const STARTUP_MS = 60_000;
// A fractional pixel is rounding, not overflow.
const OVERFLOW_TOLERANCE_PX = 0.5;
// The panels that tile the board. No two may overlap, at any size, in any state.
const PANELS = [".topline", ".bay", ".chart-tools", ".stage", ".legend", ".detail", ".governance"];
// The right column's three parts.
const SIDEBAR = ["#aircraft-tools", "#aircraft-panel", "#detail"];
// Below this the map is not a map.
const MIN_MAP_PX = 200;

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

// Boxes, not pixels: two panels overlap when their rectangles intersect by more than rounding.
async function checkLayout(page, where) {
  const found = await page.evaluate(({ panels, sidebar, tolerance }) => {
    const box = (selector) => {
      const element = document.querySelector(selector);
      if (!element || element.closest("[hidden]")) return null;
      const r = element.getBoundingClientRect();
      return r.width && r.height ? { selector, x: r.x, y: r.y, right: r.right, bottom: r.bottom } : null;
    };
    const meet = (a, b) => Math.min(a.right, b.right) - Math.max(a.x, b.x) > tolerance
      && Math.min(a.bottom, b.bottom) - Math.max(a.y, b.y) > tolerance;
    const overlaps = [];
    for (const group of [panels, sidebar]) {
      const boxes = group.map(box).filter(Boolean);
      boxes.forEach((a, i) => boxes.slice(i + 1).forEach((b) => {
        if (meet(a, b)) overlaps.push(`${a.selector} and ${b.selector}`);
      }));
    }
    // The badge belongs to the map: never over the controls or the legend.
    const badge = box("#last-update");
    for (const other of [".chart-tools", ".legend"].map(box).filter(Boolean)) {
      if (badge && meet(badge, other)) overlaps.push(`#last-update and ${other.selector}`);
    }
    const map = box(".canvas-wrap");
    return { overlaps, map: map ? Math.round(map.bottom - map.y) : null };
  }, { panels: PANELS, sidebar: SIDEBAR, tolerance: OVERFLOW_TOLERANCE_PX });
  checks += 1;
  if (found.overlaps.length) fail(where, `layout: overlapping boxes: ${found.overlaps.join("; ")}`);
  if (found.map !== null && found.map < MIN_MAP_PX) fail(where, `layout: the map is ${found.map} px tall`);
}

// Every `?` on screen: a real button in the tab order, described by a tooltip with words in it.
async function checkHints(page, where) {
  const found = await page.evaluate(() => [...document.querySelectorAll(".hint-button")]
    .filter((button) => !button.closest("[hidden]") && button.getClientRects().length)
    .map((button) => {
      const target = document.getElementById(button.getAttribute("aria-describedby") || "");
      return {
        id: button.id,
        button: button.tagName === "BUTTON",
        tabbable: button.tabIndex >= 0 && !button.disabled,
        described: Boolean(target && target.textContent.trim()),
        role: target ? target.getAttribute("role") : null,
      };
    }));
  checks += 1;
  for (const hint of found) {
    if (!hint.button || !hint.tabbable) fail(where, `hint: #${hint.id} is not a focusable button`);
    if (!hint.described) fail(where, `hint: #${hint.id} describes nothing`);
    if (hint.role !== "tooltip") fail(where, `hint: #${hint.id}'s description is not a tooltip`);
  }
  return found.map((hint) => hint.id);
}

async function inspect(page, viewport, state) {
  const where = `${viewport.width}x${viewport.height} ${state}`;
  await scan(page, where);
  if (viewport.width === OVERFLOW_WIDTH) await checkOverflow(page, where);
  await checkLayout(page, where);
  await checkHints(page, where);
  console.log(`checked ${where}`);
}

// What a reader sees that is data: the banner, every count on a chip, every visible line, and
// the size of each panel. Sizes, not positions: a click may scroll, a push changes a size.
// Opening and closing an explanation must move none of it.
async function screenState(page) {
  return page.evaluate(() => {
    const rect = (selector) => {
      const r = document.querySelector(selector).getBoundingClientRect();
      return [r.width, r.height].map(Math.round).join(",");
    };
    return JSON.stringify({
      banner: document.getElementById("banner").textContent,
      chips: [...document.querySelectorAll(".chip-row button")].map((b) => b.textContent),
      lines: ["unknown-note", "traffic-note", "class-note", "a-selection", "a-claims", "d-traffic", "i-summary"]
        .map((id) => document.getElementById(id).textContent),
      boxes: [".chart-tools", ".stage", ".legend", ".detail", "#aircraft-panel", "#detail"].map(rect),
      scroll: [document.documentElement.scrollHeight, document.querySelector(".detail").scrollHeight],
    });
  });
}

// Each `?` opens on hover and on focus, closes on blur, on Escape and on a click elsewhere,
// toggles on a tap, and changes nothing else on the page while it is open.
async function exerciseHints(page, viewport, ids, touch) {
  const where = `${viewport.width}x${viewport.height} hints`;
  const isOpen = (id) => page.evaluate((hint) => {
    const tip = document.getElementById(hint).parentElement.querySelector(".hint-text");
    if (!tip.hasAttribute("data-open")) return false;
    const r = tip.getBoundingClientRect();
    return r.left >= 0 && r.top >= 0 && r.right <= innerWidth && r.bottom <= innerHeight;
  }, id);
  const expect = async (id, wanted, how) => {
    checks += 1;
    if ((await isOpen(id)) !== wanted) {
      fail(where, `#${id} ${wanted ? "did not open inside the viewport" : "did not close"} on ${how}`);
    }
  };
  // With an airport and an aircraft chosen, these sections all have something to explain.
  const expected = ["unknown-hint", "traffic-hint", "class-hint", "legend-hint", "claims-hint", "inbound-hint"];
  const absent = expected.filter((id) => !ids.includes(id));
  checks += 1;
  if (absent.length) fail(where, `no \`?\` on screen for: ${absent.join(", ")}`);
  console.log(`exercising ${ids.length} explanations at ${viewport.width}x${viewport.height}: ${ids.join(", ")}`);
  for (const id of ids) {
    const button = page.locator(`#${id}`);
    await button.scrollIntoViewIfNeeded();
    await page.mouse.move(0, 0);
    const before = await screenState(page);
    await button.focus();
    await expect(id, true, "focus");
    checks += 1;
    if ((await screenState(page)) !== before) fail(where, `#${id} changed the page around it when it opened`);
    await page.keyboard.press("Escape");
    await expect(id, false, "Escape");
    await page.keyboard.press("Tab");
    await expect(id, false, "blur");
    await button.hover();
    await expect(id, true, "hover");
    await page.mouse.move(0, 0);
    await expect(id, false, "the pointer leaving");
    await button.click();
    await expect(id, true, "a click");
    // Somewhere that is not a control, so the click changes nothing but the focus.
    await page.locator("#a-title").click();
    await expect(id, false, "a click elsewhere");
    if (touch) {
      await button.tap();
      await expect(id, true, "a tap");
      await button.tap();
      await expect(id, false, "a second tap");
    }
    checks += 1;
    if ((await screenState(page)) !== before) fail(where, `#${id} left the page changed`);
  }
}

// Sentences that moved behind a `?`: still in the page, word for word.
const MOVED = {
  "claims-why": ["A transponder broadcasts position, not intent", "asking OpenSky costs 30 credits", "nothing drawn past the last"],
  "unknown-why": ["dashed rings"],
  "traffic-why": ["an empty patch of sky means no receiver heard anything there"],
  "class-why": ["no aircraft type is available"],
  "legend-why": ["Unknown: no report heard, not good news", "zoom in to split it", "lookups in"],
  "inbound-why": ["Approaching means airborne, within 60 nm"],
};

async function checkMoved(page, where) {
  for (const [id, sentences] of Object.entries(MOVED)) {
    // Whitespace is the page source's line wrapping, not the sentence.
    const text = (await page.locator(`#${id}`).textContent()).replace(/\s+/g, " ");
    for (const sentence of sentences) {
      checks += 1;
      if (!text.includes(sentence)) fail(where, `moved text missing from #${id}: "${sentence}"`);
    }
  }
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
  // The phone has a touchscreen, so a tap is tried there: hover does not exist on it.
  const touch = viewport.width === OVERFLOW_WIDTH;
  const context = await browser.newContext({ viewport, hasTouch: touch });
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
  // Credentials are configured in this fixture, so the traffic layer starts on by itself.
  await page.locator("#traffic-on:checked").waitFor({ state: "attached" });
  await page.locator("#traffic-cost", { hasText: "This session" }).waitFor();
  await inspect(page, viewport, "airport selected, traffic on");

  // A class chip pressed: its filled style is scanned too, then the filter is let go.
  await page.locator("#class-chips button").nth(1).click();
  await page.locator("#class-chips button[aria-pressed='true']").nth(0).waitFor();
  await inspect(page, viewport, "class filter chosen");
  await page.locator("#class-chips button").first().click();

  // Every new control is reached by Tab, in the page's own order.
  await page.locator("#a-selection", { hasText: "showing" }).waitFor();
  await page.locator("#unknown-on").focus();
  const reached = new Set();
  for (let step = 0; step < 40; step += 1) {
    reached.add(await page.evaluate(() => document.activeElement && document.activeElement.id));
    await page.keyboard.press("Tab");
  }
  const controls = ["unknown-on", "unknown-hint", "traffic-hint", "class-hint", "layer-airports", "layer-aircraft", "flights-only", "weather-chip-all", "a-sort", "a-cap", "a-radius", "a-airline", "a-band", "a-phase", "a-q", "a-focus"];
  const missed = controls.filter((id) => !reached.has(id));
  checks += 1;
  if (missed.length) fail(`${size} keyboard`, `not reached by Tab: ${missed.join(", ")}`);

  // The layer switches and the weather chips: each pressed state is scanned, then let go.
  await page.locator("#weather-chips button").nth(1).click();
  await page.locator("#weather-chips button[aria-pressed='true']").nth(0).waitFor();
  await page.locator("#weather-note", { hasText: "of" }).waitFor();
  await inspect(page, viewport, "weather filter chosen");
  await page.locator("#weather-chip-all").click();
  await page.getByRole("button", { name: "Flights only" }).click();
  await page.locator('#flights-only[aria-pressed="true"]').waitFor();
  await page.locator('#layer-airports[aria-pressed="false"]').waitFor();
  await inspect(page, viewport, "flights only");
  await page.getByRole("button", { name: "Aircraft", exact: true }).click();
  await page.locator("#layer-note", { hasText: "Nothing drawn: both layers are off" }).waitFor();
  await inspect(page, viewport, "both layers off");
  await page.getByRole("button", { name: "Airports", exact: true }).click();
  await page.getByRole("button", { name: "Aircraft", exact: true }).click();
  await page.locator('#layer-aircraft[aria-pressed="true"]').waitFor();
  await page.locator("#layer-note").waitFor({ state: "hidden" });

  await page.getByLabel("Show airports without a report").check();
  await page.locator("#unknown-why", { hasText: "dashed rings" }).waitFor({ state: "attached" });
  await inspect(page, viewport, "airports without a report shown");

  // Focusing an aircraft from the keyboard: the list, not the canvas.
  await page.locator("#a-focus").selectOption("4ca1f2");
  await page.locator("#a-claims", { hasText: "observed: 8 waypoints" }).waitFor();
  await page.getByRole("button", { name: "Follow" }).click();
  await page.locator('#a-follow[aria-pressed="true"]').waitFor();
  await inspect(page, viewport, "aircraft focused and followed");
  await checkMoved(page, `${size} moved text`);
  await exerciseHints(page, viewport, await checkHints(page, `${size} hints`), touch);
  await page.keyboard.press("Escape");
  await page.locator("#a-follow-note", { hasText: "Follow released" }).waitFor();

  await page.getByRole("button", { name: /Run triage/ }).click();
  await page.locator("#t-higher li").first().waitFor();
  await page.locator("#t-counts dd").first().waitFor();
  await inspect(page, viewport, "triage");

  await page.getByLabel("Search airports").fill("Landvetter");
  await page.locator("#page-note", { hasText: "matching" }).waitFor();
  await inspect(page, viewport, "search");

  await page.getByRole("tab", { name: "Documentation" }).click();
  await page.locator("#glossary").waitFor();
  await inspect(page, viewport, "documentation");
  await page.getByRole("tab", { name: "Board" }).click();

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
  let browser = null;
  try {
    // A11Y_CHANNEL=msedge uses an installed Edge instead of Playwright's own Chromium.
    const channel = process.env.A11Y_CHANNEL;
    browser = await chromium.launch(channel ? { channel } : {});
    console.log(`axe-core ${AXE_VERSION}, Chromium ${browser.version()}`);
    for (const viewport of VIEWPORTS) await walk(browser, viewport, servers, passcode);
  } finally {
    // A browser that never started must not leave the three servers running.
    if (browser) await browser.close();
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
