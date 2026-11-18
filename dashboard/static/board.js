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

  // Only the four flight categories have a colour. Anything else is drawn as unknown.
  const CATEGORY = {
    VFR: { colour: "#2fa873", shape: "circle" },
    MVFR: { colour: "#3f90dc", shape: "square" },
    IFR: { colour: "#e5533f", shape: "diamond" },
    LIFR: { colour: "#c95fd0", shape: "triangle" },
  };
  const INK = {
    sea: "#11222d", land: "#1b3140", coast: "#3b5a6a", grid: "#1c3341",
    unknown: "#9aa7ab", label: "#e4ebe6", halo: "#0d1b24", focus: "#f3d36b",
    aircraft: "#f4e9b8",
  };

  const $ = (id) => document.getElementById(id);
  const canvas = $("map");
  const context = canvas.getContext("2d");
  const state = {
    land: null, airports: [], selected: null, detail: null, airspace: [],
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
    try {
      const board = await api("/api/board");
      state.airports = board.airports;
      renderBanner(board.banner.mode, board.banner.label);
      renderGovernance(board.governance);
      renderRegions(board.regions);
      renderStrips();
      invalidate();
    } catch {
      renderBanner("NO DATA", "NO DATA: the board server did not answer");
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
    try {
      const layer = await api(`/api/traffic/${encodeURIComponent($("region").value)}`);
      state.airspace = layer.aircraft;
      note.textContent = layer.hint ? `${layer.summary}. ${layer.hint}` : layer.summary;
      if (layer.truncated) note.textContent += ", most recent only";
      if (layer.freshness && layer.freshness.most_recent_only) {
        note.textContent += ` (anonymous: ${layer.freshness.time_resolution_s} s positions)`;
      }
      refreshBoard();
    } catch {
      state.airspace = [];
      note.textContent = "Unknown: the board server did not answer";
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

  function buildProjection() {
    const { width, height, zoom } = state;
    if (state.projection === "globe") {
      return d3.geoOrthographic()
        .scale((Math.min(width, height) / 2 - 8) * zoom)
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
    else if (shape === "square") context.rect(x - r, y - r, r * 2, r * 2);
    else if (shape === "diamond") {
      context.moveTo(x, y - r * 1.3); context.lineTo(x + r * 1.3, y);
      context.lineTo(x, y + r * 1.3); context.lineTo(x - r * 1.3, y); context.closePath();
    } else {
      context.moveTo(x, y - r * 1.3); context.lineTo(x + r * 1.2, y + r);
      context.lineTo(x - r * 1.2, y + r); context.closePath();
    }
  }

  function drawUnknown(x, y, r) {
    context.save();
    context.beginPath();
    context.arc(x, y, r, 0, Math.PI * 2);
    context.fillStyle = INK.sea;
    context.fill();
    context.clip();
    context.strokeStyle = INK.unknown;
    context.lineWidth = 1;
    context.beginPath();
    for (let offset = -2 * r; offset <= 2 * r; offset += 3.5) {
      context.moveTo(x + offset - r, y + r);
      context.lineTo(x + offset + r, y - r);
    }
    context.stroke();
    context.restore();
    context.beginPath();
    context.arc(x, y, r, 0, Math.PI * 2);
    context.strokeStyle = INK.unknown;
    context.lineWidth = 1.5;
    context.stroke();
  }

  function drawAirport(airport) {
    const point = place(airport.longitude, airport.latitude);
    if (!point) return;
    const [x, y] = point;
    const selected = airport.icao === state.selected;
    const style = airport.marker.kind === "weather" ? CATEGORY[airport.marker.category] : null;
    if (style) {
      shapePath(style.shape, x, y, 5.5);
      context.fillStyle = style.colour;
      context.fill();
      context.strokeStyle = INK.halo;
      context.lineWidth = 1.5;
      context.stroke();
    } else {
      drawUnknown(x, y, 6);
    }
    if (selected) {
      context.beginPath();
      context.arc(x, y, 11, 0, Math.PI * 2);
      context.strokeStyle = INK.focus;
      context.lineWidth = 2;
      context.stroke();
    }
    if (selected || state.zoom >= 2.5) {
      context.font = "600 12px Bahnschrift, 'Arial Narrow', sans-serif";
      context.lineWidth = 3;
      context.strokeStyle = INK.halo;
      context.strokeText(airport.icao, x + 10, y - 8);
      context.fillStyle = INK.label;
      context.fillText(airport.icao, x + 10, y - 8);
    }
  }

  // Drawn where it was last reported and faded by age. Never moved between polls.
  function drawAircraft(aircraft, dense) {
    const point = place(aircraft.longitude, aircraft.latitude);
    if (!point) return;
    context.globalAlpha = aircraft.fade;
    context.fillStyle = INK.aircraft;
    if (dense || aircraft.on_ground || !known(aircraft.track)) {
      context.fillRect(point[0] - 1, point[1] - 1, 2, 2);
    } else {
      context.save();
      context.translate(point[0], point[1]);
      context.rotate((aircraft.track * Math.PI) / 180);
      context.beginPath();
      context.moveTo(0, -4); context.lineTo(2.6, 3); context.lineTo(-2.6, 3);
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
    const path = d3.geoPath(projection, context);
    context.clearRect(0, 0, state.width, state.height);
    context.beginPath(); path({ type: "Sphere" });
    context.fillStyle = INK.sea; context.fill();
    context.beginPath(); path(d3.geoGraticule10());
    context.strokeStyle = INK.grid; context.lineWidth = 0.5; context.stroke();
    if (state.land) {
      context.beginPath(); path(state.land);
      context.fillStyle = INK.land; context.fill();
      context.strokeStyle = INK.coast; context.lineWidth = 0.6; context.stroke();
    }
    if ($("traffic-on").checked) {
      const dense = state.airspace.length > DENSE_AIRCRAFT && state.zoom < DETAIL_ZOOM;
      state.airspace.forEach((aircraft) => drawAircraft(aircraft, dense));
    }
    if (state.detail) state.detail.traffic.aircraft.forEach((a) => drawAircraft(a, false));
    state.airports.forEach(drawAirport);
  }

  function resize() {
    const box = canvas.parentElement.getBoundingClientRect();
    const ratio = Math.min(window.devicePixelRatio || 1, 2);
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
    if ($("traffic-on").checked) refreshTraffic();
    else { state.airspace = []; $("traffic-note").textContent = ""; invalidate(); }
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
