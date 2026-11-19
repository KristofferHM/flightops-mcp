/* The board. It draws what the server decided and adds no judgement of its own.
   Short polls with jitter, stopped while the tab is hidden. Redraws only on change. */
(() => {
  "use strict";

  const BOARD_POLL_MS = 30_000;
  const DETAIL_POLL_MS = 120_000;
  const TRAFFIC_POLL_MS = 120_000;
  const MAX_ZOOM = 12;
  const HIT_RADIUS_PX = 14;
  // Past this many aircraft at a wide zoom, arrowheads merge into a solid shape.
  const DENSE_AIRCRAFT = 400;
  const DETAIL_ZOOM = 4;
  // Below this width the map drops its text annotations and shrinks its markers.
  const COMPACT_WIDTH = 520;
  const RING_DEGREES = [10, 20, 30, 40, 50, 60];
  const LABELLED_RINGS = [10, 20, 30];
  const RING_LABEL_BEARING = 318;
  const NM_PER_DEGREE = 60;
  const BADGE_GAP_PX = 10;
  const SELECTION_PX = 13;

  // The colours and faces live in board.css. The canvas reads the same tokens.
  const styles = getComputedStyle(document.documentElement);
  const token = (name) => styles.getPropertyValue(name).trim();

  // Only the four flight categories have a colour. Anything else is drawn as unknown.
  const CATEGORY = {
    VFR: { colour: token("--vfr"), shape: "circle", radius: 4.2, order: 0 },
    MVFR: { colour: token("--mvfr"), shape: "square", radius: 4.6, order: 1 },
    IFR: { colour: token("--ifr"), shape: "diamond", radius: 5.4, order: 2 },
    LIFR: { colour: token("--lifr"), shape: "triangle", radius: 6, order: 3 },
  };
  const UNKNOWN_RADIUS = 5.6;
  const UNKNOWN_ORDER = 4;
  const INK = {
    sea: token("--void"), land: token("--land"), coast: token("--coast"), grid: token("--grid"),
    ring: token("--ring"), tick: token("--tick"), note: token("--muted"),
    unknown: token("--unknown"), label: token("--type"), halo: token("--void"),
    focus: token("--amber"), plate: token("--panel"), aircraft: token("--type"),
  };
  const MONO = token("--font-mono");
  const SANS = token("--font-sans");
  // Shown when the server cannot be asked. An age kept on screen would go on looking current.
  const NO_ANSWER = {
    state: "no_data",
    lines: [{ label: "Board", state: "no_data", text: "no answer from the server" }],
  };

  const $ = (id) => document.getElementById(id);
  const canvas = $("map");
  const context = canvas.getContext("2d");
  const state = {
    land: null, airports: [], selected: null, detail: null, airspace: [],
    // The time of the traffic layer on screen, and OpenSky's clock when it was sent,
    // as the server gave them. Null when no layer is drawn.
    trafficSnapshot: null, trafficUpstream: null, boardRequest: 0,
    projection: "map", lon: 10, lat: 20, panY: 0, zoom: 1,
    width: 0, height: 0, dirty: true,
  };
  const timers = {};
  let projection = null;

  // --- polling -----------------------------------------------------------------
  function schedule(name, task, interval) {
    clearTimeout(timers[name]);
    if (document.hidden) return;
    const jitter = 0.85 + Math.random() * 0.3;
    timers[name] = setTimeout(async () => {
      await task();
      schedule(name, task, interval);
    }, interval * jitter);
  }

  async function api(path, options) {
    const response = await fetch(path, { credentials: "same-origin", ...options });
    if (response.status === 401) {
      location.assign("/login");
      throw new Error("sign-in required");
    }
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    return response.json();
  }

  async function refreshBoard() {
    // Only the newest request may paint, or a slow answer could restore a removed layer.
    const request = ++state.boardRequest;
    // Both times go back exactly as the server gave them; the page does no date arithmetic.
    const query = known(state.trafficSnapshot)
      ? `?traffic_snapshot=${state.trafficSnapshot}&traffic_upstream=${state.trafficUpstream || 0}`
      : "";
    try {
      const board = await api(`/api/board${query}`);
      if (request !== state.boardRequest) return;
      state.airports = board.airports;
      renderBanner(board.banner.mode, board.banner.label);
      renderLastUpdate(board.last_update);
      renderGovernance(board.governance);
      renderRegions(board.regions);
      renderStrips();
      invalidate();
    } catch {
      if (request !== state.boardRequest) return;
      renderBanner("NO DATA", "NO DATA: the board server did not answer");
      renderLastUpdate(NO_ANSWER);
    }
  }

  async function refreshDetail() {
    if (!state.selected) return;
    const icao = state.selected;
    try {
      const detail = await api(`/api/airport/${encodeURIComponent(icao)}`);
      if (icao !== state.selected) return;
      state.detail = detail;
      renderDetail(detail);
      invalidate();
      refreshBoard();
    } catch {
      $("d-traffic").textContent = "Unknown: the board server did not answer";
    }
  }

  async function refreshTraffic() {
    if (!$("traffic-on").checked) return;
    const note = $("traffic-note");
    const region = $("region").value;
    const superseded = () => !$("traffic-on").checked || region !== $("region").value;
    try {
      const layer = await api(`/api/traffic/${encodeURIComponent(region)}`);
      if (superseded()) return;
      state.airspace = layer.aircraft;
      state.trafficSnapshot = layer.snapshot_epoch;
      state.trafficUpstream = layer.upstream_epoch;
      note.textContent = layer.hint ? `${layer.summary}. ${layer.hint}` : layer.summary;
      if (layer.truncated) note.textContent += ", most recent only";
      if (layer.freshness && layer.freshness.most_recent_only) {
        note.textContent += ` (anonymous: ${layer.freshness.time_resolution_s} s positions)`;
      }
      refreshBoard();
    } catch {
      if (superseded()) return;
      state.airspace = [];
      state.trafficSnapshot = 0;
      state.trafficUpstream = 0;
      note.textContent = "Unknown: the board server did not answer";
      refreshBoard();
    }
    invalidate();
  }

  function startPolling() {
    schedule("board", refreshBoard, BOARD_POLL_MS);
    schedule("detail", refreshDetail, DETAIL_POLL_MS);
    schedule("traffic", refreshTraffic, TRAFFIC_POLL_MS);
  }

  document.addEventListener("visibilitychange", () => {
    if (document.hidden) {
      Object.values(timers).forEach(clearTimeout);
      return;
    }
    refreshBoard();
    refreshDetail();
    refreshTraffic();
    startPolling();
  });

  // --- panels ------------------------------------------------------------------
  function renderBanner(mode, label) {
    const banner = $("banner");
    banner.dataset.mode = mode;
    banner.textContent = label;
  }

  // Every word and number here was decided by the server (dashboard/board.py).
  function renderLastUpdate(view) {
    const badge = $("last-update");
    badge.dataset.state = view.state;
    badge.replaceChildren(...view.lines.map((line) => {
      const row = document.createElement("p");
      row.dataset.state = line.state;
      const source = document.createElement("span");
      source.className = "source";
      source.textContent = line.label;
      const text = document.createElement("span");
      text.textContent = line.text;
      row.append(source, " ", text);
      return row;
    }));
  }

  const known = (value) => value !== null && value !== undefined;
  const ofBudget = (spent, budget) =>
    known(spent) && known(budget) ? `${spent} of ${budget}` : "Unknown";
  const MODE_NAMES = {
    client_credentials: "Client credentials", anonymous: "Anonymous", none: "None",
  };

  function renderGovernance(g) {
    $("g-mode").textContent = MODE_NAMES[g.credentials] || "Unknown";
    $("g-hour").textContent = ofBudget(g.credits_hour, g.budget_hour);
    $("g-day").textContent = ofBudget(g.credits_24h, g.budget_24h);
    $("g-day").title = g.ledger_survives_restart === false ? "Resets when the board restarts" : "";
    $("g-cache").textContent = known(g.cache_hit_rate)
      ? `${Math.round(g.cache_hit_rate * 100)}%` : "No requests yet";
    $("g-left").textContent = known(g.upstream_remaining) ? g.upstream_remaining : "Unknown";
  }

  function renderRegions(regions) {
    const select = $("region");
    if (select.options.length) return;
    for (const region of regions) {
      const option = new Option(region.replace("_", " "), region);
      select.add(option);
    }
  }

  function markClass(marker) {
    return marker.kind === "weather" && CATEGORY[marker.category]
      ? `mark ${marker.category.toLowerCase()}` : "mark unknown";
  }

  function renderStrips() {
    const list = $("strips");
    const focused = document.activeElement && document.activeElement.dataset
      ? document.activeElement.dataset.icao : null;
    list.replaceChildren(...state.airports.map((airport) => {
      const item = document.createElement("li");
      const strip = document.createElement("button");
      strip.type = "button";
      strip.className = "strip";
      strip.dataset.icao = airport.icao;
      if (airport.icao === state.selected) strip.setAttribute("aria-current", "true");
      const chip = document.createElement("span");
      chip.className = "chip";
      const mark = document.createElement("i");
      mark.className = markClass(airport.marker);
      chip.append(mark);
      const code = document.createElement("b");
      code.textContent = airport.icao;
      const name = document.createElement("span");
      name.className = "name";
      name.textContent = airport.name;
      const weather = document.createElement("span");
      weather.className = "state";
      weather.textContent = airport.weather.summary;
      strip.append(chip, code, name, weather);
      strip.addEventListener("click", () => select(airport.icao));
      item.append(strip);
      return item;
    }));
    if (focused) {
      const again = list.querySelector(`[data-icao="${CSS.escape(focused)}"]`);
      if (again) again.focus();
    }
  }

  function fillList(element, items) {
    element.replaceChildren(...items.map((text) => {
      const item = document.createElement("li");
      item.textContent = text;
      return item;
    }));
  }

  function renderDetail(detail) {
    $("detail-empty").hidden = true;
    $("detail").hidden = false;
    $("d-title").textContent = detail.icao;
    $("d-name").textContent = detail.name;
    $("d-mark").className = markClass(detail.marker);
    $("d-weather").textContent = detail.weather.summary;
    $("d-traffic").textContent = detail.traffic.hint
      ? `${detail.traffic.summary}. ${detail.traffic.hint}` : detail.traffic.summary;
    if (!state.triaged) {
      $("d-arrivals").textContent = "Run triage to request";
      $("d-departures").textContent = "Run triage to request";
    }
    $("d-raw").textContent = detail.weather.raw || "";
    const gaps = [...detail.weather.unknowns, ...detail.traffic.unknowns];
    $("d-gaps").hidden = gaps.length === 0;
    fillList($("d-unknowns"), gaps);
  }

  async function select(icao) {
    state.selected = icao;
    state.detail = null;
    state.triaged = false;
    $("triage-result").hidden = true;
    const airport = state.airports.find((item) => item.icao === icao);
    if (airport && state.projection === "globe") {
      state.lon = airport.longitude;
      state.lat = airport.latitude;
    }
    renderStrips();
    invalidate();
    await refreshDetail();
    schedule("detail", refreshDetail, DETAIL_POLL_MS);
  }

  $("triage").addEventListener("click", async () => {
    const icao = state.selected;
    const button = $("triage");
    button.disabled = true;
    try {
      const result = await api(`/api/airport/${encodeURIComponent(icao)}/triage`, { method: "POST" });
      if (icao !== state.selected) return;
      state.triaged = true;
      $("triage-result").hidden = false;
      $("t-severity").textContent = result.label;
      fillList($("t-reasons"), [...result.reasons, ...result.unknowns]);
      $("t-silent").textContent = result.legs_without_evidence.length
        ? `Nothing observed from: ${result.legs_without_evidence.join(", ")}.` : "";
      $("d-arrivals").textContent = result.movements.arrivals;
      $("d-departures").textContent = result.movements.departures;
      refreshBoard();
    } catch {
      $("triage-result").hidden = false;
      $("t-severity").textContent = "Unknown";
      fillList($("t-reasons"), ["The board server did not answer."]);
      $("t-silent").textContent = "";
    } finally {
      button.disabled = false;
    }
  });

  // --- the map -----------------------------------------------------------------
  const invalidate = () => {
    if (state.dirty === "queued") return;
    state.dirty = "queued";
    requestAnimationFrame(draw);
  };

  const isCompact = () => state.width < COMPACT_WIDTH;

  function buildProjection() {
    const { width, height, zoom } = state;
    if (state.projection === "globe") {
      // The margin leaves room for the bearing ring outside the limb.
      const margin = isCompact() ? 14 : 34;
      return d3.geoOrthographic()
        .scale((Math.min(width, height) / 2 - margin) * zoom)
        .translate([width / 2, height / 2])
        .rotate([-state.lon, -state.lat])
        .clipAngle(90);
    }
    const scale = Math.min(width / (2 * Math.PI), height / Math.PI) * zoom;
    const reach = Math.max(0, (scale * Math.PI - height) / 2);
    state.panY = Math.max(-reach, Math.min(reach, state.panY));
    return d3.geoEquirectangular()
      .scale(scale)
      .translate([width / 2, height / 2 + state.panY])
      .rotate([-state.lon, 0]);
  }

  // The far side of the globe is not drawn, and its markers cannot be picked.
  function place(lon, lat) {
    if (!known(lon) || !known(lat)) return null;
    if (state.projection === "globe"
      && d3.geoDistance([lon, lat], [state.lon, state.lat]) > Math.PI / 2) return null;
    return projection([lon, lat]);
  }

  function shapePath(shape, x, y, r) {
    context.beginPath();
    if (shape === "circle") context.arc(x, y, r, 0, Math.PI * 2);
    else if (shape === "square") {
      const side = r * 0.88;
      context.rect(x - side, y - side, side * 2, side * 2);
    } else if (shape === "diamond") {
      context.moveTo(x, y - r * 1.3); context.lineTo(x + r * 1.3, y);
      context.lineTo(x, y + r * 1.3); context.lineTo(x - r * 1.3, y); context.closePath();
    } else {
      context.moveTo(x, y - r * 1.2); context.lineTo(x + r * 1.2, y + r * 0.95);
      context.lineTo(x - r * 1.2, y + r * 0.95); context.closePath();
    }
  }

  // Hollow and dashed, in the text colour: unknown is not a place on the weather scale.
  function drawUnknown(x, y, r) {
    context.beginPath();
    context.arc(x, y, r, 0, Math.PI * 2);
    context.setLineDash([2.3, 1.9]);
    context.strokeStyle = INK.unknown;
    context.lineWidth = 1.4;
    context.stroke();
    context.setLineDash([]);
  }

  function drawText(text, x, y, font, colour, align = "left") {
    context.font = font;
    context.textAlign = align;
    context.lineJoin = "round";
    context.lineWidth = 3;
    context.strokeStyle = INK.halo;
    context.strokeText(text, x, y);
    context.fillStyle = colour;
    context.fillText(text, x, y);
    context.textAlign = "left";
  }

  function drawAirport(airport) {
    const point = place(airport.longitude, airport.latitude);
    if (!point) return;
    const [x, y] = point;
    const scale = isCompact() ? 0.85 : 1;
    const style = airport.marker.kind === "weather" ? CATEGORY[airport.marker.category] : null;
    if (style) {
      shapePath(style.shape, x, y, style.radius * scale);
      context.fillStyle = style.colour;
      context.shadowColor = style.colour;
      context.shadowBlur = 2.5 * pixelRatio();
      context.fill();
      context.shadowBlur = 0;
      context.strokeStyle = INK.halo;
      context.lineWidth = 1;
      context.stroke();
    } else {
      drawUnknown(x, y, UNKNOWN_RADIUS * scale);
    }
    if (airport.icao !== state.selected && state.zoom >= 2.5) {
      drawText(airport.icao, x + 10, y - 8, `600 12px ${MONO}`, INK.label);
    }
  }

  const drawOrder = (airport) => {
    const style = airport.marker.kind === "weather" ? CATEGORY[airport.marker.category] : null;
    if (airport.icao === state.selected) return UNKNOWN_ORDER + 1;
    return style ? style.order : UNKNOWN_ORDER;
  };

  // The point reached from an origin along a bearing, all angles in degrees.
  function destination([lon, lat], bearing, distance) {
    const rad = Math.PI / 180;
    const d = distance * rad, b = bearing * rad, p = lat * rad;
    const q = Math.asin(Math.sin(p) * Math.cos(d) + Math.cos(p) * Math.sin(d) * Math.cos(b));
    const l = lon * rad + Math.atan2(
      Math.sin(b) * Math.sin(d) * Math.cos(p), Math.cos(d) - Math.sin(p) * Math.sin(q),
    );
    return [l / rad, q / rad];
  }

  const selectedAirport = () => state.airports.find((item) => item.icao === state.selected);

  // Range rings are true great-circle distances from the selected airport.
  function drawRangeRings(path, airport) {
    const origin = [airport.longitude, airport.latitude];
    context.strokeStyle = INK.ring;
    context.lineWidth = 1;
    for (const degrees of RING_DEGREES) {
      context.beginPath();
      path(d3.geoCircle().center(origin).radius(degrees)());
      context.stroke();
    }
    if (isCompact()) return;
    for (const degrees of LABELLED_RINGS) {
      const [lon, lat] = destination(origin, RING_LABEL_BEARING, degrees);
      const point = place(lon, lat);
      if (!point) continue;
      const text = `${degrees * NM_PER_DEGREE} NM`;
      drawText(text, point[0], point[1], `10.5px ${MONO}`, INK.note, "center");
    }
  }

  // An orthographic view keeps true bearings from its centre, which is what this ring reads.
  function drawBearingRing() {
    const radius = projection.scale();
    const [cx, cy] = projection.translate();
    context.strokeStyle = INK.tick;
    context.lineWidth = 1;
    context.beginPath();
    for (let bearing = 0; bearing < 360; bearing += 5) {
      const angle = (bearing * Math.PI) / 180;
      const dx = Math.sin(angle), dy = -Math.cos(angle);
      const outer = radius + (bearing % 30 === 0 ? 10 : 6);
      context.moveTo(cx + dx * (radius + 3), cy + dy * (radius + 3));
      context.lineTo(cx + dx * outer, cy + dy * outer);
    }
    context.stroke();
    if (isCompact()) return;
    for (let bearing = 0; bearing < 360; bearing += 30) {
      const angle = (bearing * Math.PI) / 180;
      const x = cx + Math.sin(angle) * (radius + 19);
      const y = cy - Math.cos(angle) * (radius + 19) + 3.5;
      const text = String(bearing).padStart(3, "0");
      drawText(text, x, y, `10.5px ${MONO}`, INK.note, "center");
    }
  }

  function fitted(text, room) {
    if (context.measureText(text).width <= room) return text;
    let kept = text;
    while (kept && context.measureText(`${kept}…`).width > room) kept = kept.slice(0, -1);
    return `${kept}…`;
  }

  const overlaps = (a, b) =>
    a.left < b.right && b.left < a.right && a.top < b.bottom && b.top < a.bottom;

  // The badge sits top right, and steps aside for the selected airport and its label.
  function placeBadge(keepClear) {
    const badge = $("last-update");
    if (getComputedStyle(badge).position !== "absolute") return;
    const corner = {
      left: state.width - BADGE_GAP_PX - badge.offsetWidth,
      right: state.width - BADGE_GAP_PX,
      top: BADGE_GAP_PX,
      bottom: BADGE_GAP_PX + badge.offsetHeight,
    };
    badge.dataset.side = keepClear && overlaps(keepClear, corner) ? "left" : "right";
  }

  // Corner brackets and a label plate joined by a leader line.
  function drawSelection() {
    const airport = selectedAirport();
    const point = airport && place(airport.longitude, airport.latitude);
    if (!point) { placeBadge(null); return; }
    const [x, y] = point;
    const s = SELECTION_PX, c = 5;
    context.strokeStyle = INK.focus;
    context.lineWidth = 1.75;
    context.beginPath();
    for (const [dx, dy] of [[-1, -1], [1, -1], [1, 1], [-1, 1]]) {
      context.moveTo(x + dx * s, y + dy * (s - c));
      context.lineTo(x + dx * s, y + dy * s);
      context.lineTo(x + dx * (s - c), y + dy * s);
    }
    context.stroke();

    const compact = isCompact();
    const codeFont = `700 13px ${MONO}`, nameFont = `11.5px ${SANS}`;
    context.font = nameFont;
    const name = fitted(airport.name, Math.max(40, state.width - 50));
    const nameWidth = context.measureText(name).width;
    context.font = codeFont;
    const width = Math.ceil(Math.max(context.measureText(airport.icao).width, nameWidth)) + 18;
    const height = 42;
    const clamp = (value, high) => Math.max(16, Math.min(high, value));
    const px = clamp(x + (compact ? 24 : 62), state.width - width - 16);
    const py = clamp(y + (compact ? 34 : 54), state.height - height - 16);

    const tx = x >= px && x <= px + width ? x : px;
    const angle = Math.atan2(py - y, tx - x);
    context.lineWidth = 1.25;
    context.beginPath();
    context.moveTo(x + Math.cos(angle) * s, y + Math.sin(angle) * s);
    context.lineTo(tx, py);
    context.stroke();
    context.fillStyle = INK.plate;
    context.fillRect(px, py, width, height);
    context.strokeRect(px, py, width, height);
    context.fillStyle = INK.label;
    context.font = codeFont;
    context.fillText(airport.icao, px + 9, py + 17);
    context.font = nameFont;
    context.fillText(name, px + 9, py + 33);

    placeBadge({
      left: Math.min(x - s, px),
      right: Math.max(x + s, px + width),
      top: Math.min(y - s, py),
      bottom: Math.max(y + s, py + height),
    });
  }

  // Drawn where it was last reported and faded by age. Never moved between polls.
  // White, not phosphor: on this board green means VFR.
  function drawAircraft(aircraft, dense) {
    const point = place(aircraft.longitude, aircraft.latitude);
    if (!point) return;
    context.globalAlpha = aircraft.fade;
    context.fillStyle = INK.aircraft;
    if (dense || aircraft.on_ground || !known(aircraft.track)) {
      context.fillRect(point[0] - 1.5, point[1] - 1.5, 3, 3);
    } else {
      context.save();
      context.translate(point[0], point[1]);
      context.rotate((aircraft.track * Math.PI) / 180);
      context.beginPath();
      context.moveTo(0, -5); context.lineTo(3, 3.5); context.lineTo(-3, 3.5);
      context.closePath();
      context.fill();
      context.restore();
    }
    context.globalAlpha = 1;
  }

  function draw() {
    state.dirty = false;
    if (!state.width || !state.height) return;
    projection = buildProjection();
    const globe = state.projection === "globe";
    const path = d3.geoPath(projection, context);
    context.clearRect(0, 0, state.width, state.height);
    context.beginPath(); path({ type: "Sphere" });
    context.fillStyle = INK.sea; context.fill();
    if (state.land) {
      context.beginPath(); path(state.land);
      context.fillStyle = INK.land; context.fill();
      context.strokeStyle = INK.coast; context.lineWidth = 0.8; context.stroke();
    }
    context.beginPath(); path(d3.geoGraticule10());
    context.strokeStyle = INK.grid; context.lineWidth = 0.5; context.stroke();
    if (globe) {
      context.beginPath(); path({ type: "Sphere" });
      context.strokeStyle = INK.coast; context.lineWidth = 1; context.stroke();
      const airport = selectedAirport();
      if (airport) drawRangeRings(path, airport);
      if (state.zoom === 1) drawBearingRing();
    }
    if ($("traffic-on").checked) {
      const dense = state.airspace.length > DENSE_AIRCRAFT && state.zoom < DETAIL_ZOOM;
      state.airspace.forEach((aircraft) => drawAircraft(aircraft, dense));
    }
    if (state.detail) state.detail.traffic.aircraft.forEach((a) => drawAircraft(a, false));
    [...state.airports].sort((a, b) => drawOrder(a) - drawOrder(b)).forEach(drawAirport);
    drawSelection();
  }

  function pixelRatio() {
    return Math.min(window.devicePixelRatio || 1, 2);
  }

  function resize() {
    const box = canvas.parentElement.getBoundingClientRect();
    const ratio = pixelRatio();
    state.width = box.width;
    state.height = box.height;
    canvas.width = Math.round(box.width * ratio);
    canvas.height = Math.round(box.height * ratio);
    context.setTransform(ratio, 0, 0, ratio, 0, 0);
    invalidate();
  }

  function nearest(x, y) {
    let best = null;
    let bestDistance = HIT_RADIUS_PX;
    for (const airport of state.airports) {
      const point = place(airport.longitude, airport.latitude);
      if (!point) continue;
      const distance = Math.hypot(point[0] - x, point[1] - y);
      if (distance <= bestDistance) { best = airport; bestDistance = distance; }
    }
    return best;
  }

  let drag = null;
  canvas.addEventListener("pointerdown", (event) => {
    canvas.setPointerCapture(event.pointerId);
    drag = { x: event.offsetX, y: event.offsetY, moved: false };
    canvas.classList.add("dragging");
    $("tip").hidden = true;
  });
  canvas.addEventListener("pointermove", (event) => {
    if (!drag) {
      const airport = projection && nearest(event.offsetX, event.offsetY);
      const tip = $("tip");
      tip.hidden = !airport;
      if (airport) {
        tip.textContent = `${airport.icao}: ${airport.marker.label}. ${airport.weather.summary}`;
        tip.style.left = `${Math.min(event.offsetX + 12, state.width - 200)}px`;
        tip.style.top = `${event.offsetY + 12}px`;
      }
      return;
    }
    const dx = event.offsetX - drag.x;
    const dy = event.offsetY - drag.y;
    if (Math.abs(dx) + Math.abs(dy) > 3) drag.moved = true;
    drag.x = event.offsetX;
    drag.y = event.offsetY;
    const perPixel = 180 / (Math.PI * projection.scale());
    state.lon = ((state.lon - dx * perPixel + 540) % 360) - 180;
    if (state.projection === "globe") {
      state.lat = Math.max(-89, Math.min(89, state.lat + dy * perPixel));
    } else {
      state.panY += dy;
    }
    invalidate();
  });
  function endDrag(event) {
    if (drag && !drag.moved) {
      const airport = nearest(event.offsetX, event.offsetY);
      if (airport) select(airport.icao);
    }
    drag = null;
    canvas.classList.remove("dragging");
  }
  canvas.addEventListener("pointerup", endDrag);
  canvas.addEventListener("pointercancel", () => { drag = null; canvas.classList.remove("dragging"); });
  canvas.addEventListener("pointerleave", () => { $("tip").hidden = true; });
  canvas.addEventListener("wheel", (event) => {
    event.preventDefault();
    const factor = event.deltaY < 0 ? 1.2 : 1 / 1.2;
    state.zoom = Math.max(1, Math.min(MAX_ZOOM, state.zoom * factor));
    invalidate();
  }, { passive: false });

  document.querySelectorAll('input[name="projection"]').forEach((input) => {
    input.addEventListener("change", () => {
      state.projection = input.value;
      state.zoom = 1;
      state.panY = 0;
      invalidate();
    });
  });
  $("traffic-on").addEventListener("change", () => {
    if ($("traffic-on").checked) { refreshTraffic(); return; }
    state.airspace = [];
    state.trafficSnapshot = null;
    state.trafficUpstream = null;
    $("traffic-note").textContent = "";
    refreshBoard();
    invalidate();
  });
  $("region").addEventListener("change", refreshTraffic);

  // --- start -------------------------------------------------------------------
  new ResizeObserver(resize).observe(canvas.parentElement);
  fetch("/static/vendor/land-110m.json")
    .then((response) => response.json())
    .then((world) => {
      state.land = topojson.feature(world, world.objects.land);
      invalidate();
    })
    .catch(() => { $("traffic-note").textContent = "The coastline file did not load."; });
  refreshBoard();
  startPolling();
})();
