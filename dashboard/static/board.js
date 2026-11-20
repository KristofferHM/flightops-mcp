/* The board. It draws what the server decided and adds no judgement of its own.
   Short polls with jitter, stopped while the tab is hidden. Redraws only on change. */
(() => {
  "use strict";

  const BOARD_POLL_MS = 30_000;
  const DETAIL_POLL_MS = 120_000;
  // The fastest the traffic layer refreshes. The server may ask for slower (`cost.interval_s`).
  const TRAFFIC_POLL_MS = 120_000;
  const LIST_PAGE = 25;
  const SEARCH_DELAY_MS = 250;
  // A moved view asks for its own airports' weather once it has been still this long.
  const VIEW_DELAY_MS = 700;
  const REGION_KEY = "airport-board.region";
  const UNKNOWN_KEY = "airport-board.unknown";
  const AIRCRAFT_KEY = "airport-board.aircraft";
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
  // Positions heard while the page is open, per aircraft, for a focused aircraft's trail.
  const TRAIL_POINTS = 120;
  const TRAIL_AIRCRAFT = 3000;
  const DIMMED = 0.3;
  // Approach lines. Projected is dashed and thinner than any observed track, which is solid.
  const PROJECTED_DASH = [4, 4];
  const PROJECTED_WIDTH = 1;
  const OBSERVED_WIDTH = 1.8;
  const LIT_EXTRA = 1;
  const FADED = 0.25;
  const INBOUND_RING_PX = 8;
  const INBOUND_OFF = "Turn traffic on to see what is approaching. It reads the traffic layer "
    + "the board already holds, so it costs nothing.";

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
  const CHOICE_DEFAULTS = { sort: "distance", cap: "200", radius_nm: "", airline: "", band: "", phase: "" };

  const $ = (id) => document.getElementById(id);
  const canvas = $("map");
  const context = canvas.getContext("2d");
  const state = {
    land: null, airports: [], selected: null, detail: null, airspace: [],
    // The time of the traffic layer on screen, and OpenSky's clock when it was sent,
    // as the server gave them. Null when no layer is drawn.
    trafficSnapshot: null, trafficUpstream: null, boardRequest: 0, listRequest: 0,
    selectionRequest: 0,
    projection: "map", lon: 10, lat: 20, panY: 0, zoom: 1,
    width: 0, height: 0, dirty: true,
    region: remembered(REGION_KEY, "europe"), offset: 0, query: "",
    // Whether the traffic checkbox was set by the server's default or by the person.
    trafficChosen: false, trafficIntervalMs: TRAFFIC_POLL_MS, trafficStopped: false,
    // The wide view: clusters, the unknown toggle (null until the reader chooses), the view sent.
    display: null, clusters: {}, hidden: null, viewSent: null,
    showUnknown: rememberedToggle(),
    // Aircraft: the reader's choices, the newest selection, the focus and its follower.
    choice: rememberedChoice(), aircraftQuery: "", selection: null,
    focus: null, focusData: null, follow: false, followHeard: null, followNote: "",
    track: null, trails: new Map(),
    // What approaches the selected airport: the server's set, the one lit, and tracks paid for.
    inbound: null, inboundHover: null, inboundTracks: new Map(),
    // Everything drawn that can be pointed at, rebuilt each frame, so a click hits what is seen.
    pickables: [], labels: [],
  };
  const timers = {};
  let projection = null;

  // Per-browser conveniences. Storage can be missing or refused; the page works without it.
  function remembered(key, fallback) {
    try { return localStorage.getItem(key) || fallback; } catch { return fallback; }
  }
  function remember(key, value) {
    try { localStorage.setItem(key, value); } catch { /* the choice lasts this page */ }
  }
  function rememberedToggle() {
    const value = remembered(UNKNOWN_KEY, "");
    return value === "1" ? true : value === "0" ? false : null;
  }
  function rememberedChoice() {
    try {
      const saved = JSON.parse(remembered(AIRCRAFT_KEY, "{}"));
      return { ...CHOICE_DEFAULTS, ...(saved && typeof saved === "object" ? saved : {}) };
    } catch { return { ...CHOICE_DEFAULTS }; }
  }

  // --- polling -----------------------------------------------------------------
  function schedule(name, task, interval) {
    clearTimeout(timers[name]);
    if (document.hidden) return;
    const jitter = 0.85 + Math.random() * 0.3;
    const wait = typeof interval === "function" ? interval() : interval;
    timers[name] = setTimeout(async () => {
      await task();
      schedule(name, task, interval);
    }, wait * jitter);
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

  // The part of the sphere on screen, as a centre and an angular radius. Rounded, so a
  // still view sends the same query and the server's weather cache answers it.
  function viewQuery() {
    if (!projection || !state.width) return "";
    const degrees = 180 / Math.PI;
    let lat = state.lat, lon = state.lon, radius;
    if (state.projection === "globe") {
      const reach = Math.hypot(state.width, state.height) / 2 / projection.scale();
      radius = reach >= 1 ? 90 : Math.asin(reach) * degrees;
    } else {
      const clampLat = ([x, y]) => [x, Math.max(-90, Math.min(90, y))];
      const centre = clampLat(projection.invert([state.width / 2, state.height / 2]));
      [lon, lat] = centre;
      radius = 0;
      const { width: w, height: h } = state;
      for (const corner of [[0, 0], [w, 0], [0, h], [w, h], [0, h / 2], [w, h / 2], [w / 2, 0], [w / 2, h]]) {
        radius = Math.max(radius, d3.geoDistance(centre, clampLat(projection.invert(corner))) * degrees);
      }
    }
    const step = (value) => (Math.round(value * 2) / 2).toFixed(1);
    lon = ((lon + 540) % 360) - 180;
    radius = Math.min(180, Math.max(0.5, radius));
    return `&lat=${step(lat)}&lon=${step(lon)}&radius=${step(radius)}`;
  }

  function viewChanged() {
    clearTimeout(timers.view);
    timers.view = setTimeout(() => {
      if (viewQuery() !== state.viewSent) refreshBoard();
    }, VIEW_DELAY_MS);
  }

  async function refreshBoard() {
    // Only the newest request may paint, or a slow answer could restore a removed layer.
    const request = ++state.boardRequest;
    // Both times go back exactly as the server gave them; the page does no date arithmetic.
    const region = state.region;
    const query = known(state.trafficSnapshot)
      ? `&traffic_snapshot=${state.trafficSnapshot}&traffic_upstream=${state.trafficUpstream || 0}`
      : "";
    const view = viewQuery();
    state.viewSent = view;
    try {
      const board = await api(`/api/board?region=${encodeURIComponent(region)}${query}${view}`);
      if (request !== state.boardRequest || region !== state.region) return;
      state.airports = board.airports;
      state.clusters = board.clusters || {};
      state.hidden = board.hidden;
      state.display = board.display;
      $("view-note").textContent = board.viewport ? board.viewport.text : "";
      renderBanner(board.banner.mode, board.banner.label);
      renderLastUpdate(board.last_update);
      renderGovernance(board.governance);
      renderRegions(board.regions, board.region);
      $("coverage").textContent = board.coverage;
      renderTrafficControl(board.traffic);
      renderUnknownToggle();
      refreshList();
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

  // One page of the region's airports. The server filters, so the page never holds them all.
  async function refreshList() {
    const request = ++state.listRequest;
    const params = new URLSearchParams({
      region: state.region, q: state.query, offset: String(state.offset), limit: String(LIST_PAGE),
    });
    try {
      const page = await api(`/api/airports?${params}`);
      if (request !== state.listRequest) return;
      state.offset = page.offset;
      renderStrips(page.items);
      const first = page.total ? page.offset + 1 : 0;
      const last = page.offset + page.items.length;
      $("page-note").textContent = page.query
        ? `${first}–${last} of ${page.total} matching, ${page.region_total} in region`
        : `${first}–${last} of ${page.total}`;
      $("page-prev").disabled = page.offset === 0;
      $("page-next").disabled = last >= page.total;
    } catch {
      if (request !== state.listRequest) return;
      $("page-note").textContent = "The board server did not answer.";
    }
  }

  // The reader's aircraft choices as the server reads them. The view is never among them:
  // following an aircraft does not change what is fetched.
  function selectionParams() {
    const params = new URLSearchParams();
    if (state.selected) params.set("airport", state.selected);
    for (const key of ["sort", "cap", "radius_nm", "airline", "band", "phase"]) {
      if (state.choice[key]) params.set(key, state.choice[key]);
    }
    if (state.aircraftQuery) params.set("q", state.aircraftQuery);
    if (state.focus) params.set("focus", state.focus);
    return params.toString();
  }

  async function refreshTraffic() {
    if (!$("traffic-on").checked || state.trafficStopped) return;
    const note = $("traffic-note");
    const region = state.region;
    const superseded = () => !$("traffic-on").checked || region !== state.region;
    try {
      const layer = await api(`/api/traffic/${encodeURIComponent(region)}?${selectionParams()}`);
      if (superseded()) return;
      if (layer.cost) {
        renderTrafficCost(layer.cost);
        state.trafficIntervalMs = Math.max(TRAFFIC_POLL_MS, layer.cost.interval_s * 1000);
      }
      if (layer.refused) {
        // A guard said no. Stop asking; the badge and this note say why.
        state.trafficStopped = true;
        clearTimeout(timers.traffic);
        note.textContent = layer.refused;
        refreshBoard();
        return;
      }
      applyLayer(layer);
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

  // The held layer under new choices. Free: the server asks no upstream for it.
  async function refreshSelection() {
    if (!$("traffic-on").checked) return;
    const request = ++state.selectionRequest;
    const region = state.region;
    try {
      const layer = await api(`/api/traffic/${encodeURIComponent(region)}/selection?${selectionParams()}`);
      if (request !== state.selectionRequest || region !== state.region) return;
      if (layer.selection) applyLayer(layer);
    } catch {
      if (request !== state.selectionRequest) return;
      $("a-selection").textContent = "The board server did not answer.";
    }
    invalidate();
  }

  function applyLayer(layer) {
    state.airspace = layer.aircraft || [];
    state.selection = layer.selection;
    collectTrails(state.airspace);
    renderSelection(layer.selection);
    applyFocus(layer.selection ? layer.selection.focus : null);
    // An answer asked for before the airport changed describes the old airport: not drawn.
    const inbound = layer.selection ? layer.selection.inbound : null;
    renderInbound(inbound && inbound.airport === state.selected ? inbound : null);
  }

  function startPolling() {
    schedule("board", refreshBoard, BOARD_POLL_MS);
    schedule("detail", refreshDetail, DETAIL_POLL_MS);
    schedule("traffic", refreshTraffic, () => state.trafficIntervalMs);
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
  // OurAirports' two kinds on this board, kept so a reader can tell a hub from a regional field.
  const TYPE_NAMES = { large_airport: "Large", medium_airport: "Medium" };
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

  // Each choice says how many airports it will draw. Rebuilt only when that changes.
  function renderRegions(regions, chosen) {
    const select = $("region");
    const labels = regions.map((region) => `${region.label} (${region.airports})`);
    const current = [...select.options].map((option) => option.textContent);
    if (labels.join("|") !== current.join("|")) {
      select.replaceChildren(...regions.map((region, index) => new Option(labels[index], region.id)));
    }
    select.value = chosen;
  }

  function renderTrafficCost(cost) {
    $("traffic-cost").textContent =
      `This session: ${cost.session_total} credits · ${cost.hour_spent} of ${cost.hour_ceiling} `
      + `this hour · refresh every ${Math.round(cost.interval_s / 60)} min`;
  }

  // On by default only with credentials. Without, it stays off and says why.
  function renderTrafficControl(traffic) {
    const box = $("traffic-on");
    box.disabled = !traffic.available;
    renderTrafficCost(traffic);
    state.trafficIntervalMs = Math.max(TRAFFIC_POLL_MS, traffic.interval_s * 1000);
    if (state.trafficChosen) return;
    state.trafficChosen = true;
    if (!traffic.on) {
      $("traffic-note").textContent = traffic.reason;
      return;
    }
    box.checked = true;
    $("traffic-note").textContent = traffic.reason;
    refreshTraffic();
  }

  // Off at wide zoom and on when zoomed in tight, until the reader chooses; then theirs.
  // A render filter only: the hidden airports are still in the payload, and still counted.
  function unknownShown() {
    if (state.showUnknown !== null) return state.showUnknown;
    return state.display ? state.zoom >= state.display.unknown_shown_from_zoom : false;
  }

  // Runs every frame, because the default follows the zoom; it writes only on a change.
  function renderUnknownToggle() {
    const shown = unknownShown();
    const box = $("unknown-on");
    if (box.checked !== shown) box.checked = shown;
    if (!state.hidden) return;
    const text = shown ? state.hidden.shown_text : state.hidden.text;
    const note = $("unknown-note");
    if (note.textContent !== text) note.textContent = text;
  }

  function markClass(marker) {
    return marker && marker.kind === "weather" && CATEGORY[marker.category]
      ? `mark ${marker.category.toLowerCase()}` : "mark unknown";
  }

  function renderStrips(items) {
    const list = $("strips");
    const focused = document.activeElement && document.activeElement.dataset
      ? document.activeElement.dataset.icao : null;
    list.replaceChildren(...items.map((airport) => {
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
      weather.textContent = `${TYPE_NAMES[airport.type] || "Airport"} · ${airport.weather.summary}`;
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

  // Label and text pairs, as the server worded them.
  function fillFacts(element, rows) {
    element.replaceChildren(...rows.flatMap((row) => {
      const term = document.createElement("dt");
      term.textContent = row.label;
      const value = document.createElement("dd");
      value.textContent = row.text;
      if (row.state) value.dataset.state = row.state;
      if (row.mono) value.className = "mono";
      return [term, value];
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
    // Approach lines belong to one airport. They are redrawn when its selection answers.
    state.inboundTracks.clear();
    $("i-tracks-note").textContent = "";
    $("i-board-note").textContent = "";
    renderInbound(null);
    $("triage-result").hidden = true;
    const airport = state.airports.find((item) => item.icao === icao);
    if (airport && state.projection === "globe") {
      state.lon = airport.longitude;
      state.lat = airport.latitude;
      viewChanged();
    }
    document.querySelectorAll("#strips .strip").forEach((strip) => {
      if (strip.dataset.icao === icao) strip.setAttribute("aria-current", "true");
      else strip.removeAttribute("aria-current");
    });
    invalidate();
    // Distance and the radius are measured from the selected airport.
    refreshSelection();
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
      fillList($("t-higher"), result.why_not_higher);
      fillList($("t-lower"), result.why_not_lower);
      fillFacts($("t-counts"), result.counts);
      fillFacts($("t-weather"), result.weather);
      fillFacts($("t-legs"), result.legs);
      $("d-arrivals").textContent = result.movements.arrivals;
      $("d-departures").textContent = result.movements.departures;
      refreshBoard();
    } catch {
      $("triage-result").hidden = false;
      $("t-severity").textContent = "Unknown";
      fillList($("t-reasons"), ["The board server did not answer."]);
      $("t-silent").textContent = "";
      ["t-higher", "t-lower"].forEach((id) => fillList($(id), []));
      ["t-counts", "t-weather", "t-legs"].forEach((id) => fillFacts($(id), []));
    } finally {
      button.disabled = false;
    }
  });

  // --- aircraft: the selection, the focus, the follower ------------------------
  function renderSelection(selection) {
    if (!selection) {
      $("a-selection").textContent = "No traffic layer is held yet.";
      $("a-dropped").textContent = "";
      return;
    }
    $("a-selection").textContent = selection.fallback
      ? `${selection.text}. ${selection.fallback}` : selection.text;
    $("a-dropped").textContent = selection.dropped || "";
    const airline = $("a-airline");
    const chosen = state.choice.airline;
    const options = [new Option("any", "")];
    let present = !chosen;
    for (const entry of selection.airlines) {
      const name = entry.name ? `${entry.name} (${entry.designator})` : entry.designator;
      options.push(new Option(`${name} · ${entry.count}`, entry.designator));
      if (entry.designator === chosen) present = true;
    }
    if (!present) options.push(new Option(`${chosen} · none in this snapshot`, chosen));
    airline.replaceChildren(...options);
    airline.value = chosen;
    const focus = $("a-focus");
    const picks = [new Option("none", "")];
    for (const aircraft of state.airspace) {
      const label = (aircraft.callsign || "").trim() || aircraft.icao24;
      picks.push(new Option(`${label} · ${aircraft.icao24}`, aircraft.icao24));
    }
    if (state.focus && !state.airspace.some((a) => a.icao24 === state.focus)) {
      picks.push(new Option(`${state.focus} · outside the selection`, state.focus));
    }
    focus.replaceChildren(...picks);
    focus.value = state.focus || "";
  }

  function collectTrails(list) {
    for (const aircraft of list) {
      if (!known(aircraft.last_contact) || !known(aircraft.latitude)) continue;
      const points = state.trails.get(aircraft.icao24) || [];
      const last = points[points.length - 1];
      if (last && last.time === aircraft.last_contact) continue;
      points.push({
        time: aircraft.last_contact, label: aircraft.heard_utc, lift: aircraft.lift,
        latitude: aircraft.latitude, longitude: aircraft.longitude,
      });
      if (points.length > TRAIL_POINTS) points.shift();
      // Re-inserted, so the oldest untouched aircraft is the first to be forgotten.
      state.trails.delete(aircraft.icao24);
      state.trails.set(aircraft.icao24, points);
      if (state.trails.size > TRAIL_AIRCRAFT) state.trails.delete(state.trails.keys().next().value);
    }
  }

  function applyFocus(answer) {
    // A poll sent before the selection changed answers for the old aircraft. Painting it
    // would bring back the brackets and the panel the reader just let go of.
    const focus = answer && answer.icao24 === state.focus ? answer : null;
    state.focusData = focus;
    if (focus && focus.aircraft) collectTrails([focus.aircraft]);
    if (state.follow && focus) {
      if (focus.state !== "reported") {
        stopFollow(focus.follow_stopped);
      } else if (focus.aircraft.last_contact !== state.followHeard) {
        // Moves only when a new observation lands. Nothing is animated in between.
        state.followHeard = focus.aircraft.last_contact;
        centreOn(focus.aircraft.longitude, focus.aircraft.latitude);
      }
    }
    renderAircraftPanel();
  }

  // Selecting an aircraft replaces the last one whole: track, follow, panel and brackets.
  function focusOn(icao24) {
    if (icao24 && state.focus && icao24 !== state.focus) say(`Selection moved to ${icao24}.`);
    else if (icao24) say("");
    state.focus = icao24 || null;
    state.focusData = null;
    state.track = null;
    state.follow = false;
    state.followNote = "";
    state.followHeard = null;
    renderAircraftPanel();
    refreshSelection();
    if (state.focus) fetchTrack(false);
    invalidate();
  }

  async function fetchTrack(estimate) {
    const icao24 = state.focus;
    const region = state.region;
    const button = $("a-estimate");
    button.disabled = true;
    try {
      const view = await api(
        `/api/traffic/${encodeURIComponent(region)}/aircraft/${encodeURIComponent(icao24)}/track${estimate ? "?estimate=true" : ""}`,
      );
      if (icao24 !== state.focus) return;
      state.track = view;
    } catch {
      if (icao24 !== state.focus) return;
      state.track = {
        waypoints: [], labels: [], unknowns: [], estimate_requested: false,
        lines: [{ label: "Transit", text: "unavailable: the board server did not answer" }],
      };
    } finally {
      button.disabled = false;
    }
    renderAircraftPanel();
    invalidate();
  }

  function stopFollow(note) {
    state.follow = false;
    state.followNote = note || "";
    renderAircraftPanel();
  }

  // The status line under the map: which of the exits just happened, in words.
  function say(text) {
    $("map-status").textContent = text;
  }

  const focusName = () => {
    const aircraft = state.focusData && state.focusData.aircraft;
    return (aircraft && (aircraft.callsign || "").trim()) || state.focus;
  };

  // Every way out of a selection ends here, so the panel, the brackets, the track and the
  // follow all clear together. `how` is the exit, for the status line.
  function clearSelection(how) {
    if (!state.focus) return;
    const name = focusName();
    focusOn(null);
    say(`Selection cleared (${how}): ${name}.`);
  }

  // Escape is one key with two steps: release the follow first, then let go of the aircraft.
  function escape() {
    if (state.follow) {
      const text = "Follow released. Press Escape again to clear the selection.";
      stopFollow(text);
      say(text);
      return;
    }
    clearSelection("Escape");
  }

  function renderAircraftPanel() {
    const panel = $("aircraft-panel");
    panel.hidden = !state.focus;
    if (!state.focus) return;
    const focus = state.focusData;
    const aircraft = focus && focus.aircraft;
    $("a-title").textContent = aircraft ? ((aircraft.callsign || "").trim() || aircraft.icao24) : state.focus;
    fillFacts($("a-facts"), focus ? focus.rows : []);
    const pending = [
      { label: "Transit", text: "asking OpenSky for the observed path (4 credits)" },
      { label: "Estimated arrival", text: "not requested" },
      { label: "Destination", text: "No source." },
    ];
    fillFacts($("a-claims"), state.track ? state.track.lines : pending);
    const follow = $("a-follow");
    follow.setAttribute("aria-pressed", String(state.follow));
    follow.textContent = state.follow ? "Following: release" : "Follow";
    follow.disabled = !state.follow && !(focus && focus.state === "reported");
    $("a-estimate").hidden = Boolean(state.track && state.track.estimate_requested);
    const reason = focus && focus.state !== "reported" && !state.followNote ? focus.reason : "";
    $("a-follow-note").textContent = state.followNote || reason || "";
    const gaps = state.track ? state.track.unknowns : [];
    $("a-gaps").hidden = gaps.length === 0;
    fillList($("a-unknowns"), gaps);
  }

  $("a-follow").addEventListener("click", () => {
    if (state.follow) { stopFollow("Follow released."); return; }
    state.follow = true;
    state.followNote = "";
    state.followHeard = null;
    applyFocus(state.focusData);
  });
  $("a-estimate").addEventListener("click", () => fetchTrack(true));
  $("a-clear").addEventListener("click", () => clearSelection("the Clear button"));
  $("a-focus").addEventListener("change", () => {
    const chosen = $("a-focus").value;
    if (chosen) focusOn(chosen);
    else clearSelection("the Focus list");
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !event.defaultPrevented) escape();
  });

  for (const [id, key] of [["a-sort", "sort"], ["a-cap", "cap"], ["a-radius", "radius_nm"],
    ["a-airline", "airline"], ["a-band", "band"], ["a-phase", "phase"]]) {
    $(id).value = state.choice[key];
    $(id).addEventListener("change", () => {
      state.choice[key] = $(id).value;
      remember(AIRCRAFT_KEY, JSON.stringify(state.choice));
      refreshSelection();
    });
  }
  let aircraftSearchTimer = null;
  $("a-q").addEventListener("input", () => {
    clearTimeout(aircraftSearchTimer);
    aircraftSearchTimer = setTimeout(() => {
      state.aircraftQuery = $("a-q").value.trim();
      refreshSelection();
    }, SEARCH_DELAY_MS);
  });

  // --- the map -----------------------------------------------------------------
  const invalidate = () => {
    if (state.dirty === "queued") return;
    state.dirty = "queued";
    requestAnimationFrame(draw);
  };

  const isCompact = () => state.width < COMPACT_WIDTH;

  function mapScale() {
    return Math.min(state.width / (2 * Math.PI), state.height / Math.PI) * state.zoom;
  }

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
    const scale = mapScale();
    const reach = Math.max(0, (scale * Math.PI - height) / 2);
    state.panY = Math.max(-reach, Math.min(reach, state.panY));
    return d3.geoEquirectangular()
      .scale(scale)
      .translate([width / 2, height / 2 + state.panY])
      .rotate([-state.lon, 0]);
  }

  // Put a place at the centre: rotate the globe to it, or pan the map to it.
  function centreOn(lon, lat) {
    state.lon = lon;
    if (state.projection === "globe") state.lat = Math.max(-89, Math.min(89, lat));
    else state.panY = mapScale() * lat * Math.PI / 180;
    invalidate();
    viewChanged();
  }

  // The far side of the globe is not drawn, and its markers cannot be picked. Airports and
  // aircraft both pass through here, so both are culled by the same test.
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

  function drawMarker(category, x, y, r) {
    const style = CATEGORY[category];
    if (!style) { drawUnknown(x, y, r); return; }
    shapePath(style.shape, x, y, r);
    context.fillStyle = style.colour;
    context.shadowColor = style.colour;
    context.shadowBlur = 2.5 * pixelRatio();
    context.fill();
    context.shadowBlur = 0;
    context.strokeStyle = INK.halo;
    context.lineWidth = 1;
    context.stroke();
  }

  function airportTip(airport) {
    const tip = airport.weather ? airport.weather.summary
      : (state.display ? state.display.trimmed_tip : "Unknown");
    return `${airport.icao}: ${airport.marker ? airport.marker.label : "Unknown"}. ${tip}`;
  }

  function drawAirport(airport) {
    const point = place(airport.longitude, airport.latitude);
    if (!point) return;
    const [x, y] = point;
    const scale = isCompact() ? 0.85 : 1;
    const category = airport.marker && airport.marker.kind === "weather" ? airport.marker.category : null;
    drawMarker(category, x, y, (CATEGORY[category] ? CATEGORY[category].radius : UNKNOWN_RADIUS) * scale);
    if (airport.icao !== state.selected && state.zoom >= 2.5) {
      state.labels.push({ text: airport.icao, x: x + 10, y: y - 8, order: drawOrder(airport) });
    }
    state.pickables.push({ x, y, kind: "airport", item: airport, tip: airportTip(airport) });
  }

  // One marker per cell: the most common category's shape, sized by count, the count beside
  // it, and a small badge in the worst category when that is worse (board.clusters).
  function drawClusters(cells) {
    const show = unknownShown();
    const scale = isCompact() ? 0.85 : 1;
    for (const cell of cells) {
      const count = cell.reported + (show ? cell.unknown : 0);
      if (!count) continue;
      const point = place(cell.longitude, cell.latitude);
      if (!point) continue;
      const [x, y] = point;
      const r = Math.min(8, 4.2 + Math.log2(count) * 0.7) * scale;
      drawMarker(cell.majority, x, y, r);
      if (cell.worst && cell.worst !== cell.majority) {
        drawMarker(cell.worst, x + r * 0.95, y - r * 0.95, Math.max(2.6, r * 0.5));
      }
      if (count > 1) drawText(String(count), x + r + 5, y + 4, `600 11px ${MONO}`, INK.label);
      const text = show ? cell.text_with_unknown : cell.text;
      state.pickables.push({
        x, y, r: Math.max(HIT_RADIUS_PX, r + 4), kind: "cluster", item: cell,
        tip: `${count} airports: ${text}. Select to zoom in.`,
      });
    }
  }

  const drawOrder = (airport) => {
    const style = airport.marker && airport.marker.kind === "weather" ? CATEGORY[airport.marker.category] : null;
    if (airport.icao === state.selected) return UNKNOWN_ORDER + 1;
    return style ? style.order : UNKNOWN_ORDER;
  };

  function clusterCell() {
    if (!state.display) return null;
    for (const [below, cell] of state.display.cluster_levels) if (state.zoom < below) return cell;
    return null;
  }

  // Clusters sit under the aircraft, so a large group marker never hides one; single
  // airports sit over them, as before.
  function clustered() {
    const cell = clusterCell();
    return cell !== null && state.clusters[String(cell)] ? state.clusters[String(cell)] : null;
  }

  function drawAirports() {
    if (clustered()) {
      const chosen = state.airports.find((airport) => airport.icao === state.selected);
      if (chosen) drawAirport(chosen);
      return;
    }
    const show = unknownShown();
    state.labels = [];
    state.airports
      .filter((airport) => airport.marker || show || airport.icao === state.selected)
      .sort((a, b) => drawOrder(a) - drawOrder(b))
      .forEach(drawAirport);
    placeLabels();
  }

  // Codes are placed worst category first, and a code that would overlap one already placed
  // is left out: the marker stays, and its tooltip still names it.
  function placeLabels() {
    context.font = `600 12px ${MONO}`;
    const placed = [];
    const ranked = state.labels.sort((a, b) => (b.order - a.order) || a.text.localeCompare(b.text));
    for (const label of ranked) {
      const width = context.measureText(label.text).width;
      const box = { left: label.x - 2, right: label.x + width + 2, top: label.y - 12, bottom: label.y + 3 };
      if (placed.some((other) => overlaps(box, other))) continue;
      placed.push(box);
      drawText(label.text, label.x, label.y, `600 12px ${MONO}`, INK.label);
    }
  }

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

  // The selection outlives a change of region: the detail carries its own position.
  const selectedAirport = () => state.airports.find((item) => item.icao === state.selected)
    || (state.detail && state.detail.icao === state.selected ? state.detail : undefined);

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

  function brackets(x, y, s) {
    const c = 5;
    context.strokeStyle = INK.focus;
    context.lineWidth = 1.75;
    context.beginPath();
    for (const [dx, dy] of [[-1, -1], [1, -1], [1, 1], [-1, 1]]) {
      context.moveTo(x + dx * s, y + dy * (s - c));
      context.lineTo(x + dx * s, y + dy * s);
      context.lineTo(x + dx * (s - c), y + dy * s);
    }
    context.stroke();
  }

  // Corner brackets and a label plate joined by a leader line.
  function drawSelection() {
    const airport = selectedAirport();
    const point = airport && place(airport.longitude, airport.latitude);
    if (!point) { placeBadge(null); return; }
    const [x, y] = point;
    const s = SELECTION_PX;
    brackets(x, y, s);

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

  // Where an aircraft is drawn. On the globe it is lifted radially by its altitude: its
  // ground point scaled outward from the centre by 1 + lift (board.globe_lift). Culled by
  // where it is over the ground, through `place`, like an airport.
  function aircraftPoint(aircraft) {
    const surface = place(aircraft.longitude, aircraft.latitude);
    if (!surface) return null;
    if (state.projection !== "globe" || !aircraft.lift) return { surface, at: surface };
    const [cx, cy] = projection.translate();
    const k = 1 + aircraft.lift;
    return { surface, at: [cx + (surface[0] - cx) * k, cy + (surface[1] - cy) * k] };
  }

  function aircraftTip(aircraft) {
    const name = (aircraft.callsign || "").trim() || aircraft.icao24;
    let height = "altitude not reported";
    if (aircraft.on_ground) height = "on the ground";
    else if (known(aircraft.altitude_m)) height = `${Math.round(aircraft.altitude_m).toLocaleString("en")} m`;
    return `${name}: ${height}. Select to focus.`;
  }

  // Drawn where it was last reported and faded by age. Never moved between polls.
  // White, not phosphor: on this board green means VFR.
  function drawAircraft(aircraft, dense, dim, pickable) {
    const point = aircraftPoint(aircraft);
    if (!point) return;
    const [x, y] = point.at;
    const alpha = aircraft.fade * (dim ? DIMMED : 1);
    context.globalAlpha = alpha;
    context.fillStyle = INK.aircraft;
    context.strokeStyle = INK.aircraft;
    if (point.at !== point.surface && !dense) {
      // The stem: from where it is over the ground up to where it is drawn.
      context.globalAlpha = alpha * 0.55;
      context.lineWidth = 0.75;
      context.beginPath();
      context.moveTo(point.surface[0], point.surface[1]);
      context.lineTo(x, y);
      context.stroke();
      context.globalAlpha = alpha;
    }
    if (dense) {
      context.fillRect(x - 1.25, y - 1.25, 2.5, 2.5);
    } else if (aircraft.on_ground) {
      context.fillRect(x - 1.5, y - 1.5, 3, 3);
    } else if (!known(aircraft.track)) {
      context.beginPath();
      context.arc(x, y, 2, 0, Math.PI * 2);
      context.fill();
    } else {
      // A little larger the higher it is, so the map and the globe agree on altitude.
      const maxLift = state.display ? state.display.globe_max_lift : 0.035;
      const size = 1 + ((aircraft.lift || 0) / maxLift) * 0.35;
      context.save();
      context.translate(x, y);
      context.rotate((aircraft.track * Math.PI) / 180);
      context.scale(size, size);
      context.beginPath();
      context.moveTo(0, -5); context.lineTo(3, 3.5); context.lineTo(-3, 3.5);
      context.closePath();
      if (known(aircraft.altitude_m)) {
        context.fill();
      } else {
        // No altitude reported: hollow, never drawn as if it were at 0.
        context.lineWidth = 1.1 / size;
        context.stroke();
      }
      context.restore();
    }
    context.globalAlpha = 1;
    if (pickable) state.pickables.push({ x, y, kind: "aircraft", item: aircraft, tip: aircraftTip(aircraft) });
  }

  // The positions heard while this page has been open, each with its own time. Dots, not a
  // line: nothing between two observations was observed.
  function drawTrail(icao24) {
    const points = state.trails.get(icao24) || [];
    context.fillStyle = INK.focus;
    points.forEach((trail, index) => {
      const point = aircraftPoint(trail);
      if (!point) return;
      context.globalAlpha = 0.85;
      context.beginPath();
      context.arc(point.at[0], point.at[1], 2.2, 0, Math.PI * 2);
      context.fill();
      context.globalAlpha = 1;
      const labelled = index === points.length - 1 || index % 4 === 0;
      if (!isCompact() && labelled && trail.label && index !== points.length - 1) {
        drawText(trail.label, point.at[0] + 6, point.at[1] + 12, `10px ${MONO}`, INK.note);
      }
    });
  }

  // The observed transit: OpenSky's waypoints joined in order along great circles, so on the
  // globe it follows the sphere. Dimmer than the live position, and never past the last point.
  function drawTransit(path) {
    const track = state.track;
    if (!track || !track.waypoints || track.waypoints.length === 0) return;
    const coordinates = track.waypoints.map((w) => [w.longitude, w.latitude]);
    context.save();
    context.globalAlpha = 0.6;
    context.strokeStyle = INK.focus;
    // Solid: observed. A dash on this board means projected, not observed.
    context.setLineDash([]);
    context.lineWidth = OBSERVED_WIDTH;
    context.beginPath();
    path({ type: "LineString", coordinates });
    context.stroke();
    context.restore();
    if (isCompact()) return;
    for (const index of track.labels) {
      const waypoint = track.waypoints[index];
      const point = place(waypoint.longitude, waypoint.latitude);
      if (!point) continue;
      context.fillStyle = INK.focus;
      context.beginPath();
      context.arc(point[0], point[1], 2.5, 0, Math.PI * 2);
      context.fill();
      drawText(waypoint.time_utc, point[0] + 6, point[1] - 6, `10.5px ${MONO}`, INK.note);
    }
  }

  function drawFocused() {
    const focus = state.focusData;
    const aircraft = focus && focus.aircraft;
    if (!aircraft) return;
    drawAircraft(aircraft, false, false, true);
    const point = aircraftPoint(aircraft);
    if (!point) return;
    const [x, y] = point.at;
    brackets(x, y, 10);
    const name = (aircraft.callsign || "").trim() || aircraft.icao24;
    drawText(name, x + 14, y - 10, `700 12.5px ${MONO}`, INK.focus);
  }

  // --- what approaches the selected airport ------------------------------------
  const inboundIds = () => new Set(state.inbound ? state.inbound.aircraft.map((a) => a.icao24) : []);

  // The one lit: the one under the pointer, else the selected one if it is approaching.
  function inboundLit() {
    if (state.inboundHover) return state.inboundHover;
    return state.focus && inboundIds().has(state.focus) ? state.focus : null;
  }

  function hoverInbound(icao24) {
    const next = icao24 && inboundIds().has(icao24) ? icao24 : null;
    if (next === state.inboundHover) return;
    state.inboundHover = next;
    invalidate();
  }

  // The legend's words for the lines. The projection is never called a track.
  function inboundLegend() {
    return "Approach lines: dashed is projected, not observed — a great circle from where an "
      + "aircraft was heard to the selected airport, nothing more. Solid is an observed track "
      + "from OpenSky, drawn only when asked for. A ring marks an approaching aircraft.";
  }

  // Lines first, then the aircraft over them, ringed and above every other aircraft.
  function drawInbound(path) {
    const airport = selectedAirport();
    if (!state.inbound || !airport) return;
    const lit = inboundLit();
    context.save();
    context.strokeStyle = INK.focus;
    for (const aircraft of state.inbound.aircraft) {
      const observed = state.inboundTracks.get(aircraft.icao24);
      const extra = lit === aircraft.icao24 ? LIT_EXTRA : 0;
      context.globalAlpha = lit && !extra ? FADED : 0.85;
      context.beginPath();
      if (observed && observed.length) {
        // Observed: solid. It replaces the projection for this aircraft only.
        context.setLineDash([]);
        context.lineWidth = OBSERVED_WIDTH + extra;
        path({ type: "LineString", coordinates: observed.map((w) => [w.longitude, w.latitude]) });
      } else {
        // Projected, not observed: dashed, and thinner than any observed track.
        context.setLineDash(PROJECTED_DASH);
        context.lineWidth = PROJECTED_WIDTH + extra;
        path({
          type: "LineString",
          coordinates: [[aircraft.longitude, aircraft.latitude], [airport.longitude, airport.latitude]],
        });
      }
      context.stroke();
    }
    context.restore();
    for (const aircraft of state.inbound.aircraft) {
      if (aircraft.icao24 === state.focus) continue;
      // Faded beside the lit one; dimmed like every other aircraft beside a selected one.
      const faded = lit ? lit !== aircraft.icao24 : Boolean(state.focus);
      drawAircraft(aircraft, false, faded, true);
      const point = aircraftPoint(aircraft);
      if (!point) continue;
      context.globalAlpha = faded ? FADED : 1;
      context.strokeStyle = INK.focus;
      context.lineWidth = 1.25;
      context.beginPath();
      context.arc(point.at[0], point.at[1], INBOUND_RING_PX, 0, Math.PI * 2);
      context.stroke();
      context.globalAlpha = 1;
    }
  }

  function inboundItem(aircraft) {
    const item = document.createElement("li");
    const button = document.createElement("button");
    button.type = "button";
    button.className = "quiet";
    button.dataset.icao24 = aircraft.icao24;
    button.dataset.evidence = aircraft.evidence;
    button.textContent = aircraft.text;
    button.addEventListener("click", () => focusOn(aircraft.icao24));
    for (const name of ["mouseenter", "focus"]) {
      button.addEventListener(name, () => hoverInbound(aircraft.icao24));
    }
    for (const name of ["mouseleave", "blur"]) button.addEventListener(name, () => hoverInbound(null));
    item.append(button);
    return item;
  }

  // Every word here was decided by the server (dashboard/inbound.py), except the legend's.
  function renderInbound(inbound) {
    state.inbound = inbound;
    if (state.inboundHover && !inboundIds().has(state.inboundHover)) state.inboundHover = null;
    const legend = $("inbound-legend");
    legend.hidden = !inbound;
    legend.textContent = inbound ? inboundLegend() : "";
    const tracks = $("i-tracks");
    if (!inbound) {
      $("i-summary").textContent = $("traffic-on").checked ? "No traffic layer is held yet." : INBOUND_OFF;
      $("i-dropped").textContent = "";
      $("i-rule").textContent = "";
      $("i-list").replaceChildren();
      tracks.hidden = true;
      invalidate();
      return;
    }
    $("i-summary").textContent = inbound.text;
    $("i-dropped").textContent = inbound.dropped || "";
    $("i-rule").textContent = inbound.rule;
    if (inbound.board_text) $("i-board-note").textContent = inbound.board_text;
    $("i-list").replaceChildren(...inbound.aircraft.map(inboundItem));
    tracks.textContent = inbound.price.text;
    tracks.hidden = inbound.price.count === 0;
    invalidate();
  }

  $("i-tracks").addEventListener("click", async () => {
    const { inbound } = state;
    const icao = state.selected;
    const region = state.region;
    if (!inbound || !icao) return;
    const button = $("i-tracks");
    const note = $("i-tracks-note");
    button.disabled = true;
    note.textContent = `Asking OpenSky for ${inbound.price.count} tracks (${inbound.price.total} credits).`;
    try {
      const answer = await api(
        `/api/traffic/${encodeURIComponent(region)}/inbound/${encodeURIComponent(icao)}/tracks?count=${inbound.price.count}`,
        { method: "POST" },
      );
      if (icao !== state.selected || region !== state.region) return;
      for (const track of answer.tracks) {
        if (track.state === "observed") state.inboundTracks.set(track.icao24, track.waypoints);
      }
      // A refusal is printed as it came, never replaced by the projection without a word.
      note.textContent = answer.text;
      refreshBoard();
    } catch {
      if (icao !== state.selected) return;
      note.textContent = "The board server did not answer. No track was drawn; the lines stay projected.";
    } finally {
      button.disabled = false;
      invalidate();
    }
  });

  $("i-board").addEventListener("click", async () => {
    const icao = state.selected;
    if (!icao) return;
    const button = $("i-board");
    const note = $("i-board-note");
    button.disabled = true;
    try {
      const answer = await api(`/api/airport/${encodeURIComponent(icao)}/arrivals`, { method: "POST" });
      if (icao !== state.selected) return;
      note.textContent = answer.text;
      refreshSelection();
      refreshBoard();
    } catch {
      if (icao !== state.selected) return;
      note.textContent = "The board server did not answer. The arrival board was not read.";
    } finally {
      button.disabled = false;
    }
  });

  function draw() {
    state.dirty = false;
    state.pickables = [];
    if (!state.width || !state.height) return;
    projection = buildProjection();
    // The first poll waits for the first frame, so it already asks about the view on screen.
    if (state.viewSent === null) refreshBoard();
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
    const layer = $("traffic-on").checked;
    const cells = clustered();
    if (cells) drawClusters(cells);
    if (state.focus) drawTransit(path);
    if (layer) {
      const dense = state.airspace.length > DENSE_AIRCRAFT && state.zoom < DETAIL_ZOOM;
      const dim = Boolean(state.focus);
      const approaching = inboundIds();
      state.airspace.forEach((aircraft) => {
        if (aircraft.icao24 !== state.focus && !approaching.has(aircraft.icao24)) {
          drawAircraft(aircraft, dense, dim, true);
        }
      });
      drawInbound(path);
    } else if (state.detail) {
      state.detail.traffic.aircraft.forEach((a) => drawAircraft(a, false, false, false));
    }
    drawAirports();
    if (state.focus) { drawTrail(state.focus); drawFocused(); }
    drawSelection();
    renderUnknownToggle();
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
    viewChanged();
  }

  // What is under the pointer, among what was drawn in the last frame.
  function nearest(x, y) {
    let best = null;
    let bestDistance = Infinity;
    for (const pick of state.pickables) {
      const distance = Math.hypot(pick.x - x, pick.y - y);
      if (distance <= (pick.r || HIT_RADIUS_PX) && distance < bestDistance) {
        best = pick;
        bestDistance = distance;
      }
    }
    return best;
  }

  function zoomInto(cell) {
    const levels = state.display ? state.display.cluster_levels : [];
    const level = levels.find(([below]) => state.zoom < below);
    state.zoom = Math.min(MAX_ZOOM, level ? level[0] : state.zoom * 2);
    centreOn(cell.longitude, cell.latitude);
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
      const pick = projection && nearest(event.offsetX, event.offsetY);
      hoverInbound(pick && pick.kind === "aircraft" ? pick.item.icao24 : null);
      const tip = $("tip");
      tip.hidden = !pick;
      if (pick) {
        tip.textContent = pick.tip;
        tip.style.left = `${Math.min(event.offsetX + 12, state.width - 200)}px`;
        tip.style.top = `${event.offsetY + 12}px`;
      }
      return;
    }
    const dx = event.offsetX - drag.x;
    const dy = event.offsetY - drag.y;
    if (Math.abs(dx) + Math.abs(dy) > 3) {
      drag.moved = true;
      // Moving the view by hand is the reader taking it back.
      if (state.follow) stopFollow("Follow released: the view was moved.");
    }
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
      const pick = nearest(event.offsetX, event.offsetY);
      if (pick && pick.kind === "airport") select(pick.item.icao);
      else if (pick && pick.kind === "cluster") zoomInto(pick.item);
      else if (pick && pick.kind === "aircraft" && pick.item.icao24 === state.focus) {
        // The selected aircraft again: a toggle, so the tap that chose it can also let it go.
        clearSelection("selected again");
      } else if (pick && pick.kind === "aircraft") focusOn(pick.item.icao24);
      // Empty sky: nothing under the pointer is a request to let go.
      else if (!pick) clearSelection("empty map");
    } else if (drag) {
      viewChanged();
    }
    drag = null;
    canvas.classList.remove("dragging");
  }
  canvas.addEventListener("pointerup", endDrag);
  canvas.addEventListener("pointercancel", () => { drag = null; canvas.classList.remove("dragging"); });
  canvas.addEventListener("pointerleave", () => { $("tip").hidden = true; hoverInbound(null); });
  canvas.addEventListener("wheel", (event) => {
    event.preventDefault();
    const factor = event.deltaY < 0 ? 1.2 : 1 / 1.2;
    state.zoom = Math.max(1, Math.min(MAX_ZOOM, state.zoom * factor));
    invalidate();
    viewChanged();
  }, { passive: false });

  document.querySelectorAll('input[name="projection"]').forEach((input) => {
    input.addEventListener("change", () => {
      state.projection = input.value;
      state.zoom = 1;
      state.panY = 0;
      invalidate();
      viewChanged();
    });
  });
  $("unknown-on").addEventListener("change", () => {
    // The reader's choice from now on, here and on the next visit. Nothing is asked of the server.
    state.showUnknown = $("unknown-on").checked;
    remember(UNKNOWN_KEY, state.showUnknown ? "1" : "0");
    invalidate();
  });
  $("traffic-on").addEventListener("change", () => {
    state.trafficChosen = true;
    state.trafficStopped = false;
    if ($("traffic-on").checked) {
      refreshTraffic();
      schedule("traffic", refreshTraffic, () => state.trafficIntervalMs);
      return;
    }
    state.airspace = [];
    state.trafficSnapshot = null;
    state.trafficUpstream = null;
    $("traffic-note").textContent = "";
    renderSelection(null);
    renderInbound(null);
    state.inboundTracks.clear();
    focusOn(null);
    refreshBoard();
    invalidate();
  });
  $("region").addEventListener("change", () => {
    state.region = $("region").value;
    remember(REGION_KEY, state.region);
    state.offset = 0;
    state.airports = [];
    state.clusters = {};
    state.airspace = [];
    state.trafficSnapshot = null;
    state.trafficStopped = false;
    // A region change is the one thing a selection does not survive.
    clearSelection("region changed");
    renderInbound(null);
    state.inboundTracks.clear();
    refreshBoard();
    refreshTraffic();
    invalidate();
  });

  let searchTimer = null;
  $("search").addEventListener("input", () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => {
      state.query = $("search").value;
      state.offset = 0;
      refreshList();
    }, SEARCH_DELAY_MS);
  });
  $("page-prev").addEventListener("click", () => {
    state.offset = Math.max(0, state.offset - LIST_PAGE);
    refreshList();
  });
  $("page-next").addEventListener("click", () => {
    state.offset += LIST_PAGE;
    refreshList();
  });

  // --- tabs --------------------------------------------------------------------
  const TABS = [["tab-board", "view-board"], ["tab-docs", "view-docs"]];
  function showTab(id, focus) {
    for (const [tab, panel] of TABS) {
      const chosen = tab === id;
      $(tab).setAttribute("aria-selected", String(chosen));
      $(tab).tabIndex = chosen ? 0 : -1;
      $(panel).hidden = !chosen;
    }
    if (focus) $(id).focus();
    if (id === "tab-board") resize();
  }
  TABS.forEach(([tab], index) => {
    $(tab).addEventListener("click", () => showTab(tab, false));
    $(tab).addEventListener("keydown", (event) => {
      if (event.key !== "ArrowRight" && event.key !== "ArrowLeft") return;
      const step = event.key === "ArrowRight" ? 1 : TABS.length - 1;
      showTab(TABS[(index + step) % TABS.length][0], true);
    });
  });
  $("to-glossary").addEventListener("click", (event) => {
    event.preventDefault();
    showTab("tab-docs", false);
    $("glossary").focus();
  });

  // --- start -------------------------------------------------------------------
  new ResizeObserver(resize).observe(canvas.parentElement);
  fetch("/static/vendor/land-110m.json")
    .then((response) => response.json())
    .then((world) => {
      state.land = topojson.feature(world, world.objects.land);
      invalidate();
    })
    .catch(() => { $("traffic-note").textContent = "The coastline file did not load."; });
  startPolling();
})();
