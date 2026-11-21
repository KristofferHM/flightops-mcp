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

// --- the airport's four exits ------------------------------------------------------------
// Each is pressed for real. Each must end in the one clear, which names the exit on the status
// line, and leave nothing of the airport: no panel, no current strip, no approaching list, no
// approach lines (their legend is shown only while they are drawn), and a traffic selection
// asked for again without the airport.
async function exerciseAirportExits(page, size) {
  const where = `${size} airport exits`;
  const regionWide = () => page.waitForRequest(
    (request) => /\/api\/traffic\/[^/]+\/selection\?/.test(request.url()) && !request.url().includes("airport="),
    { timeout: 10_000 },
  ).then(() => true, () => false);
  const exit = async (how, icao, action) => {
    const reread = regionWide();
    await action();
    checks += 1;
    try {
      await page.locator("#map-status", { hasText: `Airport cleared (${how}): ${icao}.` }).waitFor({ timeout: 10_000 });
    } catch {
      fail(where, `${how} did not clear ${icao}; the status line says "${await page.locator("#map-status").textContent()}"`);
      return;
    }
    const left = await page.evaluate(() => ({
      panel: !document.getElementById("detail").hidden,
      empty: !document.getElementById("detail-empty").hidden,
      current: document.querySelectorAll("#strips [aria-current]").length,
      inbound: document.getElementById("i-list").children.length,
      lines: !document.getElementById("inbound-legend").hidden,
      triage: !document.getElementById("triage-result").hidden,
    }));
    const kept = [
      left.panel && "the panel", !left.empty && "no empty-state text", left.current && "a current strip",
      left.inbound && "the approaching list", left.lines && "the approach lines", left.triage && "the triage",
    ].filter(Boolean);
    checks += 1;
    if (kept.length) fail(where, `${how} left ${kept.join(", ")}`);
    checks += 1;
    if (!(await reread)) fail(where, `${how}: the traffic selection was not asked for again without the airport`);
  };
  const strip = page.locator("#strips button").first();
  const icao = await strip.getAttribute("data-icao");
  const choose = async () => {
    await strip.click();
    await page.locator("#detail:not([hidden]) #d-title", { hasText: icao }).waitFor();
  };
  const chosen = await page.locator("#d-title").textContent();

  // Escape: the aircraft first, then the airport. One press undoes one thing.
  await page.keyboard.press("Escape");
  await page.locator("#map-status", { hasText: "Selection cleared (Escape)" }).waitFor();
  checks += 1;
  if (await page.locator("#detail").isHidden()) fail(where, "one Escape let go of the aircraft and the airport together");
  await exit("Escape", chosen, () => page.keyboard.press("Escape"));

  // The selected strip pressed again: a toggle.
  await choose();
  await exit("selected again", icao, () => strip.click());

  // The panel's Close, for a phone with no Escape key. The keyboard lands on the strip.
  await choose();
  await exit("the Close button", icao, () => page.locator("#d-close").click());
  checks += 1;
  if ((await page.evaluate(() => document.activeElement.dataset.icao)) !== icao) {
    fail(where, "after Close the keyboard focus did not return to the airport's strip");
  }

  // A click on empty map: the bottom of the map is the far south, where this region has nothing.
  await choose();
  const map = page.locator("#map");
  await map.scrollIntoViewIfNeeded();
  // The pointer leaves first, so the hover tip is not left over the spot.
  await page.mouse.move(0, 0);
  const box = await map.boundingBox();
  // The first point along the bottom row that nothing else covers: at 390 px the badge and
  // the hover tip can sit over part of it.
  const spot = await page.evaluate(({ x, y, width }) => {
    for (const share of [0.5, 0.3, 0.7, 0.15, 0.85]) {
      const at = { x: x + width * share, y };
      if (document.elementFromPoint(at.x, at.y)?.id === "map") return at;
    }
    return null;
  }, { x: box.x, y: box.y + box.height - 6, width: box.width });
  checks += 1;
  if (!spot) { fail(where, "every empty-map spot is covered by another element"); return; }
  await exit("empty map", icao, () => page.mouse.click(spot.x, spot.y));
}

// An answer that lands after its airport was let go, or after another was chosen, must not
// paint. Each is held back by the route, and the exit or the switch happens while it waits.
const LATE_MS = 1500;
async function checkLateAnswers(page, size) {
  const where = `${size} late answers`;
  const late = (match, answer) => page.route(match, async (route) => {
    await new Promise((done) => setTimeout(done, LATE_MS));
    if (answer) await route.fulfill(answer);
    else await route.continue().catch(() => {});
  });
  const settle = () => page.waitForTimeout(LATE_MS + 1000);
  const broken = { status: 500, contentType: "application/json", body: "{}" };
  await page.getByLabel("Search airports").fill("");
  await page.waitForFunction(() => !document.getElementById("page-note").textContent.includes("matching"));
  const strips = page.locator("#strips button");
  const [a, b] = [await strips.nth(0).getAttribute("data-icao"), await strips.nth(1).getAttribute("data-icao")];
  const choose = async (icao) => {
    await page.locator(`#strips [data-icao="${icao}"]`).click();
    await page.locator("#detail:not([hidden]) #d-title", { hasText: icao }).waitFor();
  };
  const detail = (icao) => (url) => url.pathname === `/api/airport/${icao}`;
  const [detailA, detailB] = [detail(a), detail(b)];

  // A failed detail for A lands while B is on screen.
  await late(detailA, broken);
  await page.locator(`#strips [data-icao="${a}"]`).click();
  await choose(b);
  await settle();
  await page.unroute(detailA);
  checks += 1;
  if ((await page.locator("#d-traffic").textContent()).includes("did not answer")) {
    fail(where, `a failed detail for ${a} was painted into ${b}'s panel`);
  }

  // A failed triage for B lands after A was chosen.
  const triage = (url) => url.pathname === `/api/airport/${b}/triage`;
  await late(triage, broken);
  await page.locator("#triage").click();
  await choose(a);
  await settle();
  await page.unroute(triage);
  checks += 1;
  if (await page.locator("#triage-result").isVisible()) fail(where, `a failed triage for ${b} was painted into ${a}'s panel`);

  // B's detail lands after B was let go.
  await late(detailB);
  await page.locator(`#strips [data-icao="${b}"]`).click();
  await page.keyboard.press("Escape");
  await settle();
  await page.unroute(detailB);
  checks += 1;
  if (await page.locator("#detail").isVisible()) fail(where, `${b}'s detail repainted the panel after the clear`);

  // A traffic refresh asked for A lands after A was let go: its airport-centred selection is
  // not painted; the region-wide one is read instead.
  await choose(a);
  const traffic = (url) => url.pathname === "/api/traffic/europe";
  await late(traffic);
  const landed = page.waitForResponse((response) => traffic(new URL(response.url())), { timeout: 10_000 });
  await page.locator("#traffic-on").uncheck();
  await page.locator("#traffic-on").check();
  await page.keyboard.press("Escape");
  await landed.catch(() => {});
  await page.waitForTimeout(1000);
  await page.unroute(traffic);
  const after = await page.evaluate(() => ({
    selection: document.getElementById("a-selection").textContent,
    inbound: document.getElementById("i-list").children.length,
    lines: !document.getElementById("inbound-legend").hidden,
  }));
  checks += 1;
  if (!after.selection.includes("No airport is selected.") || after.inbound || after.lines) {
    fail(where, `a traffic answer asked for ${a} repainted its selection after the clear: "${after.selection}"`);
  }
}

// --- the traffic timeline ------------------------------------------------------------------
// The finding behind "flights do not update while an airport is selected": nothing was
// broken; the layer refreshes every `interval_s` (4 minutes at the default ceiling) and the
// page did not say so. Encoded here on a fake clock: after an airport is chosen, the page
// states the interval, its countdown is the armed timer's, exactly one refresh goes out when
// it reaches zero, it still asks for the airport, and the layer it brings is shown as new.
async function checkTrafficTimeline(browser, servers, passcode) {
  const where = "traffic timeline";
  const viewport = { width: 1280, height: 720 };
  const context = await browser.newContext({ viewport });
  await context.route("**/*", (route) => {
    const url = new URL(route.request().url());
    if (url.hostname === LOCAL_HOST) return route.continue();
    fail(where, `a request left this machine: ${url.origin}`);
    return route.abort();
  });
  const page = await context.newPage();
  page.on("pageerror", (error) => fail(where, `page error: ${error.message}`));
  await page.clock.install();
  const asked = [];
  page.on("request", (request) => {
    const url = new URL(request.url());
    if (url.pathname === "/api/traffic/europe") asked.push(url.searchParams.get("airport"));
  });
  const countdown = async () => {
    const text = await page.locator("#traffic-clock").textContent();
    const found = /next refresh in (\d+):(\d\d)/.exec(text);
    return found ? Number(found[1]) * 60 + Number(found[2]) : null;
  };
  const refreshes = async (count) => {
    for (let wait = 0; wait < 50 && asked.length < count; wait += 1) await page.waitForTimeout(100);
    return asked.length;
  };
  try {
    await open(page, servers.data.base, passcode, viewport);
    await page.locator("#traffic-clock", { hasText: "next refresh in" }).waitFor({ timeout: 10_000 });
    const interval = await page.evaluate(
      () => fetch("/api/board?region=europe").then((r) => r.json()).then((board) => board.traffic.interval_s),
    );
    const strip = page.locator("#strips button").first();
    const icao = await strip.getAttribute("data-icao");
    await strip.click();
    await page.locator("#d-title", { hasText: icao }).waitFor();
    const minutes = `about every ${interval / 60} minutes`;
    checks += 1;
    if (!(await page.locator("#traffic-cost").textContent()).includes(minutes)) {
      fail(where, `the toggle does not say the layer refreshes ${minutes}`);
    }
    // Twice: the first wait is the 120 s floor, armed before the server named its interval;
    // every wait after it is the server's, jittered by up to 15%.
    for (const [round, longest] of [[1, 120], [2, interval]]) {
      const left = await countdown();
      const before = asked.length;
      checks += 1;
      if (left === null) { fail(where, `round ${round}: no countdown on the traffic line`); return; }
      if (left > longest * 1.15 + 5) fail(where, `round ${round}: next refresh in ${left} s, over ${longest} s + jitter`);
      await page.clock.runFor(Math.max(0, left - 3) * 1000);
      // A short real wait: the installed clock also flows naturally, so a long one would
      // itself carry the countdown past zero.
      await page.waitForTimeout(300);
      checks += 1;
      if (asked.length !== before) fail(where, `round ${round}: a refresh went out before the countdown ended`);
      await page.clock.runFor(8_000);
      checks += 1;
      if ((await refreshes(before + 1)) !== before + 1) {
        fail(where, `round ${round}: ${asked.length - before} refreshes when the countdown ended, not 1`);
      }
      checks += 1;
      if (asked.at(-1) !== icao) fail(where, `round ${round}: the refresh did not ask for ${icao}`);
      await page.locator("#traffic-clock", { hasText: /^heard \d+ s ago · next refresh in/ }).waitFor({ timeout: 5_000 })
        .catch(() => fail(where, `round ${round}: the new layer is not shown as just heard`));
    }
    console.log(`checked ${where}: ${asked.length} refreshes, interval ${interval} s`);
  } catch (error) {
    fail(where, error.message.split("\n")[0]);
  } finally {
    await context.close();
  }
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

  await exerciseAirportExits(page, size);
  await inspect(page, viewport, "airport cleared");
  await checkLateAnswers(page, size);

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
    await checkTrafficTimeline(browser, servers, passcode);
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
