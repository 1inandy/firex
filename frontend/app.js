import {
  MONTHS, MONTHS_LONG, viridis, countT, fmt, fmtRatio, niceTicks,
  calendarSVG, cellLabel, seasonalitySVG, validationSVG, svgToPng, calValue, OVERLAP_START,
  RAW_NAMES, sourceNote, rawNote,
} from "./charts.js";

// ?api= wins. Local dev (frontend served on its own port) talks to the API on :8000; everywhere else
// the API is mounted same-origin under /api (production).
const LOCAL_DEV = ["localhost", "127.0.0.1"].includes(location.hostname) && location.port !== "8000";
const API = (new URLSearchParams(location.search).get("api") || (LOCAL_DEV ? "http://localhost:8000" : `${location.origin}/api`)).replace(/\/$/, "");
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
const map = L.map("map", { preferCanvas: true, worldCopyJump: true, zoomSnap: 0.25 }).setView([37, -119.5], 5);
// Esri dark-gray canvas (no API key needed); reference labels drawn above the data layers.
const ESRI = "https://server.arcgisonline.com/ArcGIS/rest/services/Canvas";
L.tileLayer(`${ESRI}/World_Dark_Gray_Base/MapServer/tile/{z}/{y}/{x}`, {
  attribution: 'Tiles &copy; <a href="https://www.esri.com">Esri</a>, HERE, Garmin, &copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
  maxZoom: 16,
}).addTo(map);
map.createPane("labels").style.zIndex = 450;
map.getPane("labels").style.pointerEvents = "none";
L.tileLayer(`${ESRI}/World_Dark_Gray_Reference/MapServer/tile/{z}/{y}/{x}`, { pane: "labels", maxZoom: 16 }).addTo(map);
new ResizeObserver(() => map.invalidateSize()).observe(document.getElementById("map"));

const ACCENT = "#f0a04b";
const SHAPE = { color: ACCENT, weight: 2, fill: false, dashArray: "6 4" };
const drawn = new L.FeatureGroup().addTo(map);
const cellsLayer = L.layerGroup();
const eventLayer = L.layerGroup().addTo(map);
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
  analyze();
});

// cell layer legend
const legend = L.control({ position: "bottomright" });
let legendDiv;
legend.onAdd = () => (legendDiv = L.DomUtil.create("div", "map-legend"));
legend.addTo(map);
legendDiv.hidden = true;
// Map ramp starts at 0.2 so the darkest viridis purples stay visible on the dark basemap.
const mapT = (v, max) => 0.2 + 0.8 * countT(v, max);
function renderMapLegend(max) {
  if (!max) { legendDiv.hidden = true; return; }
  const stops = Array.from({ length: 11 }, (_, i) => viridis(0.2 + 0.8 * (i / 10))).join(",");
  const ticks = niceTicks(max, 3).filter((v, i, a) => i === 0 || countT(v, max) - countT(a[i - 1], max) > 0.3);
  legendDiv.innerHTML = "";
  legendDiv.append(
    el("div", { text: "Harmonized fire cell-days per 0.05° cell" }),
    el("div", { class: "bar", style: `background:linear-gradient(90deg,${stops})`, role: "img", "aria-label": `Color scale from 0 to ${fmt(max)} harmonized fire cell-days, square-root` }),
    el("div", { class: "ticks", style: "position:relative;height:12px;display:block" },
      ticks.map((v) => el("span", { style: `position:absolute;left:${countT(v, max) * 100}%;transform:translateX(-50%)`, text: fmt(v) })),
    ),
  );
  legendDiv.hidden = !$("#cells-toggle").checked;
}
$("#cells-toggle").addEventListener("change", (e) => {
  if (e.target.checked) { cellsLayer.addTo(map); if (legendDiv.innerHTML) legendDiv.hidden = false; }
  else { map.removeLayer(cellsLayer); legendDiv.hidden = true; }
});
if ($("#cells-toggle").checked) cellsLayer.addTo(map);

function drawSelection(a, fit = true) {
  drawn.clearLayers();
  const opts = { ...SHAPE, interactive: false };
  const b = a.bbox;
  const shape = a.kind === "circle" ? L.circle([a.circle[1], a.circle[0]], { ...opts, radius: a.circle[2] * 1000 })
    : a.kind === "poly" ? L.polygon(a.poly.map(([x, y]) => [y, x]), opts)
    : L.rectangle([[b[1], b[0]], [b[3], b[2]]], opts);
  drawn.addLayer(shape);
  map.invalidateSize();
  if (fit) map.fitBounds(L.latLngBounds([[b[1], b[0]], [b[3], b[2]]]), { padding: [24, 24], maxZoom: 10, animate: !matchMedia("(prefers-reduced-motion: reduce)").matches });
}

// ───────────────────────── form ─────────────────────────
const form = $("#area-form");
const fields = { w: $("#f-w"), s: $("#f-s"), e: $("#f-e"), n: $("#f-n"), lat: $("#f-lat"), lon: $("#f-lon"), km: $("#f-km"), from: $("#f-from"), to: $("#f-to") };
const mode = () => form.querySelector("input[name=mode]:checked").value;
form.querySelectorAll("input[name=mode]").forEach((r) => r.addEventListener("change", () => {
  form.querySelectorAll(".mode-pane").forEach((p) => (p.hidden = p.dataset.mode !== mode()));
}));

function showFormError(msg, field) {
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
    showFormError("No data built yet. See the banner above for setup steps.");
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
  drawSelection(state.area, fit);
  eventLayer.clearLayers();

  $("#empty").hidden = true;
  $("#cards").hidden = false;
  updateDownloads(params);
  loadValidation(bbox);
  for (const id of ["card-current", "card-precedent", "card-summary", "card-events"]) setLoading(id);
  setLoading("card-calendar", "Loading calendar…");
  setLoading("card-season");
  $("#cal-table").replaceChildren();
  $("#current-source").textContent = "";
  $("#events-count").textContent = "";
  body("card-brief").replaceChildren(el("div", { class: "state", text: "Generate a briefing for this area on demand." }));
  $("#brief-btn").disabled = false;

  const live = (fn) => (v) => { if (gen === state.gen) fn(v); };
  const fail = (id) => (e) => { if (gen === state.gen && e.name !== "AbortError") setError(id, e); };

  const calP = getJSON("/calendar", params).then(live((d) => { state.calendar = d; renderCalendar(); renderSummary(); }))
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
  const b = body("card-current");
  if (!d || d.value == null) return setEmpty("card-current", "No current-month data for this area.");
  $("#current-source").textContent = d.live ? "Live · NOAA-20 NRT" : "Archive";
  $("#current-source").classList.toggle("live", !!d.live);
  const mName = MONTHS_LONG[d.month - 1];
  const kids = [
    el("div", { class: "cur-head", text: `${mName} ${d.year}${d.partial ? ` · first ${d.days_covered ?? "?"} days` : ""}` }),
    el("div", { class: "cur-big" }, el("span", { class: "cur-value", text: fmt(d.value) }), el("span", { class: "cur-unit", text: "fire cell-days" })),
  ];
  let verdict, cls = "";
  const isRecord = d.highest_since == null && d.rank === 1;
  if (!(d.median > 0) || d.ratio == null) {
    verdict = d.value > 0 ? `No fires in a typical ${mName}; this is unusual${isRecord ? ", the highest on record" : ""}.` : `Quiet, as usual: a typical ${mName} has no fire activity here.`;
    cls = d.value > 0 ? "hot" : "";
  } else if (d.ratio > 1.15) {
    verdict = `${fmtRatio(d.ratio)} normal`;
    if (isRecord) verdict += `, the highest ${mName} on record`;
    else if (d.highest_since != null) verdict += `, highest since ${d.highest_since}`;
    cls = "hot";
  } else if (d.ratio < 0.85) {
    verdict = `${fmtRatio(d.ratio)} normal, quieter than usual`;
    cls = "cool";
  } else {
    verdict = `${fmtRatio(d.ratio)} normal, about typical for ${mName}`;
  }
  kids.push(el("div", { class: `cur-verdict ${cls}`, text: verdict + "." }));
  kids.push(el("div", { class: "cur-meta" }, "Historical median for ", d.partial ? `the same ${d.days_covered ?? ""} days of ${mName}` : mName, ": ", el("strong", { text: fmt(d.median) })));
  if (d.rank != null && d.n_years) kids.push(el("div", { class: "cur-meta" }, "Ranks ", el("strong", { text: `#${d.rank}` }), ` of ${d.n_years} ${mName}s on record.`));
  if (d.median > 0 && d.ratio != null) {
    // log-scale ratio bar, ¼× … 4×, 1× in the middle
    const pos = Math.max(0, Math.min(1, (Math.log2(Math.max(d.ratio, 1e-3)) + 2) / 4));
    kids.push(el("div", { class: "ratio-bar", role: "img", "aria-label": `${fmtRatio(d.ratio)} the historical median` },
      el("div", { class: "fill", style: `width:${pos * 100}%;background:${d.ratio >= 1 ? "var(--accent)" : "#6b9bc7"}` }),
      el("div", { class: "mid", style: "left:calc(50% - 1px)" })));
    kids.push(el("div", { class: "ratio-ticks", "aria-hidden": "true" }, ["¼×", "½×", "1× median", "2×", "4×+"].map((t) => el("span", { text: t }))));
  }
  if (d.partial) kids.push(el("div", { class: "note", text: `Month in progress: ${d.days_covered ?? "some"} days of data so far, compared with days 1–${d.days_covered ?? "?"} of ${mName} in past years. The total will rise as the month continues.` }));
  if (d.note) kids.push(el("div", { class: "note", text: d.note }));
  b.replaceChildren(...kids);
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
  if (lg?.bbox) kids.push(el("button", { type: "button", class: "btn btn-ghost btn-sm", style: "margin-top:8px", onclick: () => showEvent(lg), text: "Show it on the map" }));
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
    " as many fire cell-days as MODIS over this area: that is the sensor difference, not more fire.",
  ];
  if (h.raw_jump != null && h.harmonized_jump != null && h.before_years && h.after_years) {
    kids.push(` Stitched together raw, the record jumps `, el("strong", { text: fmtX(h.raw_jump) }),
      ` at ${h.after_years[0]} (${span(h.after_years)} vs. ${span(h.before_years)}); harmonized, `,
      el("strong", { text: fmtX(h.harmonized_jump) }), ". Real year-to-year change is part of both numbers.");
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
          el("div", { class: "t-muted", text: rec.anomaly != null ? `${fmtRatio(rec.anomaly)} the ${MONTHS[m - 1]} median (${fmt(med)})${rec.partial ? ", provisional" : ""}` : `${MONTHS[m - 1]} median: ${fmt(med)}` }),
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
  const n20 = d.months.some((r) => r.source === "noaa20_converted");
  const n20Note = n20 ? ` Months after ${state.health?.snpp_end || "the end of Suomi NPP"}: NOAA-20 ${raw ? "VIIRS" : "converted to Suomi NPP units"}.` : "";
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
  const stats = el("div", { class: "stats" },
    s.peak_year ? stat("Peak year", String(s.peak_year.year), `${fmt(s.peak_year.value)} cell-days`) : null,
    s.peak_month ? stat("Peak month", `${MONTHS[s.peak_month.month - 1]} ${s.peak_month.year}`, `${fmt(s.peak_month.value)} cell-days`) : null,
    s.typical_peak_month ? stat("Typical peak", MONTHS_LONG[s.typical_peak_month - 1], "highest median month") : null,
    stat("Trend", trend == null ? "–" : `${trend > 0 ? "+" : ""}${fmt(trend, 1)}%`, trend == null ? "too few years" : "per decade"),
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
      radius: r, stroke: false, fillColor: viridis(mapT(c.cell_days, max)), fillOpacity: 0.9,
    }).bindTooltip(`${fmt(c.cell_days)} harmonized fire cell-days<br><span style="color:#8b95a1">${c.lat.toFixed(3)}, ${c.lon.toFixed(3)}</span>`, { direction: "top" })
      .addTo(cellsLayer);
  }
  renderMapLegend(max);
}
map.on("zoomend", () => { const r = cellRadius(); cellsLayer.eachLayer((l) => l.setRadius(r)); });

// events -------------------------------------------------------------
function renderEvents(d) {
  const evs = d.events || [];
  $("#events-count").textContent = evs.length ? `${evs.length} shown` : "";
  if (!evs.length) return setEmpty("card-events", "No fire events in this area and date range.");
  const list = el("div", { class: "events", role: "list" },
    el("div", { class: "events-head", "aria-hidden": "true" }, ["Start", "Days", "Footprint km²", "Peak FRP", "Cell-days"].map((t) => el("span", { text: t }))),
    evs.slice(0, 15).map((ev) => el("div", { role: "listitem" }, el("button", {
      type: "button", class: "event", "aria-pressed": "false",
      "aria-label": `Fire event first detected ${ev.start}, detected over ${ev.duration_days} days, ${fmt(ev.cell_days, 0)} fire cell-days, detection footprint ${fmt(ev.area_km2)} square kilometres, peak FRP ${fmt(ev.peak_frp, 0)} megawatts. Show on map.`,
      onclick: (e) => {
        document.querySelectorAll(".event").forEach((b) => b.setAttribute("aria-pressed", "false"));
        e.currentTarget.setAttribute("aria-pressed", "true");
        showEvent(ev);
      },
    }, el("span", { text: ev.start }), el("span", { text: fmt(ev.duration_days, 0) }), el("span", { text: fmt(ev.area_km2, 0) }),
    el("span", { text: ev.peak_frp != null ? `${fmt(ev.peak_frp, 0)} MW` : "–" }), el("span", { text: fmt(ev.cell_days, 0) })))),
  );
  body("card-events").replaceChildren(list);
}

function showEvent(ev) {
  eventLayer.clearLayers();
  const b = ev.bbox;
  if (!b) return;
  const rect = L.rectangle([[b[1], b[0]], [b[3], b[2]]], { color: "#ffffff", weight: 2, fillColor: ACCENT, fillOpacity: 0.12 })
    .bindTooltip(`Detections from ${ev.start} to ${ev.end || "?"} · over ${fmt(ev.duration_days, 0)} days · footprint ${fmt(ev.area_km2, 0)} km²`, { permanent: false })
    .addTo(eventLayer);
  map.flyToBounds(rect.getBounds(), { padding: [40, 40], maxZoom: 11, duration: matchMedia("(prefers-reduced-motion: reduce)").matches ? 0 : 0.8 });
}

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
      kids.push(h("Does it work on areas it never saw?"));
      kids.push(p(`Spatial holdout${sp.description ? ` (${sp.description})` : ""}: annual error `, el("strong", { text: pct(sp.annual_abs_error_pct) }),
        `, monthly MAPE ${pct(sp.mape_monthly_pct)}`, sp.r2 != null ? `, R² ${sp.r2.toFixed(2)}` : "",
        ". This matters because you draw your own area: the fit has to transfer to cells it was not trained on."));
      if (sp.methods?.length > 1) kids.push(methodTable("Every method on the spatial holdout", sp.methods));
    }

    // 3. drift stress test
    const dt = v.drift_test;
    if (dt?.annual?.length) {
      const sats = v.drift_from ? Object.entries(v.drift_from).map(([s, y]) => `${s} since ~${y}`).join(", ") : "";
      kids.push(h(`Stress test: MODIS orbit drift (${span(dt.years)})`));
      kids.push(p(`Terra and Aqua have been drifting to earlier overpass times${sats ? ` (${sats})` : ""}, so late-mission MODIS sees a cooler part of the daily fire cycle and fewer fires. `,
        `Applying the ${v.train_years ? span(v.train_years) : "fit-window"} conversion to these later years gives an annual error of `, el("strong", { text: pct(dt.annual_abs_error_pct) }),
        " and a mean bias of ", el("strong", { text: signed(dt.mean_bias_pct) }),
        dt.mean_bias_pct < 0 ? " (converted MODIS comes out too low, the direction drift would push it)" : "",
        `. The converted record (before ${OVERLAP_START}) predates the drift and these years are measured by VIIRS directly, so this bias is reported as a limit of MODIS, not applied as a correction.`));
      kids.push(yearTable("Drift years: converted MODIS vs. measured VIIRS (fire cell-days per year)", dt.annual, "From MODIS"));
    }

    // 4. uncertainty band
    if (v.band && v.band_coverage_pct != null) {
      kids.push(h("Is the 10–90% band honest?"));
      kids.push(p(`Converted months carry a band of ${v.band[0].toFixed(2)}× to ${v.band[1].toFixed(2)}× the estimate, set from the leave-one-year-out errors. Measured on the ${v.band_coverage_on || "held-out months"}, `,
        el("strong", { text: pct(v.band_coverage_pct) }), " of months fell inside it. A perfectly calibrated 10–90% band would hold 80%",
        v.band_coverage_pct < 80 ? ", so on those years it is somewhat too narrow." : "."));
    }

    // 5. NOAA-20 → S-NPP bridge
    const nb = v.noaa20_bridge;
    if (nb?.annual?.length) {
      kids.push(h("NOAA-20 → Suomi NPP bridge"));
      kids.push(p("Suomi NPP stopped delivering observations on 2 November 2026, so later months come from the VIIRS on NOAA-20, scaled to Suomi NPP units with a monthly factor. ",
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

// ───────────────────────── boot ─────────────────────────
function showBanner(html, err = false) {
  const b = $("#banner");
  b.innerHTML = html;
  b.classList.toggle("err", err);
  b.hidden = false;
}

async function loadRegions() {
  const host = $("#presets");
  try {
    const regions = await getJSON("/regions");
    state.regions = regions;
    if (!regions.length) return host.replaceChildren(el("span", { class: "small muted", text: "No presets." }));
    host.replaceChildren(...regions.map((r) => el("button", {
      type: "button", class: "chip", "aria-pressed": "false", "data-bbox": bboxStr(r.bbox), text: r.name,
      onclick: () => { setBbox(r.bbox); showFormError(""); analyze(); },
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

  let health;
  try {
    health = await getJSON("/health");
  } catch (e) {
    $("#coverage").innerHTML = `<span class="dot bad"></span>API offline`;
    showBanner(`Can't reach the Plume API at <code>${API.replace(/</g, "&lt;")}</code>. Start it with <code>uvicorn backend.app.main:app --port 8000</code>, or pass <code>?api=URL</code>.`, true);
    if (urlArea) { setArea(urlArea); drawSelection(state.area); }
    fields.from.value = urlFrom || "2001-01"; fields.to.value = urlTo || nowYM();
    return;
  }
  state.health = health;
  state.coverage = health.coverage || null;
  const covFrom = state.coverage?.from || "2001-01", covTo = state.coverage?.to || nowYM();
  fields.from.min = fields.to.min = covFrom;
  fields.from.max = fields.to.max = covTo;
  fields.from.value = urlFrom || covFrom;
  fields.to.value = urlTo || covTo;
  $("#coverage").innerHTML = health.data_ready
    ? `<span class="dot ok"></span>Data ${covFrom} → ${health.max_date || covTo}${health.synthetic ? " · demo" : ""}`
    : `<span class="dot bad"></span>No data built`;

  if (health.method === "ratio") {
    const p = document.querySelector("#how-ratio");
    if (p) p.innerHTML = "<strong>Learn the conversion.</strong> On the overlap years (2012–2019) we fit a VIIRS ÷ MODIS ratio for each calendar month, with a 10–90% band.";
  }
  if (!health.data_ready) {
    showBanner(`No harmonized data yet. Build it, then reload: <code>python -m pipeline.run data/raw/&lt;region&gt;</code> (or a demo: <code>python -m pipeline.demo &amp;&amp; python -m pipeline.run data/raw/demo</code>).`, true);
  } else if (health.synthetic) {
    showBanner(`<strong>Demo data:</strong> synthetic FIRMS-format hotspots. Run the pipeline on real FIRMS archives to replace.`);
  }

  loadRegions();
  if (!urlArea) loadValidation();
  if (urlArea) {
    setArea(urlArea);
    if (health.data_ready) analyze();
    else drawSelection(state.area);
  }
}

boot();
