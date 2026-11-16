import {
  MONTHS, MONTHS_LONG, viridis, countT, fmt, fmtRatio, niceTicks,
  calendarSVG, cellLabel, seasonalitySVG, validationSVG, svgToPng, calValue, OVERLAP_START,
  RAW_NAMES, sourceNote, rawNote, snppEra,
} from "./charts.js";

// ?api= wins; otherwise the API is mounted same-origin under /api (`uvicorn index:app` serves both).
const API = (new URLSearchParams(location.search).get("api") || `${location.origin}/api`).replace(/\/$/, "");
const $ = (sel, root = document) => root.querySelector(sel);
const el = (tag, attrs = {}, ...kids) => {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v == null || v === false) continue;
    if (k === "class") n.className = v;
    else if (k === "text") n.textContent = v;
    else if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
    else n.setAttribute(k, v === true ? "" : v);
  }
  for (const k of kids.flat()) if (k != null) n.append(k.nodeType ? k : document.createTextNode(String(k)));
  return n;
};

const state = {
  health: null,
  coverage: null,
  bbox: null,           // [W,S,E,N] envelope of the selected area
  area: null,           // {kind: "bbox"|"poly"|"circle", bbox, poly?: [[lon,lat],…], circle?: [lon,lat,km]}
  from: null, to: null, // "YYYY-MM"
  calendar: null,
  current: null,
  events: null,         // events from /events for the current analysis (findings + map callouts)
  calMode: "counts",
  gen: 0,               // request generation, to drop stale responses
  regions: [],
};

// ───────────────────────── fetch helpers ─────────────────────────
function apiUrl(path, params = {}) {
  const u = new URL(API + path);
  for (const [k, v] of Object.entries(params)) if (v != null) u.searchParams.set(k, v);
  return u.toString();
}
async function getJSON(path, params, signal, retries = 1) {
  let r;
  try {
    r = await fetch(apiUrl(path, params), { signal });
  } catch (e) {
    if (e.name === "AbortError") throw e;
    // A network-level failure (or a 5xx without CORS headers) — retry once before giving up.
    if (retries > 0) { await new Promise((res) => setTimeout(res, 400)); return getJSON(path, params, signal, retries - 1); }
    throw new Error(state.health
      ? `Request to ${path} failed (server error or network). Try again.`
      : `Can't reach the API at ${API}. Is the backend running?`);
  }
  if (!r.ok) {
    if (r.status >= 500 && r.status !== 503 && retries > 0) { await new Promise((res) => setTimeout(res, 400)); return getJSON(path, params, signal, retries - 1); }
    let msg = `HTTP ${r.status}`;
    try { const j = await r.json(); if (j.detail) msg = typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail); } catch {}
    const err = new Error(msg); err.status = r.status; throw err;
  }
  return r.json();
}

// ───────────────────────── card states ─────────────────────────
const body = (id) => $(`#${id} .card-body`);
function setLoading(id, label = "Loading…") { body(id).replaceChildren(el("div", { class: "state loading", text: label })); }
function setError(id, err) {
  const msg = err?.status === 404 ? "Not available from this API yet (404)." : err?.message || String(err);
  body(id).replaceChildren(el("div", { class: "state err", role: "alert" }, msg, " ",
    state.bbox ? el("button", { type: "button", class: "btn btn-ghost btn-sm", onclick: () => analyze({ fit: false }), text: "Retry" }) : null));
}
function setEmpty(id, msg) { body(id).replaceChildren(el("div", { class: "state", text: msg })); }

// ───────────────────────── dates / bbox utils ─────────────────────────
const nowYM = () => { const d = new Date(); return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}`; };
const validYM = (s) => /^\d{4}-(0[1-9]|1[0-2])$/.test(s || "");
/** "2000-11" → "Nov 2000". */
const ymLabel = (ym) => `${MONTHS[Number(ym.slice(5, 7)) - 1]} ${ym.slice(0, 4)}`;
const monthStart = (ym) => `${ym}-01`;
function monthEnd(ym) {
  const [y, m] = ym.split("-").map(Number);
  const d = new Date(Date.UTC(y, m, 0)).getUTCDate();
  return `${ym}-${String(d).padStart(2, "0")}`;
}
const r3 = (v) => Math.round(v * 1000) / 1000;
const bboxStr = (b) => b.map(r3).join(",");
function parseBbox(s) {
  if (!s) return null;
  const p = s.split(",").map(Number);
  return p.length === 4 && p.every(Number.isFinite) ? p : null;
}
function bboxError(b) {
  if (!b) return "Enter four numbers: West, South, East, North.";
  const [w, s, e, n] = b;
  if (w < -180 || e > 180 || w > 180 || e < -180) return "Longitudes must be between −180 and 180.";
  if (s < -90 || n > 90 || s > 90 || n < -90) return "Latitudes must be between −90 and 90.";
  if (w >= e) return "West must be less than East.";
  if (s >= n) return "South must be less than North.";
  return null;
}
function areaKm2([w, s, e, n]) {
  const R = 6371.0088, rad = Math.PI / 180;
  return R * R * Math.abs((e - w) * rad) * Math.abs(Math.sin(n * rad) - Math.sin(s * rad));
}
function pointToBbox(lat, lon, km) {
  const dlat = km / 110.574;
  const dlon = km / (111.32 * Math.max(Math.cos(lat * Math.PI / 180), 0.01));
  return [Math.max(-180, lon - dlon), Math.max(-90, lat - dlat), Math.min(180, lon + dlon), Math.min(90, lat + dlat)];
}
const r4 = (v) => Math.round(v * 1e4) / 1e4;
function polyKm2(pts) {
  const R = 6371.0088, rad = Math.PI / 180;
  const lat0 = pts.reduce((a, p) => a + p[1], 0) / pts.length * rad;
  const xy = pts.map(([x, y]) => [x * rad * R * Math.cos(lat0), y * rad * R]);
  let a = 0;
  xy.forEach(([x1, y1], i) => { const [x2, y2] = xy[(i + 1) % xy.length]; a += x1 * y2 - x2 * y1; });
  return Math.abs(a) / 2;
}
function shapeKm2(a) {
  if (a.kind === "circle") return Math.PI * a.circle[2] ** 2;
  if (a.kind === "poly") return polyKm2(a.poly);
  return areaKm2(a.bbox);
}
/** API query params for the selected area (exactly one of bbox / poly / circle). */
function areaParams(a = state.area) {
  if (!a) return {};
  if (a.kind === "poly") return { poly: a.poly.map(([x, y]) => `${r4(x)},${r4(y)}`).join(";") };
  if (a.kind === "circle") return { circle: a.circle.map(r4).join(",") };
  return { bbox: bboxStr(a.bbox) };
}
function parseAreaQuery(q) {
  const poly = q.get("poly"), circle = q.get("circle"), bbox = parseBbox(q.get("bbox"));
  if (poly) {
    const pts = poly.split(";").map((p) => p.split(",").map(Number));
    if (pts.length >= 3 && pts.every((p) => p.length === 2 && p.every(Number.isFinite))) return { kind: "poly", poly: pts };
  }
  if (circle) {
    const c = circle.split(",").map(Number);
    if (c.length === 3 && c.every(Number.isFinite) && c[2] > 0 && c[2] <= 2000) return { kind: "circle", circle: c };
  }
  if (bbox && !bboxError(bbox)) return { kind: "bbox", bbox };
  return null;
}
/** Ratio with two decimals when close to 1 (fmtRatio would round 1.04× to 1.0×). */
const fmtX = (r) => (r < 2 ? `${r.toFixed(2)}×` : fmtRatio(r));
const AREA_NAMES = { bbox: "Rectangle", poly: "Polygon", circle: "Circle" };

// ───────────────────────── map ─────────────────────────
const NARROW = matchMedia("(max-width: 900px)");
const REDUCED = matchMedia("(prefers-reduced-motion: reduce)");
const map = L.map("map", { preferCanvas: true, worldCopyJump: true, zoomSnap: 0.25 }).setView([37, -119.5], 5);
// OpenTopoMap shaded relief (no API key needed).
L.tileLayer("https://{s}.tile.opentopomap.org/{z}/{x}/{y}.png", {
  attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors, <a href="https://www.earthdata.nasa.gov/data/instruments/srtm">SRTM</a> | map style &copy; <a href="https://opentopomap.org">OpenTopoMap</a> (<a href="https://creativecommons.org/licenses/by-sa/3.0/">CC-BY-SA</a>)',
  maxZoom: 17,
}).addTo(map);
new ResizeObserver(() => map.invalidateSize()).observe(document.getElementById("map"));

/** Padding that keeps a fitted shape clear of the floating cards, the findings rail and the drawer. */
function mapPad() {
  if (NARROW.matches) return { paddingTopLeft: [20, 20], paddingBottomRight: [20, 20] };
  const w = innerWidth, h = innerHeight;
  const left = $(".hud-left").getBoundingClientRect().right + 16;
  const rail = $("#findings");
  const right = (rail.hidden ? 0 : w - rail.getBoundingClientRect().left) + 16;
  const bottom = Math.min(h - $("#analysis").getBoundingClientRect().top, h * 0.5) + $("#legend").offsetHeight + 60;
  return { paddingTopLeft: [Math.min(left, w * 0.4), 84], paddingBottomRight: [Math.min(right, w * 0.35), bottom] };
}

const INK = "#0f172a";
const SHAPE = { color: INK, weight: 2.5, fill: false, dashArray: "7 5" };
const drawn = new L.FeatureGroup().addTo(map);
const cellsLayer = L.layerGroup();
const eventLayer = L.layerGroup().addTo(map);
const calloutLayer = L.layerGroup().addTo(map);
map.addControl(new L.Control.Draw({
  position: "topleft",
  draw: {
    rectangle: { shapeOptions: SHAPE, showArea: false },
    polygon: { shapeOptions: SHAPE, allowIntersection: false, showArea: false },
    circle: { shapeOptions: SHAPE, showRadius: true, metric: true },
    polyline: false, marker: false, circlemarker: false,
  },
  edit: false,
}));
map.on(L.Draw.Event.CREATED, (e) => {
  const lyr = e.layer;
  showFormError("");
  if (e.layerType === "circle") {
    const c = lyr.getLatLng(), km = lyr.getRadius() / 1000;
    if (km > 2000) return showFormError("Circles can be at most 2000 km in radius.");
    setArea({ kind: "circle", circle: [c.lng, c.lat, km] });
  } else if (e.layerType === "polygon") {
    setArea({ kind: "poly", poly: lyr.getLatLngs()[0].map((p) => [p.lng, p.lat]) });
  } else {
    const b = lyr.getBounds();
    setArea({ kind: "bbox", bbox: [b.getWest(), b.getSouth(), b.getEast(), b.getNorth()] });
  }
  setFormOpen(false);
  analyze();
});

// cell layer legend (the ramp block of the legend panel)
const legendDiv = $("#legend-ramp");
// Full viridis range: the dark end reads well on the light relief basemap.
const mapT = (v, max) => countT(v, max);
function renderMapLegend(max) {
  if (!max) { legendDiv.hidden = true; legendDiv.replaceChildren(); return; }
  const stops = Array.from({ length: 11 }, (_, i) => viridis(i / 10)).join(",");
  const ticks = niceTicks(max, 3).filter((v, i, a) => i === 0 || countT(v, max) - countT(a[i - 1], max) > 0.3);
  legendDiv.replaceChildren(
    el("div", { class: "legend-title", text: "Harmonized fire cell-days per 0.05° cell" }),
    el("div", { class: "bar", style: `background:linear-gradient(90deg,${stops})`, role: "img", "aria-label": `Color scale from 0 to ${fmt(max)} harmonized fire cell-days, square-root` }),
    el("div", { class: "ticks" },
      ticks.map((v) => el("span", { style: `left:${countT(v, max) * 100}%`, text: fmt(v) })),
    ),
  );
  legendDiv.hidden = !$("#cells-toggle").checked;
}
$("#cells-toggle").addEventListener("change", (e) => {
  if (e.target.checked) { cellsLayer.addTo(map); if (legendDiv.childElementCount) legendDiv.hidden = false; }
  else { map.removeLayer(cellsLayer); legendDiv.hidden = true; }
});
if ($("#cells-toggle").checked) cellsLayer.addTo(map);

function drawSelection(a, fit = true) {
  drawn.clearLayers();
  const b = a.bbox;
  const mk = (o) => (a.kind === "circle" ? L.circle([a.circle[1], a.circle[0]], { ...o, radius: a.circle[2] * 1000 })
    : a.kind === "poly" ? L.polygon(a.poly.map(([x, y]) => [y, x]), o)
    : L.rectangle([[b[1], b[0]], [b[3], b[2]]], o));
  // white casing under the dashed ink line keeps the outline readable on any terrain colour
  drawn.addLayer(mk({ color: "#ffffff", weight: 5, opacity: 0.85, fill: false, interactive: false }));
  drawn.addLayer(mk({ ...SHAPE, interactive: false }));
  map.invalidateSize();
  if (fit) map.fitBounds(L.latLngBounds([[b[1], b[0]], [b[3], b[2]]]), { ...mapPad(), maxZoom: 10, animate: !REDUCED.matches });
}

// ───────────────────────── form ─────────────────────────
const form = $("#area-form");
const fields = { w: $("#f-w"), s: $("#f-s"), e: $("#f-e"), n: $("#f-n"), lat: $("#f-lat"), lon: $("#f-lon"), km: $("#f-km"), from: $("#f-from"), to: $("#f-to") };
const mode = () => form.querySelector("input[name=mode]:checked").value;
form.querySelectorAll("input[name=mode]").forEach((r) => r.addEventListener("change", () => {
  form.querySelectorAll(".mode-pane").forEach((p) => (p.hidden = p.dataset.mode !== mode()));
}));

// The area controls live in a collapsible section of the area card.
const areaToggle = $("#area-toggle");
function setFormOpen(open) {
  form.hidden = !open;
  areaToggle.setAttribute("aria-expanded", String(open));
}
areaToggle.addEventListener("click", () => setFormOpen(form.hidden));

function showFormError(msg, field) {
  if (msg) setFormOpen(true);
  $("#form-error").textContent = msg || "";
  Object.values(fields).forEach((f) => f.removeAttribute("aria-invalid"));
  if (field) { field.setAttribute("aria-invalid", "true"); field.focus(); }
}

function setBbox(b) { setArea({ kind: "bbox", bbox: b }); }

function setArea(a) {
  let b;
  if (a.kind === "circle") b = pointToBbox(a.circle[1], a.circle[0], a.circle[2]);
  else if (a.kind === "poly") {
    const xs = a.poly.map((p) => p[0]), ys = a.poly.map((p) => p[1]);
    b = [Math.min(...xs), Math.min(...ys), Math.max(...xs), Math.max(...ys)];
  } else b = a.bbox;
  b = b.map(r3);
  state.area = { ...a, bbox: b };
  state.bbox = b;
  [fields.w.value, fields.s.value, fields.e.value, fields.n.value] = b;
  if (a.kind === "circle") { fields.lat.value = r3(a.circle[1]); fields.lon.value = r3(a.circle[0]); fields.km.value = r3(a.circle[2]); }
  else { fields.lat.value = r3((b[1] + b[3]) / 2); fields.lon.value = r3((b[0] + b[2]) / 2); }
  const desc = a.kind === "circle" ? `${r3(a.circle[2])} km around ${r3(a.circle[1])}, ${r3(a.circle[0])}`
    : a.kind === "poly" ? `${a.poly.length} vertices` : bboxStr(b);
  $("#area-readout").textContent = `${AREA_NAMES[a.kind]} ${desc} · ≈ ${fmt(shapeKm2(state.area), 0)} km²`;
  document.querySelectorAll(".chip").forEach((ch) => ch.setAttribute("aria-pressed", String(a.kind === "bbox" && ch.dataset.bbox === bboxStr(b))));
  renderAreaCard();
}

/** Area card title + stats line: region name (or "Custom area") · km² · date range · span. */
function renderAreaCard() {
  const a = state.area;
  if (!a) return;
  const region = a.kind === "bbox" && state.regions.find((r) => bboxStr(r.bbox) === bboxStr(a.bbox));
  $("#area-name").textContent = region ? region.name : "Custom area";
  $("#area-name").classList.toggle("long", !!region && region.name.length > 18);
  const parts = [`${fmt(shapeKm2(a), 0)} km²`];
  const f = fields.from.value, t = fields.to.value;
  if (validYM(f) && validYM(t) && f <= t) {
    const n = (Number(t.slice(0, 4)) - Number(f.slice(0, 4))) * 12 + Number(t.slice(5, 7)) - Number(f.slice(5, 7)) + 1;
    parts.push(`${ymLabel(f)} – ${ymLabel(t)}`, n >= 12 ? `${Math.round(n / 12)} yr${Math.round(n / 12) === 1 ? "" : "s"}` : `${n} mo`);
  }
  $("#area-stats").textContent = parts.join(" · ");
}

form.addEventListener("submit", (e) => {
  e.preventDefault();
  showFormError("");
  let area = state.area;
  const m = mode();
  if (m === "bounds") {
    let b;
    b = [fields.w, fields.s, fields.e, fields.n].map((f) => parseFloat(f.value));
    if (b.some((v) => !Number.isFinite(v))) return showFormError("Enter all four bounds: West, South, East, North.", [fields.w, fields.s, fields.e, fields.n].find((f) => !Number.isFinite(parseFloat(f.value))));
    const err = bboxError(b);
    if (err) return showFormError(err, fields.w);
    area = { kind: "bbox", bbox: b };
  } else if (m === "point") {
    const lat = parseFloat(fields.lat.value), lon = parseFloat(fields.lon.value), km = parseFloat(fields.km.value);
    if (!Number.isFinite(lat) || lat < -90 || lat > 90) return showFormError("Latitude must be a number between −90 and 90.", fields.lat);
    if (!Number.isFinite(lon) || lon < -180 || lon > 180) return showFormError("Longitude must be a number between −180 and 180.", fields.lon);
    if (!Number.isFinite(km) || km <= 0 || km > 2000) return showFormError("Radius must be between 1 and 2000 km.", fields.km);
    area = { kind: "circle", circle: [lon, lat, km] };
  } else if (!area) {
    return showFormError("Draw a rectangle, polygon or circle on the map first, or switch to Bounds / Point + radius to type an area.");
  }
  const dErr = dateError();
  if (dErr) return showFormError(dErr.msg, dErr.field);
  setArea(area);
  analyze();
});

function dateError() {
  const f = fields.from.value, t = fields.to.value;
  if (!validYM(f)) return { msg: "Pick a start month (YYYY-MM).", field: fields.from };
  if (!validYM(t)) return { msg: "Pick an end month (YYYY-MM).", field: fields.to };
  if (f > t) return { msg: "Start month must be before the end month.", field: fields.from };
  return null;
}

// ───────────────────────── analyze ─────────────────────────
async function analyze({ fit = true } = {}) {
  if (!state.bbox) return;
  if (!state.health?.data_ready) {
    showFormError("No data built yet. See the banner for setup steps.");
    return;
  }
  const dErr = dateError();
  if (dErr) return showFormError(dErr.msg, dErr.field);
  state.from = fields.from.value; state.to = fields.to.value;
  const gen = ++state.gen;
  const bbox = bboxStr(state.bbox);
  const ap = areaParams();
  const params = { ...ap, from: state.from, to: state.to };
  const apiQ = new URLSearchParams(location.search).get("api");
  const url = new URLSearchParams(apiQ ? { api: apiQ } : {});
  for (const [k, v] of Object.entries(params)) url.set(k, v);
  history.replaceState(null, "", "?" + url.toString().replace(/%2C/g, ",").replace(/%3B/g, ";"));
  document.body.classList.remove("no-area");
  $("#card-current").hidden = false;
  $("#findings").hidden = false;
  syncPanels();
  renderAreaCard();
  drawSelection(state.area, fit);
  eventLayer.clearLayers();
  calloutLayer.clearLayers();
  state.calendar = null; state.events = null;
  findingsUi.dismissed.clear(); findingsUi.expanded = false;
  renderFindings(true);

  updateDownloads(params);
  loadValidation(bbox);
  for (const id of ["card-current", "card-precedent", "card-summary", "card-events"]) setLoading(id);
  setLoading("card-calendar", "Loading calendar…");
  setLoading("card-season");
  $("#cal-table").replaceChildren();
  $("#current-source").textContent = "";
  $("#current-status").textContent = "";
  $("#card-current").dataset.level = "info";
  $("#events-count").textContent = "";
  body("card-brief").replaceChildren(el("div", { class: "state", text: "Generate a briefing for this area on demand." }));
  $("#brief-btn").disabled = false;

  const live = (fn) => (v) => { if (gen === state.gen) fn(v); };
  const fail = (id) => (e) => { if (gen === state.gen && e.name !== "AbortError") setError(id, e); };

  const calP = getJSON("/calendar", params).then(live((d) => { state.calendar = d; renderCalendar(); renderSummary(); renderFindings(); }))
    .catch((e) => { fail("card-calendar")(e); fail("card-summary")(e); fail("card-season")(e); });
  const curP = getJSON("/current", ap).then(live((d) => { state.current = d; renderCurrent(d); return d; }))
    .catch((e) => { fail("card-current")(e); return null; });

  Promise.all([calP, curP]).then(() => { if (gen === state.gen && state.calendar) renderSeason(); });
  curP.then((cur) => {
    if (gen !== state.gen) return;
    const month = cur?.month ?? Number(state.to.split("-")[1]);
    getJSON("/precedent", { ...ap, month }).then(live(renderPrecedent)).catch(fail("card-precedent"));
  });
  getJSON("/cells", { ...params, limit: 5000 }).then(live(renderCells)).catch((e) => { if (gen === state.gen) { cellsLayer.clearLayers(); renderMapLegend(0); console.warn("cells:", e.message); } });
  getJSON("/events", { ...ap, from: monthStart(state.from), to: monthEnd(state.to), limit: 50 }).then(live(renderEvents)).catch(fail("card-events"));
}

function updateDownloads(params) {
  $("#dl-calendar").href = apiUrl("/calendar.csv", params);
  $("#dl-hotspots").href = apiUrl("/csv", { ...areaParams(), from: monthStart(params.from), to: monthEnd(params.to) });
}

// ───────────────────────── renderers ─────────────────────────
function renderCurrent(d) {
  const card = $("#card-current"), status = $("#current-status"), foot = $("#current-source");
  if (!d || d.value == null) return setEmpty("card-current", "No current-month data for this area.");
  const mName = MONTHS_LONG[d.month - 1];
  const soFar = d.partial ? " so far" : "";
  const counts = `${fmt(d.value)} fire cell-days${soFar}`;
  const isRecord = d.highest_since == null && d.rank === 1;
  const hasRatio = d.median > 0 && d.ratio != null;
  // Status label: always text, the colour only repeats it (red ≥ 2×, amber 1.5–2×, blue otherwise).
  let level = "info", label, sentence;
  if (!hasRatio) {
    level = d.value > 0 ? "warning" : "info";
    label = d.value > 0 ? "Unusual" : "Quiet";
    sentence = d.value > 0
      ? `${mName} ${d.year} is unusual: ${counts} where a typical ${mName} has none${isRecord ? ", the highest on record" : ""}`
      : `${mName} ${d.year} is quiet, as usual: a typical ${mName} has no fire activity here`;
  } else {
    const shown = Math.round(d.ratio * 10) / 10; // classify on the ratio as displayed
    level = shown >= 2 ? "critical" : shown >= 1.5 ? "warning" : "info";
    label = `${fmtRatio(d.ratio)} normal`;
    const vs = `${counts} vs a median of ${fmt(d.median)}`;
    sentence = d.ratio > 1.15 ? `${mName} ${d.year} is running above normal: ${vs}`
      : d.ratio < 0.85 ? `${mName} ${d.year} is quieter than usual: ${vs}`
      : `${mName} ${d.year} is about typical: ${vs}`;
  }
  card.dataset.level = level;
  status.textContent = label;
  foot.textContent = `${d.live ? "Live · NOAA-20 NRT" : "Archive"} · ${MONTHS[d.month - 1]} ${d.year}`;
  foot.classList.toggle("live", !!d.live);

  const why = [];
  if (d.rank != null && d.n_years) why.push(el("span", {}, "Ranks ", el("strong", { text: `#${d.rank}` }), ` of ${d.n_years} ${mName}s on record`));
  if (isRecord) why.push(el("span", {}, "the ", el("strong", { text: `highest ${mName}` }), " on record"));
  else if (d.highest_since != null && d.ratio > 1.15) why.push(el("span", {}, "highest since ", el("strong", { text: String(d.highest_since) })));
  if (d.partial) why.push(el("span", {}, el("strong", { text: `${d.days_covered ?? "some"} days` }), ` of data so far, compared with the same days of ${mName} in past years (the total will rise as the month continues)`));
  else if (hasRatio) why.push(el("span", {}, "full month, compared with the median ", mName));
  const kids = [el("p", { class: "hero-action", text: sentence + "." })];
  const box = el("div", { class: "hero-why" }, why.flatMap((n, i) => (i ? [" · ", n] : [n])));
  if (hasRatio) {
    // log-scale ratio bar, ¼× … 4×, 1× in the middle
    const pos = Math.max(0, Math.min(1, (Math.log2(Math.max(d.ratio, 1e-3)) + 2) / 4));
    box.append(
      el("div", { class: "ratio-bar", role: "img", "aria-label": `${fmtRatio(d.ratio)} the historical median` },
        el("div", { class: "fill", style: `width:${pos * 100}%` }),
        el("div", { class: "mid", style: "left:calc(50% - 1px)" })),
      el("div", { class: "ratio-ticks", "aria-hidden": "true" }, ["¼×", "½×", "1× median", "2×", "4×+"].map((t) => el("span", { text: t }))));
  }
  kids.push(box);
  if (d.note) kids.push(el("p", { class: "note hero-note", text: d.note }));
  body("card-current").replaceChildren(...kids);
}

function renderPrecedent(d) {
  const mName = MONTHS_LONG[(d.month || 1) - 1];
  if (!d.n_events) return setEmpty("card-precedent", `No fire events starting in ${mName} on record in this area.`);
  const kids = [
    el("p", {}, `In this area, fire events starting in ${mName} were detected over a median of `,
      el("strong", { text: `${fmt(d.median_duration_days, 0)} day${d.median_duration_days === 1 ? "" : "s"}` }),
      d.p90_duration_days != null ? ` (1 in 10 over ${fmt(d.p90_duration_days, 0)}+ days)` : "", "."),
  ];
  const lg = d.largest;
  if (lg) {
    const yr = (lg.start || "").slice(0, 4);
    kids.push(el("p", {}, "The most persistent had a detection footprint of ", el("strong", { text: `${fmt(lg.area_km2, 0)} km²` }),
      lg.duration_days != null ? `, detected over ${fmt(lg.duration_days, 0)} days` : "", yr ? ` (${yr})` : "", "."));
  }
  const dl = el("dl", { class: "kv" },
    el("dt", { text: "Events on record" }), el("dd", { text: fmt(d.n_events, 0) }),
    el("dt", { text: "Median detection footprint" }), el("dd", { text: d.median_area_km2 != null ? `${fmt(d.median_area_km2)} km²` : "–" }),
    lg?.start ? [el("dt", { text: "Most persistent started" }), el("dd", { text: lg.start })] : null,
    lg?.peak_frp != null ? [el("dt", { text: "Its peak FRP" }), el("dd", { text: `${fmt(lg.peak_frp, 0)} MW` })] : null,
  );
  kids.push(dl);
  if (lg?.bbox) kids.push(el("button", { type: "button", class: "btn btn-ghost btn-sm", style: "margin-top:8px", onclick: () => selectEvent(lg), text: "Show it on the map" }));
  body("card-precedent").replaceChildren(el("div", { class: "prec" }, kids));
}

// calendar ------------------------------------------------------------
let calFocus = null; // "y-m" of roving tabindex cell
function renderCalendar() {
  const d = state.calendar;
  if (!d?.months?.length) {
    setEmpty("card-calendar", "No fire activity recorded in this area and date range.");
    body("card-calendar").append(el("div", { id: "cal-tip", class: "tip", hidden: true }));
    return;
  }
  const { svg, years, by } = calendarSVG(d, state.calMode, state.coverage, state.health);
  const host = el("div", { id: "calendar" });
  host.innerHTML = svg;
  const tip = el("div", { id: "cal-tip", class: "tip", role: "tooltip", hidden: true });
  body("card-calendar").replaceChildren(host, tip);
  renderHandoff(d.handoff);
  const small = $("#small-area-note");
  small.hidden = !(d.area_km2 < 100000 && d.months.some((m) => m.source === "modis_converted"));
  if (!small.hidden) small.textContent = `Small area (≈ ${fmt(d.area_km2, 0)} km²): areas under ~100,000 km² convert less reliably, so MODIS-converted months here carry the wider small-area 10–90% band (see the Method tab).`;
  wireCalendar(host, tip, by, years);
  renderCalTable(d, years, by);
}

function renderHandoff(h) {
  const box = $("#handoff");
  if (!h || h.viirs_per_modis == null) { box.hidden = true; return; }
  const span = (a) => `${a[0]}–${a[1]}`;
  const raw = state.calMode === "raw";
  box.hidden = false;
  box.classList.toggle("on", raw);
  const kids = [
    el("strong", { text: "Sensor handoff check. " }),
    `In the same months (${span(h.overlap_years)}) VIIRS logged `,
    el("strong", { class: "accent", text: fmtX(h.viirs_per_modis) }),
    " as many fire cell-days as MODIS over this area. Both were watching the same fires, so that ratio is the sensor difference.",
  ];
  if (h.raw_jump != null && h.harmonized_jump != null && h.before_years && h.after_years) {
    kids.push(` Stitched together raw, the record changes by `, el("strong", { text: fmtX(h.raw_jump) }),
      ` at ${h.after_years[0]} (${span(h.after_years)} vs. ${span(h.before_years)}): about ${fmtX(h.viirs_per_modis)} of that is what the sensor change alone would give, the rest is real change in fire between those years. Harmonized, the same comparison gives `,
      el("strong", { text: fmtX(h.harmonized_jump) }), ".");
  }
  if (!raw) kids.push(" ", el("button", { type: "button", class: "linkish", onclick: () => setCalMode("raw"), text: "Show raw sensors" }));
  box.replaceChildren(...kids);
}

function setCalMode(m) {
  state.calMode = m;
  const r = document.querySelector(`input[name=calmode][value=${m}]`);
  if (r) r.checked = true;
  if (state.calendar) renderCalendar();
}

function wireCalendar(host, tip, by, years) {
  const svg = host.querySelector("svg");
  const cells = [...svg.querySelectorAll("rect.cell")];
  if (!cells.length) return;
  const key = (r) => `${r.dataset.y}-${r.dataset.m}`;
  const start = cells.find((c) => key(c) === calFocus) ||
    cells.find((c) => { const p = state.calendar.summary?.peak_month; return p && key(c) === `${p.year}-${p.month}`; }) || cells[0];
  start.setAttribute("tabindex", "0");

  const show = (r) => {
    const y = +r.dataset.y, m = +r.dataset.m, rec = by.get(`${y}-${m}`);
    const med = state.calendar.median_by_month?.[m] ?? state.calendar.median_by_month?.[String(m)];
    if (state.calMode === "raw") {
      tip.replaceChildren(...[
        el("div", {}, el("b", { text: `${MONTHS_LONG[m - 1]} ${y}` })),
        rec && rec.raw != null
          ? [
            el("div", {}, el("b", { text: fmt(rec.raw) }), ` raw ${RAW_NAMES[rec.raw_source] || rec.raw_source} cell-days${rec.partial ? " so far" : ""}`),
            el("div", { class: "t-muted", text: `Harmonized: ${fmt(rec.value)}` }),
            el("div", { class: "t-muted", text: rawNote(rec, y, m, state.health) }),
          ]
          : el("div", { class: "t-muted", text: "No data" }),
      ].flat().filter(Boolean));
    } else tip.replaceChildren(...[
      el("div", {}, el("b", { text: `${MONTHS_LONG[m - 1]} ${y}` })),
      rec && rec.value != null
        ? [
          el("div", {}, el("b", { text: fmt(rec.value) }), rec.partial ? " fire cell-days so far" : " fire cell-days"),
          rec.partial ? el("div", { class: "t-warn", text: "Partial: month in progress, anomaly is provisional" }) : null,
          el("div", { class: "t-muted", text: rec.anomaly != null ? `${fmtRatio(rec.anomaly)} the ${MONTHS[m - 1]} median (${fmt(med)})${rec.partial ? ", provisional" : ""}` : `No comparison: ${MONTHS[m - 1]} median is ${fmt(med)}` }),
          el("div", { class: "t-muted", text: sourceNote(rec, y, m, state.health) }),
        ]
        : el("div", { class: "t-muted", text: "No data" }),
    ].flat().filter(Boolean));
    tip.hidden = false;
    const hb = host.parentElement.getBoundingClientRect(), rb = r.getBoundingClientRect();
    let left = rb.left - hb.left + host.parentElement.scrollLeft + rb.width + 8;
    if (left + 230 > hb.width) left = rb.left - hb.left - 240;
    tip.style.left = `${Math.max(0, left)}px`;
    tip.style.top = `${rb.top - hb.top - 4}px`;
  };
  const hide = () => (tip.hidden = true);
  svg.addEventListener("mouseover", (e) => { if (e.target.matches("rect.cell")) show(e.target); });
  svg.addEventListener("mouseout", (e) => { if (e.target.matches("rect.cell")) hide(); });
  svg.addEventListener("focusin", (e) => {
    if (!e.target.matches("rect.cell")) return;
    svg.querySelectorAll('rect.cell[tabindex="0"]').forEach((c) => c !== e.target && c.setAttribute("tabindex", "-1"));
    e.target.setAttribute("tabindex", "0");
    calFocus = key(e.target);
    show(e.target);
  });
  svg.addEventListener("focusout", hide);
  svg.addEventListener("click", (e) => {
    if (!e.target.matches("rect.cell")) return;
    svg.querySelectorAll('rect.cell[tabindex="0"]').forEach((c) => c.setAttribute("tabindex", "-1"));
    e.target.setAttribute("tabindex", "0");
    e.target.focus();
  });
  svg.addEventListener("keydown", (e) => {
    const r = e.target;
    if (!r.matches?.("rect.cell")) return;
    if (e.key === "Escape") return hide();
    let y = +r.dataset.y, m = +r.dataset.m;
    const moves = { ArrowLeft: [0, -1], ArrowRight: [0, 1], ArrowUp: [-1, 0], ArrowDown: [1, 0], Home: [0, -12], End: [0, 12] };
    if (!moves[e.key]) return;
    e.preventDefault();
    const [dy, dm] = moves[e.key];
    y = years[Math.max(0, Math.min(years.length - 1, years.indexOf(y) + dy))];
    m = Math.max(1, Math.min(12, m + dm));
    let next = svg.querySelector(`rect.cell[data-y="${y}"][data-m="${m}"]`);
    // skip over future months at the end of the record
    while (!next && m > 1) next = svg.querySelector(`rect.cell[data-y="${y}"][data-m="${--m}"]`);
    if (!next) return;
    r.setAttribute("tabindex", "-1");
    next.setAttribute("tabindex", "0");
    next.focus();
  });
}

function renderCalTable(d, years, by) {
  const raw = state.calMode === "raw";
  const valueOf = (y, m) => {
    const r = by.get(`${y}-${m}`);
    if (!r || calValue(r, state.calMode) == null) return "–";
    const v = state.calMode === "anomaly" ? (r.anomaly != null ? fmtRatio(r.anomaly) : "n/a") : fmt(calValue(r, state.calMode));
    return r.partial ? `${v}†` : v;
  };
  const n20 = d.months.filter((r) => r.source === "noaa20_converted");
  const n20Late = n20.some((r) => !snppEra(r.year, r.month, state.health));
  const n20Note = !n20.length ? "" : ` ${n20Late ? `Months after ${state.health?.snpp_end || "the end of Suomi NPP"}` : "Months with a Suomi NPP outage"}: NOAA-20 ${raw ? "VIIRS" : "converted to Suomi NPP units"}.`;
  const caption = (raw ? `Raw fire cell-days, not harmonized: MODIS before ${OVERLAP_START} (* rows), VIIRS after. † = month in progress (so far).`
    : state.calMode === "anomaly" ? "Value ÷ monthly median. * = MODIS converted to VIIRS-equivalent. † = month in progress (provisional)."
    : "Fire cell-days per month. * = MODIS converted to VIIRS-equivalent. † = month in progress (so far).") + n20Note;
  const t = el("table", { class: "data" },
    el("caption", { text: caption }),
    el("thead", {}, el("tr", {}, el("th", { scope: "col", text: "Year" }), MONTHS.map((m) => el("th", { scope: "col", text: m })))),
    el("tbody", {}, years.map((y) => {
      const conv = d.months.some((r) => r.year === y && r.source === "modis_converted");
      return el("tr", {}, el("th", { scope: "row", text: `${y}${conv ? "*" : ""}` }),
        Array.from({ length: 12 }, (_, i) => el("td", { title: cellLabel(y, i + 1, by.get(`${y}-${i + 1}`), d, state.calMode, state.health), text: valueOf(y, i + 1) })));
    })),
  );
  $("#cal-table").replaceChildren(t);
}

document.querySelectorAll("input[name=calmode]").forEach((r) => r.addEventListener("change", (e) => setCalMode(e.target.value)));

// seasonality -------------------------------------------------------------
function renderSeason() {
  const d = state.calendar;
  if (!d?.seasonality?.length) return setEmpty("card-season", "Not enough data for a seasonal profile.");
  const yr = state.current?.year ?? Math.max(...d.months.map((m) => m.year));
  const pts = d.months.filter((m) => m.year === yr).map((m) => ({ month: m.month, value: m.value }));
  const host = el("div", { id: "season" });
  host.innerHTML = seasonalitySVG(d.seasonality, yr, pts);
  body("card-season").replaceChildren(host);
}

// summary -------------------------------------------------------------
function renderSummary() {
  const d = state.calendar;
  const s = d?.summary;
  if (!d?.months?.length) return setEmpty("card-summary", "No fire activity recorded in this area and date range.");
  if (!s) return setEmpty("card-summary", "Summary not provided by the API.");
  const stat = (label, value, sub) => el("div", { class: "stat" },
    el("div", { class: "stat-label", text: label }), el("div", { class: "stat-value", text: value }), sub ? el("div", { class: "stat-sub", text: sub }) : null);
  const trend = s.trend_per_decade_pct;
  // Only a significant slope is shown as a trend; otherwise the number is muted and labelled as noise-compatible.
  const weak = trend != null && s.trend_significant === false;
  const trendStat = stat("Trend", trend == null ? "–" : `${trend > 0 ? "+" : ""}${fmt(trend, 1)}%`,
    trend == null ? "too few years" : weak ? `per decade · not statistically significant${s.trend_t != null ? ` (t = ${s.trend_t.toFixed(1)})` : ""}` : "per decade");
  if (weak) trendStat.classList.add("weak");
  const stats = el("div", { class: "stats" },
    s.peak_year ? stat("Peak year", String(s.peak_year.year), `${fmt(s.peak_year.value)} cell-days`) : null,
    s.peak_month ? stat("Peak month", `${MONTHS[s.peak_month.month - 1]} ${s.peak_month.year}`, `${fmt(s.peak_month.value)} cell-days`) : null,
    s.typical_peak_month ? stat("Typical peak", MONTHS_LONG[s.typical_peak_month - 1], "highest median month") : null,
    trendStat,
    s.total != null ? stat("Total", fmt(s.total, 0), `cell-days, ${state.from}…${state.to}`) : null,
  );
  const kids = [stats];
  const an = (s.top_anomalies || []).slice(0, 8);
  if (an.length) {
    const maxA = Math.max(...an.map((a) => a.anomaly || 0));
    kids.push(el("h3", { class: "sub-h", text: "Biggest anomalies (vs. median for that month)" }));
    kids.push(el("ul", { class: "anoms" }, an.map((a) => el("li", {},
      el("span", { text: `${MONTHS[a.month - 1]} ${a.year}` }),
      el("span", { class: "bar", "aria-hidden": "true" }, el("i", { style: `width:${maxA ? (a.anomaly / maxA) * 100 : 0}%` })),
      el("span", { text: `${fmtRatio(a.anomaly)} · ${fmt(a.value)}` }),
    ))));
  }
  body("card-summary").replaceChildren(...kids);
}

// cells -------------------------------------------------------------
// Cells are drawn as circle markers sized to the 0.05° cell at the current zoom, with a minimum
// pixel radius so they stay visible when zoomed out to a whole region.
let cellDeg = 0.05;
const cellRadius = () => Math.max(2, ((256 * 2 ** map.getZoom() * cellDeg) / 360) * 0.5);
function renderCells(d) {
  cellsLayer.clearLayers();
  const cells = [...(d.cells || [])].sort((a, b) => a.cell_days - b.cell_days); // biggest on top
  cellDeg = d.deg || 0.05;
  const max = Math.max(0, ...cells.map((c) => c.cell_days));
  const r = cellRadius();
  for (const c of cells) {
    L.circleMarker([c.lat, c.lon], {
      radius: r, color: INK, weight: 0.4, opacity: 0.55, fillColor: viridis(mapT(c.cell_days, max)), fillOpacity: 0.92,
    }).bindTooltip(`${fmt(c.cell_days)} harmonized fire cell-days<br><span style="color:#94a3b8">${c.lat.toFixed(3)}, ${c.lon.toFixed(3)}</span>`, { direction: "top" })
      .addTo(cellsLayer);
  }
  renderMapLegend(max);
}
map.on("zoomend", () => { const r = cellRadius(); cellsLayer.eachLayer((l) => l.setRadius(r)); });

// events -------------------------------------------------------------
function renderEvents(d) {
  const evs = d.events || [];
  state.events = evs;
  renderCallouts(evs);
  renderFindings();
  $("#events-count").textContent = evs.length ? `${evs.length} shown` : "";
  if (!evs.length) return setEmpty("card-events", "No fire events in this area and date range.");
  const list = el("div", { class: "events", role: "list" },
    el("div", { class: "events-head", "aria-hidden": "true" }, ["Start", "Days", "Footprint km²", "Peak FRP", "Cell-days"].map((t) => el("span", { text: t }))),
    evs.slice(0, 15).map((ev) => el("div", { role: "listitem" }, el("button", {
      type: "button", class: "event", "aria-pressed": "false", "data-id": ev.id,
      "aria-label": `Fire event first detected ${ev.start}, detected over ${ev.duration_days} days, ${fmt(ev.cell_days, 0)} fire cell-days, detection footprint ${fmt(ev.area_km2)} square kilometres, peak FRP ${fmt(ev.peak_frp, 0)} megawatts. Show on map.`,
      onclick: () => selectEvent(ev),
    }, el("span", { text: ev.start }), el("span", { text: fmt(ev.duration_days, 0) }), el("span", { text: fmt(ev.area_km2, 0) }),
    el("span", { text: ev.peak_frp != null ? `${fmt(ev.peak_frp, 0)} MW` : "–" }), el("span", { text: fmt(ev.cell_days, 0) })))),
  );
  body("card-events").replaceChildren(list);
}

function showEvent(ev) {
  eventLayer.clearLayers();
  const b = ev.bbox;
  if (!b) return;
  const rect = L.rectangle([[b[1], b[0]], [b[3], b[2]]], { color: "#1d4ed8", weight: 2.5, fillColor: "#3b82f6", fillOpacity: 0.14 })
    .bindTooltip(`Detections from ${ev.start} to ${ev.end || "?"} · over ${fmt(ev.duration_days, 0)} days · footprint ${fmt(ev.area_km2, 0)} km²`, { permanent: false })
    .addTo(eventLayer);
  map.flyToBounds(rect.getBounds(), { ...mapPad(), maxZoom: 11, duration: REDUCED.matches ? 0 : 0.8 });
}
/** Select an event everywhere: footprint on the map, pressed row in the events table. */
function selectEvent(ev) {
  document.querySelectorAll(".event").forEach((b) => b.setAttribute("aria-pressed", String(ev.id != null && b.dataset.id === String(ev.id))));
  showEvent(ev);
  if (NARROW.matches) document.getElementById("map").scrollIntoView({ behavior: REDUCED.matches ? "auto" : "smooth", block: "start" });
}

// on-map callouts: solid badges at the centroids of the three most persistent events
const evMonth = (ev) => `${MONTHS[Number(ev.start.slice(5, 7)) - 1]} ${ev.start.slice(0, 4)}`;
function renderCallouts(evs) {
  calloutLayer.clearLayers();
  evs.filter((ev) => ev.lat != null && ev.lon != null && ev.start).slice(0, 3).forEach((ev, i) => {
    const text = `${evMonth(ev)} · ${fmt(ev.duration_days, 0)} days · ${fmt(ev.area_km2, 0)} km²`;
    const badge = el("span", { class: `callout callout-${i ? "warning" : "critical"}`, text });
    const m = L.marker([ev.lat, ev.lon], {
      icon: L.divIcon({ className: "callout-icon", html: badge.outerHTML, iconSize: [0, 0] }),
      keyboard: true, zIndexOffset: 1000 - i * 10,
    }).on("click", () => selectEvent(ev)).addTo(calloutLayer);
    m.getElement()?.setAttribute("aria-label",
      `${i ? "Top fire event" : "Most persistent fire event"}, first detected ${ev.start}, detected over ${fmt(ev.duration_days, 0)} days, detection footprint ${fmt(ev.area_km2, 0)} square kilometres. Show on map.`);
  });
  staggerCallouts();
}

/** Lift a callout that would sit on top of an earlier one, so the three labels stay readable at any zoom. */
function staggerCallouts() {
  const placed = [];
  calloutLayer.eachLayer((m) => {
    const badge = m.getElement()?.querySelector(".callout");
    if (!badge) return;
    const p = map.latLngToLayerPoint(m.getLatLng());
    let lift = 0;
    while (placed.some((q) => Math.abs(q.x - p.x) < 230 && Math.abs(q.y - (p.y - lift)) < 28)) lift += 30;
    placed.push({ x: p.x, y: p.y - lift });
    badge.style.bottom = `${8 + lift}px`;
    badge.style.setProperty("--lift", `${lift}px`);
    // on a narrow map, slide the label sideways so it is not cut off by the map edge
    const cx = map.latLngToContainerPoint(m.getLatLng()).x, half = badge.offsetWidth / 2, w = map.getSize().x;
    const shift = NARROW.matches && cx > 0 && cx < w ? Math.max(54 + half - cx, Math.min(0, w - 6 - half - cx)) : 0;
    badge.style.transform = `translateX(calc(-50% + ${Math.round(shift)}px))`;
    badge.classList.toggle("shifted", shift !== 0);
    badge.classList.toggle("lifted", lift > 0);
  });
}
map.on("zoomend moveend", staggerCallouts);

// findings rail -------------------------------------------------------------
// Every card is built from numbers the API returned for this area: nothing is invented here.
const findingsUi = { dismissed: new Set(), expanded: false };
const FINDINGS_SHOWN = 3;
function buildFindings() {
  const out = [];
  const d = state.calendar, s = d?.summary, evs = state.events || [];
  const anoms = (s?.top_anomalies || []).filter((a) => a.anomaly != null).slice(0, 2).map((a) => ({
    id: `anom-${a.year}-${a.month}`,
    level: a.anomaly >= 2 ? "critical" : a.anomaly >= 1.5 ? "warning" : "info",
    text: `${MONTHS_LONG[a.month - 1]} ${a.year} reached ${fmtRatio(a.anomaly)} the ${MONTHS[a.month - 1]} median (${fmt(a.value)} fire cell-days)`,
    meta: `${a.anomaly >= 2 ? "Major anomaly" : a.anomaly >= 1.5 ? "Anomaly" : "Mild anomaly"} · ${MONTHS[a.month - 1]} ${a.year}`,
  }));
  const events = evs.filter((ev) => ev.start).slice(0, 3).map((ev, i) => ({
    id: `ev-${ev.id ?? ev.start}`,
    level: i ? "warning" : "critical",
    text: `${i ? "Top fire event" : "Most persistent fire event"}: detected over ${fmt(ev.duration_days, 0)} days from ${ev.start}, ${fmt(ev.area_km2, 0)} km² detection footprint`,
    meta: `Fire event · ${evMonth(ev)}${ev.bbox ? " · show on map" : ""}`,
    action: ev.bbox ? () => selectEvent(ev) : null,
  }));
  const info = [];
  const trend = s?.trend_per_decade_pct;
  if (trend != null) {
    const pctTxt = `${trend > 0 ? "+" : ""}${fmt(trend, 1)}% per decade`;
    info.push({
      id: "trend", level: "info",
      text: s.trend_significant === false
        ? `Trend of ${pctTxt} is not statistically significant${s.trend_t != null ? ` (t = ${s.trend_t.toFixed(1)})` : ""}`
        : `Fire activity is ${trend >= 0 ? "rising" : "falling"}: ${pctTxt}`,
      meta: `Trend · ${state.from.slice(0, 4)}–${state.to.slice(0, 4)}`,
    });
  }
  const rest = [];
  if (s?.peak_year) rest.push({ id: "peak-year", level: "info", text: `Peak year was ${s.peak_year.year}, with ${fmt(s.peak_year.value)} fire cell-days`, meta: "Record · peak year" });
  if (s?.peak_month) rest.push({ id: "peak-month", level: "info", text: `Peak month was ${MONTHS_LONG[s.peak_month.month - 1]} ${s.peak_month.year}, with ${fmt(s.peak_month.value)} fire cell-days`, meta: "Record · peak month" });
  if (s?.typical_peak_month) rest.push({ id: "typical", level: "info", text: `${MONTHS_LONG[s.typical_peak_month - 1]} is the typical peak month (highest median)`, meta: "Season" });
  const h = d?.handoff;
  if (h?.viirs_per_modis != null) rest.push({
    id: "handoff", level: "info",
    text: `VIIRS logged ${fmtX(h.viirs_per_modis)} as many fire cell-days as MODIS here${h.overlap_years ? ` in ${h.overlap_years[0]}–${h.overlap_years[1]}` : ""}; the record is harmonized for that gap`,
    meta: "Sensor handoff · open calendar",
    action: () => selectTab("calendar", true),
  });
  const outs = Object.entries(state.health?.outages || {}).filter(([, r]) => r?.length);
  if (d && outs.length) rest.push({
    id: "outages", level: "info",
    text: `Satellite outages detected in the archive: ${outs.map(([k, r]) => `${OUTAGE_NAMES[k] || k} ${r.length}`).join(", ")}. Missing days are filled from observed days`,
    meta: "Data quality · open method",
    action: () => selectTab("method", true),
  });
  // lead with one of each kind, then the remainder
  out.push(anoms[0], events[0], info[0], anoms[1], ...rest.slice(0, 2), ...events.slice(1), ...rest.slice(2));
  return out.filter(Boolean);
}
function renderFindings(loading = false) {
  const list = $("#findings-list"), count = $("#findings-count");
  if (loading) {
    count.textContent = "…";
    list.replaceChildren(el("div", { class: "finding finding-info" }, el("span", { class: "finding-ind", "aria-hidden": "true" }),
      el("div", { class: "finding-main" }, el("div", { class: "state loading", text: "Reading the record…" }))));
    return;
  }
  const all = buildFindings().filter((f) => !findingsUi.dismissed.has(f.id));
  count.textContent = String(all.length);
  $("#findings-live").textContent = `${all.length} finding${all.length === 1 ? "" : "s"}`;
  if (!all.length) {
    list.replaceChildren(el("div", { class: "rail-empty", text: state.calendar || state.events ? "No findings for this area and date range." : "Findings unavailable." }));
    return;
  }
  const shown = findingsUi.expanded ? all : all.slice(0, FINDINGS_SHOWN);
  const kids = shown.map((f) => {
    const inner = [el("span", { class: "finding-text", text: f.text + "." }), el("span", { class: "finding-meta", text: f.meta })];
    return el("div", { class: `finding finding-${f.level}` },
      el("span", { class: "finding-ind", "aria-hidden": "true" }),
      f.action ? el("button", { type: "button", class: "finding-main", onclick: f.action }, inner) : el("div", { class: "finding-main" }, inner),
      el("button", { type: "button", class: "finding-ack", "aria-label": `Dismiss finding: ${f.text}`, title: "Dismiss", text: "✓",
        onclick: () => { findingsUi.dismissed.add(f.id); renderFindings(); } }));
  });
  const more = all.length - FINDINGS_SHOWN;
  if (more > 0) kids.push(el("button", { type: "button", class: "show-more", "aria-expanded": String(findingsUi.expanded),
    text: findingsUi.expanded ? "▲ Show fewer" : `▼ Show ${more} more`,
    onclick: () => { findingsUi.expanded = !findingsUi.expanded; renderFindings(); } }));
  list.replaceChildren(...kids);
}

// drawer + tabs -------------------------------------------------------------
const drawer = $("#analysis");
const tabs = [...drawer.querySelectorAll('[role="tab"]')];
let activeTab = "calendar";
function syncPanels() {
  const hasArea = !document.body.classList.contains("no-area");
  tabs.forEach((t) => {
    const on = t.id === `tab-${activeTab}`;
    t.setAttribute("aria-selected", String(on));
    t.tabIndex = on ? 0 : -1;
    document.getElementById(t.getAttribute("aria-controls")).hidden = !on;
  });
  $("#cards").hidden = !hasArea;
  $("#empty").hidden = hasArea || activeTab === "method";
}
function setDrawer(size) {
  drawer.dataset.size = size;
  document.body.dataset.drawer = size;
  const closed = size === "closed", full = size === "full";
  const c = $("#drawer-collapse"), x = $("#drawer-expand");
  c.setAttribute("aria-expanded", String(!closed));
  c.setAttribute("aria-label", closed ? "Open analysis panel" : "Collapse analysis panel");
  c.title = closed ? "Open" : "Collapse";
  c.textContent = closed ? "▴" : "▾";
  x.setAttribute("aria-pressed", String(full));
  x.setAttribute("aria-label", full ? "Restore analysis panel size" : "Expand analysis panel");
  x.title = full ? "Restore" : "Expand";
  $("#drawer-body").hidden = closed;
}
function selectTab(name, focus = false) {
  activeTab = name;
  if (drawer.dataset.size === "closed") setDrawer("half");
  syncPanels();
  $("#drawer-body").scrollTop = 0;
  if (focus) $(`#tab-${name}`).focus();
}
tabs.forEach((t, i) => {
  t.addEventListener("click", () => selectTab(t.id.slice(4)));
  t.addEventListener("keydown", (e) => {
    const to = { ArrowRight: (i + 1) % tabs.length, ArrowLeft: (i - 1 + tabs.length) % tabs.length, Home: 0, End: tabs.length - 1 }[e.key];
    if (to == null) return;
    e.preventDefault();
    selectTab(tabs[to].id.slice(4), true);
  });
});
$("#drawer-collapse").addEventListener("click", () => setDrawer(drawer.dataset.size === "closed" ? "half" : "closed"));
$("#drawer-expand").addEventListener("click", () => setDrawer(drawer.dataset.size === "full" ? "half" : "full"));
setDrawer("half");
syncPanels();

// brief -------------------------------------------------------------
$("#brief-btn").addEventListener("click", async () => {
  if (!state.bbox) return;
  const btn = $("#brief-btn"), gen = state.gen;
  btn.disabled = true;
  setLoading("card-brief", "Writing briefing…");
  try {
    const d = await getJSON("/brief", areaParams());
    if (gen !== state.gen) return;
    body("card-brief").replaceChildren(el("p", { class: "brief-text", text: d.text || "(empty briefing)" }));
  } catch (e) {
    if (gen === state.gen) setError("card-brief", e);
  } finally {
    btn.disabled = false;
  }
});

// downloads -------------------------------------------------------------
const pngCal = () => svgToPng($("#calendar svg"), `plume_calendar_${bboxStr(state.bbox || [])}.png`,
  `Plume burning calendar · ${bboxStr(state.bbox || [])} · ${state.calMode === "anomaly" ? "anomaly vs. median" : state.calMode === "raw" ? "raw sensors (not harmonized)" : "fire cell-days"}`).catch((e) => alert(e.message));
const pngSeason = () => svgToPng($("#season svg"), `plume_seasonality_${bboxStr(state.bbox || [])}.png`,
  `Plume seasonality · ${bboxStr(state.bbox || [])}`).catch((e) => alert(e.message));
$("#png-calendar").addEventListener("click", pngCal);
$("#dl-png-cal").addEventListener("click", pngCal);
$("#png-season").addEventListener("click", pngSeason);
$("#dl-png-season").addEventListener("click", pngSeason);

// validation -------------------------------------------------------------
const METHOD_NAMES = { frp_strata: "FRP-stratified fit", ratio: "simple monthly ratio", quantile: "per-cell quantile mapping" };
let validationKey = null;
async function loadValidation(bbox) {
  const key = bbox || "";
  if (key === validationKey) return;
  validationKey = key;
  const host = $("#validation");
  host.replaceChildren(el("div", { class: "state loading", text: "Loading validation…" }));
  try {
    const v = await getJSON("/validation", { bbox: bbox || null });
    if (validationKey !== key) return;
    const kids = [];
    const mName = METHOD_NAMES[v.method] || v.method;
    const bl = v.baseline;
    const span = (a) => `${a[0]}–${a[1]}`;
    const pct = (x) => (x == null ? "–" : `${fmt(x, 1)}%`);
    const signed = (x) => (x == null ? "–" : `${x > 0 ? "+" : x < 0 ? "−" : ""}${fmt(Math.abs(x), 1)}%`);
    const h = (t) => el("h3", { class: "val-h", text: t });
    const p = (...c) => el("p", { class: "val-p" }, ...c);
    const yearTable = (caption, rows, predLabel) => el("div", { class: "table-scroll" }, el("table", { class: "data methods" },
      el("caption", { text: caption }),
      el("thead", {}, el("tr", {}, ["Year", predLabel, "Observed", "Error"].map((t) => el("th", { scope: "col", text: t })))),
      el("tbody", {}, rows.map((r) => el("tr", {},
        el("th", { scope: "row", text: `${r.year}${r.modis_drift?.length ? ` (${r.modis_drift.join(" + ")} drifting)` : ""}` }),
        el("td", { text: fmt(r.predicted, 0) }), el("td", { text: fmt(r.observed, 0) }), el("td", { text: signed(r.error_pct) })))),
    ));
    const methodTable = (caption, methods) => {
      const best = Math.min(...methods.map((m) => m.annual_abs_error_pct ?? Infinity));
      return el("div", { class: "table-scroll" }, el("table", { class: "data methods" },
        el("caption", { text: caption }),
        el("thead", {}, el("tr", {}, ["Method", "Annual error", "Monthly MAPE", "R²"].map((t) => el("th", { scope: "col", text: t })))),
        el("tbody", {}, methods.map((m) => el("tr", { class: m.method === v.method ? "active" : null },
          el("th", { scope: "row" }, METHOD_NAMES[m.method] || m.method, m.method === v.method ? el("span", { class: "pill", text: "in use" }) : null),
          el("td", { class: m.annual_abs_error_pct === best ? "best" : null, text: pct(m.annual_abs_error_pct) }),
          el("td", { text: pct(m.mape_monthly_pct) }),
          el("td", { text: m.r2 == null ? "–" : m.r2.toFixed(2) }),
        ))),
      ));
    };

    if (v.region_name) kids.push(el("div", { class: "small muted", text: `Validation region: ${v.region_name}${bbox ? " (contains your area’s centre)" : ""}` }));

    // 1. headline: leave-one-year-out vs. the ratio baseline
    if (v.annual_abs_error_pct != null) {
      kids.push(el("p", { class: "headline" }, "Held-out years: converted MODIS lands within ",
        el("span", { class: "accent", text: pct(v.annual_abs_error_pct) }), " of the real VIIRS annual total on average",
        bl?.annual_abs_error_pct != null ? ` (${METHOD_NAMES[bl.method] || bl.method}: ${pct(bl.annual_abs_error_pct)})` : "", "."));
    }
    if (v.test_years) {
      kids.push(p(`Leave-one-year-out: each year in ${span(v.test_years)} is held out in turn, the conversion${mName ? ` (${mName})` : ""} is fit on the other years, and the held-out year’s MODIS is converted and compared with what VIIRS actually measured. `,
        "These years come before the MODIS orbit drift, like the MODIS-only record the conversion is applied to."));
    }
    const row = (label, ours, base, f) => [el("dt", { text: label }),
      el("dd", { text: ours == null ? "–" : f(ours) + (base != null ? `   (ratio baseline ${f(base)})` : "") })];
    kids.push(el("dl", { class: "kv" },
      v.train_years ? [el("dt", { text: "Fit window" }), el("dd", { text: span(v.train_years) })] : null,
      v.test_years ? [el("dt", { text: "Tested on" }), el("dd", { text: `${span(v.test_years)}, each year held out in turn` })] : null,
      v.annual_abs_error_pct != null ? row("Annual error (mean abs.)", v.annual_abs_error_pct, bl?.annual_abs_error_pct, pct) : null,
      v.mape_monthly_pct != null ? row("Monthly error (MAPE)", v.mape_monthly_pct, bl?.mape_monthly_pct, pct) : null,
      v.r2 != null ? row("R² (monthly)", v.r2, bl?.r2, (x) => x.toFixed(2)) : null,
    ));
    if (v.methods?.length > 1) kids.push(methodTable("Every method, scored the same leave-one-year-out way", v.methods));
    if (v.annual?.length) {
      const c = el("div", { style: "margin-top:10px" });
      c.innerHTML = validationSVG(v.annual);
      kids.push(c);
    }

    // 2. spatial holdout
    const sp = v.spatial;
    if (sp?.annual_abs_error_pct != null) {
      kids.push(h("Does it transfer across the region’s grid cells?"));
      kids.push(p(`Spatial holdout${sp.description ? ` (${sp.description})` : ""}: annual error `, el("strong", { text: pct(sp.annual_abs_error_pct) }),
        `, monthly MAPE ${pct(sp.mape_monthly_pct)}`, sp.r2 != null ? `, R² ${sp.r2.toFixed(2)}` : "",
        ". This tests transfer across the region’s grid cells. Neighbouring cells share the same fires, so it is not an independent test, and it is scored on the whole region, not on a small area."));
      if (sp.methods?.length > 1) kids.push(methodTable("Every method on the spatial holdout", sp.methods));
    }

    // 2b. small areas: the conversion scored tile by tile
    const sa = (v.small_area || []).filter((t) => t.median_annual_abs_error_pct != null);
    if (sa.length) {
      const tileKm2 = (deg) => (deg >= 1 ? "≈ 10,000 km²" : deg >= 0.25 ? "≈ 600 km²" : `≈ ${fmt((deg * 111) ** 2, 0)} km²`);
      const whole = (x) => `${Math.round(x)}%`;
      kids.push(h("How well does it work on a small area?"));
      kids.push(p("Less well than on the whole region. The same held-out-year conversion, scored on single tiles instead of the regional total:"));
      kids.push(el("ul", { class: "val-list" }, sa.map((t) => el("li", {},
        `For a single ${t.tile_deg}° tile (${tileKm2(t.tile_deg)}) the median annual error is `, el("strong", { text: whole(t.median_annual_abs_error_pct) }),
        t.p90_annual_abs_error_pct != null ? ` (1 in 10 tile-years worse than ${whole(t.p90_annual_abs_error_pct)})` : "",
        t.band ? `, so areas of that size get the wider band ${t.band[0].toFixed(2)}×–${t.band[1].toFixed(2)}×` : "",
        t.n_tile_years ? ` (${fmt(t.n_tile_years, 0)} tile-years).` : "."))));
      kids.push(p("Areas under ~100,000 km² are given the 1° band and areas under ~3,000 km² the 0.25° band automatically; the bands in the calendar tooltips and CSV are already widened."));
    }

    // 3. drift stress test
    const dt = v.drift_test;
    if (dt?.annual?.length) {
      const df = v.drift_from || {};
      kids.push(h(`Stress test: MODIS orbit drift (${span(dt.years)})`));
      kids.push(p(`Terra has been drifting to earlier overpass times${df.Terra ? ` (since ~${df.Terra})` : ""} and Aqua to later ones${df.Aqua ? ` (free drift since ~${df.Aqua})` : ""}, so late-mission MODIS samples a different part of the daily fire cycle, which may change how many fires it detects. `,
        `Applying the ${v.train_years ? span(v.train_years) : "fit-window"} conversion to these later years gives an annual error of `, el("strong", { text: pct(dt.annual_abs_error_pct) }),
        " and a mean bias of ", el("strong", { text: signed(dt.mean_bias_pct) }),
        dt.mean_bias_pct < 0 ? " (converted MODIS comes out too low; drift can bias it this way, but these few years do not prove the cause)" : "",
        `. The converted record (before ${OVERLAP_START}) predates the drift and these years are measured by VIIRS directly, so this bias is reported as a limit of MODIS, not applied as a correction.`));
      kids.push(yearTable("Drift years: converted MODIS vs. measured VIIRS (fire cell-days per year)", dt.annual, "From MODIS"));
    }

    // 4. uncertainty band
    if (v.band && v.band_coverage_pct != null) {
      kids.push(h("Is the 10–90% band honest?"));
      kids.push(p(`For the region as a whole, MODIS-converted months carry a band of ${v.band[0].toFixed(2)}× to ${v.band[1].toFixed(2)}× the estimate, set from the leave-one-year-out errors (small areas get the wider bands above; the Terra-only and NOAA-20 factors take their bands from year-to-year spread instead). Measured on the ${v.band_coverage_on || "held-out months"}, `,
        el("strong", { text: pct(v.band_coverage_pct) }), " of months fell inside it. A perfectly calibrated 10–90% band would hold 80%",
        v.band_coverage_pct < 80 ? ", so on those years it is somewhat too narrow." : "."));
    }

    // 5. NOAA-20 → S-NPP bridge
    const nb = v.noaa20_bridge;
    if (nb?.annual?.length) {
      kids.push(h("NOAA-20 → Suomi NPP bridge"));
      kids.push(p("Suomi NPP stopped delivering observations on 2 November 2026, so later months (and earlier months with a Suomi NPP outage) come from the VIIRS on NOAA-20, scaled to Suomi NPP units with a monthly factor. ",
        `Checked on ${span(nb.years)}, when both satellites flew (each year held out in turn): annual error `, el("strong", { text: pct(nb.annual_abs_error_pct) }), "."));
      kids.push(yearTable("Converted NOAA-20 vs. measured Suomi NPP (fire cell-days per year)", nb.annual, "From NOAA-20"));
    }

    // 6. Terra-only scaling
    const ts = Object.values(v.terra_scale || {});
    if (ts.length) {
      const aqua = state.health?.aqua_first_month;
      kids.push(p(el("strong", { text: "Terra-only months. " }),
        `${aqua ? `Before ${aqua}` : "Before Aqua launched"} only Terra carried MODIS. Those months are first scaled up to Terra+Aqua by a monthly factor (${Math.min(...ts).toFixed(2)}× to ${Math.max(...ts).toFixed(2)}×) learned from years when both flew, then converted like the rest.`));
    }
    host.replaceChildren(...kids);
  } catch (e) {
    host.replaceChildren(el("div", { class: "state err", text: `Validation unavailable: ${e.message}` }));
  }
}

// ───────────────────────── landing ─────────────────────────
// Shown on a plain visit; a URL that already carries an area goes straight to the app.
const landing = $("#landing"), appRoot = $("#app");
const HERO_BBOX = [-124.5, 32.5, -114.0, 42.0]; // California: the calendar drawn behind the headline
const baseUrl = () => { const a = new URLSearchParams(location.search).get("api"); return location.pathname + (a ? `?api=${encodeURIComponent(a)}` : ""); };
let landingLoaded = false;
function showLanding(load = true) {
  landing.hidden = false;
  appRoot.inert = true;
  document.title = "Plume · every fire season since 2000, one record";
  if (load && !landingLoaded) { landingLoaded = true; loadLanding(); }
}
function hideLanding() {
  landing.hidden = true;
  appRoot.inert = false;
  document.title = "Plume · burning calendar";
  map.invalidateSize();
}
function openApp() {
  history.pushState(null, "", location.href); // Back returns to the landing page
  hideLanding();
  if (!state.area) {
    const ca = state.regions.find((r) => r.id === "california");
    setBbox(ca ? ca.bbox : HERO_BBOX);
    setFormOpen(false);
  }
  if (state.health?.data_ready) analyze(); else drawSelection(state.area);
  areaToggle.focus({ preventScroll: true });
}
$("#lp-open").addEventListener("click", openApp);
$("#lp-open-2").addEventListener("click", openApp);
$("#lp-how-link").addEventListener("click", (e) => {
  e.preventDefault();
  const t = $("#lp-how");
  t.scrollIntoView({ behavior: REDUCED.matches ? "auto" : "smooth", block: "start" });
  t.focus({ preventScroll: true });
});
$("#brand-home").addEventListener("click", (e) => {
  e.preventDefault();
  history.pushState(null, "", baseUrl());
  landing.scrollTop = 0;
  showLanding();
  $("#lp-open").focus();
});
addEventListener("popstate", () => {
  if (parseAreaQuery(new URLSearchParams(location.search))) hideLanding(); else showLanding();
});

const ymFrac = (ym) => Number(ym.slice(0, 4)) + (Number(ym.slice(5, 7)) - 1) / 12;
/** Sensor timeline: start dates come from /health when it answers; the fallbacks are the launch-era facts. */
function renderTimeline(h) {
  const t0 = 2000.5, t1 = 2028, pos = (ym) => `${(((ymFrac(ym) - t0) / (t1 - t0)) * 100).toFixed(2)}%`;
  const SNPP_END = "2026-11"; // Suomi NPP stopped delivering observations on 2 November 2026
  const rows = [
    { name: "Terra", sub: "MODIS · 1 km", cls: "modis", from: h?.coverage?.from || "2000-11" },
    { name: "Aqua", sub: "MODIS · 1 km", cls: "modis", from: h?.aqua_first_month || "2002-07" },
    { name: "Suomi NPP", sub: "VIIRS · 375 m", cls: "viirs", from: (h?.sensors?.VIIRS?.[0] || "2012-01").slice(0, 7), to: SNPP_END },
    { name: "NOAA-20", sub: "VIIRS · 375 m", cls: "viirs", from: (h?.sensors?.NOAA20?.[0] || "2018-04").slice(0, 7) },
  ];
  const handoff = rows[2].from;
  $("#lp-tl").replaceChildren(
    el("div", { class: "lp-tl-rows", role: "list" },
      rows.map((r) => el("div", { class: "lp-tl-row", role: "listitem" },
        el("div", { class: "lp-tl-name" }, r.name, el("small", { text: r.sub })),
        el("div", { class: "lp-tl-track" }, el("div", {
          class: `lp-tl-bar ${r.cls} ${r.to ? "ends" : "open"}`,
          style: `left:${pos(r.from)};${r.to ? `width:calc(${pos(r.to)} - ${pos(r.from)})` : "right:0"}`,
          text: r.to ? `${ymLabel(r.from)} → ends 2 ${ymLabel(r.to)}` : `${ymLabel(r.from)} →`,
        })))),
      el("div", { class: "lp-tl-marks", "aria-hidden": "true" },
        el("i", { class: "lp-tl-mark", style: `left:${pos(handoff)}` }),
        el("i", { class: "lp-tl-mark crit", style: `left:${pos(SNPP_END)}` }))),
    el("div", { class: "lp-tl-axis", "aria-hidden": "true" }, [2005, 2010, 2015, 2020, 2025].map((y) => el("span", { style: `left:${pos(`${y}-01`)}`, text: y }))),
    el("div", { class: "lp-tl-notes" },
      el("span", { class: "lp-flag warn" }, el("b", { text: `Handoff · ${ymLabel(handoff)}` }), "MODIS-only years are converted to VIIRS units"),
      el("span", { class: "lp-flag crit" }, el("b", { text: "S-NPP ends · 2 Nov 2026" }), "NOAA-20 carries the record on, scaled to S-NPP units")),
  );
}

/** Background: the real California burning calendar, one column per year and one row per month. */
function renderHeroCalendar(d) {
  const months = d.months || [];
  if (!months.length) return;
  const by = new Map(months.map((m) => [`${m.year}-${m.month}`, m.value]));
  const years = [...new Set(months.map((m) => m.year))].sort((a, b) => a - b);
  const max = Math.max(0, ...months.map((m) => m.value ?? 0));
  const cells = [];
  years.forEach((y, c) => {
    for (let m = 1; m <= 12; m++) {
      const v = by.get(`${y}-${m}`);
      cells.push(el("i", { style: `${v != null ? `background:${viridis(countT(v, max))};` : ""}animation-delay:${c * 45 + m * 22}ms` }));
    }
  });
  $("#lp-cal").replaceChildren(...cells);
  $("#lp-calnote").textContent = `Behind: California’s burning calendar, ${years[0]}–${years.at(-1)} · one column per year, one row per month`;
}

/** Every number on the landing page comes from the API; a figure that is not available is left out. */
async function loadLanding() {
  renderTimeline(state.health);
  const bbox = bboxStr(HERO_BBOX);
  const stats = [], facts = { 1: null, 2: null, 3: null };
  const paint = () => {
    $("#lp-stats").replaceChildren(...stats.filter(Boolean).map(([v, label]) => el("div", { class: "lp-stat" }, el("b", { text: v }), el("span", { text: label }))));
    for (const [k, f] of Object.entries(facts)) $(`#lp-fact-${k}`).replaceChildren(...(f ? [el("b", { text: f[0] }), f[1]] : []));
  };
  const h = state.health;
  if (h?.data_ready) {
    const from = h.coverage?.from, to = (h.max_date || h.coverage?.to || "").slice(0, 7);
    if (validYM(from) && validYM(to)) stats[0] = [`${ymLabel(from)} – ${ymLabel(to)}`, `continuous record${h.synthetic ? " (demo data)" : ""}`];
    const n = Object.keys(h.sensors || {}).length;
    if (n) stats[1] = [String(n), "sensor records fused"];
    paint();
  }
  if (!h?.data_ready) return;
  const from = h.coverage?.from, to = h.coverage?.to;
  getJSON("/calendar", { bbox, from, to }).then((d) => {
    renderHeroCalendar(d);
    if (d.summary?.total != null) stats[2] = [fmt(d.summary.total, 0), "fire cell-days in California"];
    if (d.months?.length) facts[2] = [fmt(d.months.length, 0), "months in California’s calendar"];
    if (d.handoff?.viirs_per_modis != null && !facts[1]) facts[1] = [fmtX(d.handoff.viirs_per_modis), "VIIRS per MODIS cell-day, California"];
    paint();
  }).catch((e) => console.warn("landing calendar:", e.message));
  getJSON("/validation", { bbox }).then((v) => {
    if (v.annual_abs_error_pct != null) {
      stats[3] = [`${fmt(v.annual_abs_error_pct, 1)}%`, "annual error on held-out years"];
      facts[1] = [`${fmt(v.annual_abs_error_pct, 1)}%`, `annual error, held-out years${v.region_name ? ` (${v.region_name})` : ""}`];
      paint();
    }
  }).catch((e) => console.warn("landing validation:", e.message));
  getJSON("/current", { bbox }).then((c) => {
    if (c?.ratio != null && c.median > 0) {
      facts[3] = [`${fmtRatio(c.ratio)} normal`, `California, ${MONTHS[c.month - 1]} ${c.year}${c.partial ? " so far" : ""}`];
      paint();
    }
  }).catch((e) => console.warn("landing current:", e.message));
}

// ───────────────────────── boot ─────────────────────────
function showBanner(html, err = false) {
  const b = $("#banner");
  b.innerHTML = html;
  b.classList.toggle("err", err);
  b.hidden = false;
}

// Outage ranges per sensor group from /health, one line per sensor in "How we harmonize".
const OUTAGE_NAMES = { MODIS: "MODIS (Terra and Aqua together)", TERRA: "Terra", AQUA: "Aqua", VIIRS: "S-NPP", NOAA20: "NOAA-20" };
function outageRange([a, b]) {
  const d = (s) => Number(s.slice(8, 10)), mon = (s) => MONTHS[Number(s.slice(5, 7)) - 1], y = (s) => s.slice(0, 4);
  if (a === b) return `${d(a)} ${mon(a)} ${y(a)}`;
  if (y(a) !== y(b)) return `${d(a)} ${mon(a)} ${y(a)}–${d(b)} ${mon(b)} ${y(b)}`;
  return a.slice(5, 7) === b.slice(5, 7) ? `${d(a)}–${d(b)} ${mon(b)} ${y(b)}` : `${d(a)} ${mon(a)}–${d(b)} ${mon(b)} ${y(b)}`;
}
function renderOutages(outages) {
  const host = $("#how-outages");
  const rows = Object.entries(outages || {}).filter(([, r]) => r?.length);
  if (!host || !rows.length) return;
  host.replaceChildren(
    el("strong", { text: "Outages. " }),
    "Days when a satellite delivered no data are detected from the archive. They are kept out of the fits and the validation, and in the record the missing days of a month are filled from the days that were observed (or, for Suomi NPP, from NOAA-20 when it saw more of the month). The affected calendar cells say so.",
    el("ul", { class: "val-list" }, rows.map(([k, r]) => el("li", {}, el("strong", { text: `${OUTAGE_NAMES[k] || k}: ` }), r.map(outageRange).join(", ")))),
  );
  host.hidden = false;
}

async function loadRegions() {
  const host = $("#presets");
  try {
    const regions = await getJSON("/regions");
    state.regions = regions;
    if (!regions.length) return host.replaceChildren(el("span", { class: "small muted", text: "No presets." }));
    host.replaceChildren(...regions.map((r) => el("button", {
      type: "button", class: "chip", "aria-pressed": "false", "data-bbox": bboxStr(r.bbox), text: r.name,
      onclick: () => { setBbox(r.bbox); showFormError(""); setFormOpen(false); analyze(); },
    })));
    if (state.area) setArea(state.area); // refresh pressed state
  } catch (e) {
    host.replaceChildren(el("span", { class: "small muted", text: e.status === 404 ? "No presets available." : "Presets unavailable." }));
  }
}

async function boot() {
  const q = new URLSearchParams(location.search);
  const urlArea = parseAreaQuery(q);
  const urlFrom = validYM(q.get("from")) ? q.get("from") : null;
  const urlTo = validYM(q.get("to")) ? q.get("to") : null;
  const urlRegion = urlArea ? null : q.get("region"); // ?region=<id> selects a quick-pick region
  const direct = !!(urlArea || urlRegion);
  setFormOpen(!direct && !NARROW.matches);
  if (direct) hideLanding(); else showLanding(false);

  let health;
  try {
    health = await getJSON("/health");
  } catch (e) {
    $("#coverage").innerHTML = $("#lp-coverage").innerHTML = `<span class="dot bad"></span>API offline`;
    if (!direct) showLanding();
    showBanner(`Can't reach the Plume API at <code>${API.replace(/</g, "&lt;")}</code>. Start it with <code>uvicorn index:app --port 8000</code>, or pass <code>?api=URL</code>.`, true);
    if (urlArea) { setArea(urlArea); drawSelection(state.area); }
    fields.from.value = urlFrom || "2000-11"; fields.to.value = urlTo || nowYM();
    return;
  }
  state.health = health;
  state.coverage = health.coverage || null;
  const covFrom = state.coverage?.from || "2000-11", covTo = state.coverage?.to || nowYM();
  const recordSpan = `${ymLabel(covFrom)} – ${ymLabel((health.max_date || covTo).slice(0, 7))}`;
  if (health.data_ready) document.querySelectorAll(".record-span").forEach((n) => (n.textContent = recordSpan));
  renderOutages(health.outages);
  fields.from.min = fields.to.min = covFrom;
  fields.from.max = fields.to.max = covTo;
  fields.from.value = urlFrom || covFrom;
  fields.to.value = urlTo || covTo;
  $("#coverage").innerHTML = health.data_ready
    ? `<span class="dot ok"></span>Data ${recordSpan}${health.synthetic ? " · demo" : ""}`
    : `<span class="dot bad"></span>No data built`;
  $("#lp-coverage").innerHTML = $("#coverage").innerHTML;

  if (health.method === "ratio") {
    const p = document.querySelector("#how-ratio");
    if (p) p.innerHTML = "<strong>Learn the conversion.</strong> On the overlap years (2012–2019) we fit a VIIRS ÷ MODIS ratio for each calendar month, with a 10–90% band.";
  }
  if (!health.data_ready) {
    showBanner(`No harmonized data yet. Build it, then reload: <code>python -m pipeline.run data/raw/&lt;region&gt;</code> (or a demo: <code>python -m pipeline.demo &amp;&amp; python -m pipeline.run data/raw/demo</code>).`, true);
  } else if (health.synthetic) {
    showBanner(`<strong>Demo data:</strong> synthetic FIRMS-format hotspots. Run the pipeline on real FIRMS archives to replace.`);
  }

  renderAreaCard();
  if (!direct) showLanding();
  const regionsP = loadRegions();
  if (urlRegion) regionsP.then(() => {
    const r = state.regions.find((x) => x.id === urlRegion);
    if (r && !state.area) { setBbox(r.bbox); if (health.data_ready) analyze(); else drawSelection(state.area); }
  });
  if (!urlArea) loadValidation();
  if (urlArea) {
    setArea(urlArea);
    if (health.data_ready) analyze();
    else drawSelection(state.area);
  }
}

boot();
