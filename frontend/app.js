import {
  MONTHS, MONTHS_LONG, viridis, countT, fmt, fmtRatio, niceTicks,
  calendarSVG, cellLabel, seasonalitySVG, validationSVG, svgToPng,
} from "./charts.js";

const API = (new URLSearchParams(location.search).get("api") || "http://localhost:8000").replace(/\/$/, "");
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
  bbox: null,           // [W,S,E,N]
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
const drawn = new L.FeatureGroup().addTo(map);
const cellsLayer = L.layerGroup();
const eventLayer = L.layerGroup().addTo(map);
map.addControl(new L.Control.Draw({
  position: "topleft",
  draw: {
    rectangle: { shapeOptions: { color: ACCENT, weight: 2, fill: false, dashArray: "6 4" }, showArea: false },
    polygon: false, polyline: false, circle: false, marker: false, circlemarker: false,
  },
  edit: false,
}));
map.on(L.Draw.Event.CREATED, (e) => {
  const b = e.layer.getBounds();
  setBbox([b.getWest(), b.getSouth(), b.getEast(), b.getNorth()]);
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
    el("div", { text: "Fire cell-days per 0.05° cell" }),
    el("div", { class: "bar", style: `background:linear-gradient(90deg,${stops})`, role: "img", "aria-label": `Color scale from 0 to ${fmt(max, 0)} fire cell-days, square-root` }),
    el("div", { class: "ticks", style: "position:relative;height:12px;display:block" },
      ticks.map((v) => el("span", { style: `position:absolute;left:${countT(v, max) * 100}%;transform:translateX(-50%)`, text: fmt(v, 0) })),
    ),
  );
  legendDiv.hidden = !$("#cells-toggle").checked;
}
$("#cells-toggle").addEventListener("change", (e) => {
  if (e.target.checked) { cellsLayer.addTo(map); if (legendDiv.innerHTML) legendDiv.hidden = false; }
  else { map.removeLayer(cellsLayer); legendDiv.hidden = true; }
});
if ($("#cells-toggle").checked) cellsLayer.addTo(map);

function drawSelection(b, fit = true) {
  drawn.clearLayers();
  const rect = L.rectangle([[b[1], b[0]], [b[3], b[2]]], { color: ACCENT, weight: 2, fill: false, dashArray: "6 4", interactive: false });
  drawn.addLayer(rect);
  map.invalidateSize();
  if (fit) map.fitBounds(rect.getBounds(), { padding: [24, 24], maxZoom: 10, animate: !matchMedia("(prefers-reduced-motion: reduce)").matches });
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

function setBbox(b) {
  state.bbox = b.map(r3);
  [fields.w.value, fields.s.value, fields.e.value, fields.n.value] = state.bbox;
  const c = [(b[1] + b[3]) / 2, (b[0] + b[2]) / 2];
  fields.lat.value = r3(c[0]); fields.lon.value = r3(c[1]);
  $("#area-readout").textContent = `Area ${bboxStr(state.bbox)} · ≈ ${fmt(areaKm2(state.bbox), 0)} km²`;
  document.querySelectorAll(".chip").forEach((ch) => ch.setAttribute("aria-pressed", String(ch.dataset.bbox === bboxStr(state.bbox))));
}

form.addEventListener("submit", (e) => {
  e.preventDefault();
  showFormError("");
  let b = state.bbox;
  const m = mode();
  if (m === "bounds") {
    b = [fields.w, fields.s, fields.e, fields.n].map((f) => parseFloat(f.value));
    if (b.some((v) => !Number.isFinite(v))) return showFormError("Enter all four bounds: West, South, East, North.", [fields.w, fields.s, fields.e, fields.n].find((f) => !Number.isFinite(parseFloat(f.value))));
    const err = bboxError(b);
    if (err) return showFormError(err, fields.w);
  } else if (m === "point") {
    const lat = parseFloat(fields.lat.value), lon = parseFloat(fields.lon.value), km = parseFloat(fields.km.value);
    if (!Number.isFinite(lat) || lat < -90 || lat > 90) return showFormError("Latitude must be a number between −90 and 90.", fields.lat);
    if (!Number.isFinite(lon) || lon < -180 || lon > 180) return showFormError("Longitude must be a number between −180 and 180.", fields.lon);
    if (!Number.isFinite(km) || km <= 0 || km > 2000) return showFormError("Radius must be between 1 and 2000 km.", fields.km);
    b = pointToBbox(lat, lon, km);
  } else if (!b) {
    return showFormError("Draw a rectangle on the map first, or switch to Bounds / Point + radius to type an area.");
  }
  const dErr = dateError();
  if (dErr) return showFormError(dErr.msg, dErr.field);
  setBbox(b);
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
  const params = { bbox, from: state.from, to: state.to };
  const apiQ = new URLSearchParams(location.search).get("api");
  history.replaceState(null, "", "?" + (apiQ ? `api=${encodeURIComponent(apiQ)}&` : "") + `bbox=${bbox}&from=${state.from}&to=${state.to}`);
  drawSelection(state.bbox, fit);
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
  const curP = getJSON("/current", { bbox }).then(live((d) => { state.current = d; renderCurrent(d); return d; }))
    .catch((e) => { fail("card-current")(e); return null; });

  Promise.all([calP, curP]).then(() => { if (gen === state.gen && state.calendar) renderSeason(); });
  curP.then((cur) => {
    if (gen !== state.gen) return;
    const month = cur?.month ?? Number(state.to.split("-")[1]);
    getJSON("/precedent", { bbox, month }).then(live(renderPrecedent)).catch(fail("card-precedent"));
  });
  getJSON("/cells", { ...params, limit: 5000 }).then(live(renderCells)).catch((e) => { if (gen === state.gen) { cellsLayer.clearLayers(); renderMapLegend(0); console.warn("cells:", e.message); } });
  getJSON("/events", { bbox, from: monthStart(state.from), to: monthEnd(state.to), limit: 50 }).then(live(renderEvents)).catch(fail("card-events"));
}

function updateDownloads(params) {
  $("#dl-calendar").href = apiUrl("/calendar.csv", params);
  $("#dl-hotspots").href = apiUrl("/csv", { bbox: params.bbox, from: monthStart(params.from), to: monthEnd(params.to) });
}

// ───────────────────────── renderers ─────────────────────────
function renderCurrent(d) {
  const b = body("card-current");
  if (!d || d.value == null) return setEmpty("card-current", "No current-month data for this area.");
  $("#current-source").textContent = d.source === "nrt" ? "Live NRT" : d.source === "archive" ? "Archive" : "";
  $("#current-source").classList.toggle("live", d.source === "nrt");
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
  b.replaceChildren(...kids);
}

function renderPrecedent(d) {
  const mName = MONTHS_LONG[(d.month || 1) - 1];
  if (!d.n_events) return setEmpty("card-precedent", `No fire events starting in ${mName} on record in this area.`);
  const kids = [
    el("p", {}, `In this area, fires starting in ${mName} historically burned a median of `,
      el("strong", { text: `${fmt(d.median_duration_days, 0)} day${d.median_duration_days === 1 ? "" : "s"}` }),
      d.p90_duration_days != null ? ` (1 in 10 lasted ${fmt(d.p90_duration_days, 0)}+ days)` : "", "."),
  ];
  const lg = d.largest;
  if (lg) {
    const yr = (lg.start || "").slice(0, 4);
    kids.push(el("p", {}, "The largest was ", el("strong", { text: `${fmt(lg.area_km2, 0)} km²` }),
      lg.duration_days != null ? ` over ${fmt(lg.duration_days, 0)} days` : "", yr ? ` (${yr})` : "", "."));
  }
  const dl = el("dl", { class: "kv" },
    el("dt", { text: "Events on record" }), el("dd", { text: fmt(d.n_events, 0) }),
    el("dt", { text: "Median area" }), el("dd", { text: d.median_area_km2 != null ? `${fmt(d.median_area_km2)} km²` : "–" }),
    lg?.start ? [el("dt", { text: "Largest started" }), el("dd", { text: lg.start })] : null,
    lg?.peak_frp != null ? [el("dt", { text: "Largest peak FRP" }), el("dd", { text: `${fmt(lg.peak_frp, 0)} MW` })] : null,
  );
  kids.push(dl);
  if (lg?.bbox) kids.push(el("button", { type: "button", class: "btn btn-ghost btn-sm", style: "margin-top:8px", onclick: () => showEvent(lg), text: "Show largest on map" }));
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
  const { svg, years, by } = calendarSVG(d, state.calMode, state.coverage);
  const host = el("div", { id: "calendar" });
  host.innerHTML = svg;
  const tip = el("div", { id: "cal-tip", class: "tip", role: "tooltip", hidden: true });
  body("card-calendar").replaceChildren(host, tip);
  wireCalendar(host, tip, by, years);
  renderCalTable(d, years, by);
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
    tip.replaceChildren(...[
      el("div", {}, el("b", { text: `${MONTHS_LONG[m - 1]} ${y}` })),
      rec && rec.value != null
        ? [
          el("div", {}, el("b", { text: fmt(rec.value) }), rec.partial ? " fire cell-days so far" : " fire cell-days"),
          rec.partial ? el("div", { class: "t-warn", text: "Partial: month in progress, anomaly is provisional" }) : null,
          el("div", { class: "t-muted", text: rec.anomaly != null ? `${fmtRatio(rec.anomaly)} the ${MONTHS[m - 1]} median (${fmt(med)})${rec.partial ? ", provisional" : ""}` : `${MONTHS[m - 1]} median: ${fmt(med)}` }),
          rec.source === "modis_converted"
            ? el("div", { class: "t-muted", text: `MODIS converted · 10–90% band ${fmt(rec.lo)}–${fmt(rec.hi)}` })
            : el("div", { class: "t-muted", text: "VIIRS measured" }),
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
  const valueOf = (y, m) => {
    const r = by.get(`${y}-${m}`);
    if (!r || r.value == null) return "–";
    const v = state.calMode === "anomaly" ? (r.anomaly != null ? fmtRatio(r.anomaly) : "n/a") : fmt(r.value);
    return r.partial ? `${v}†` : v;
  };
  const t = el("table", { class: "data" },
    el("caption", { text: state.calMode === "anomaly" ? "Value ÷ monthly median. * = MODIS converted to VIIRS-equivalent. † = month in progress (provisional)." : "Fire cell-days per month. * = MODIS converted to VIIRS-equivalent. † = month in progress (so far)." }),
    el("thead", {}, el("tr", {}, el("th", { scope: "col", text: "Year" }), MONTHS.map((m) => el("th", { scope: "col", text: m })))),
    el("tbody", {}, years.map((y) => {
      const conv = d.months.some((r) => r.year === y && r.source === "modis_converted");
      return el("tr", {}, el("th", { scope: "row", text: `${y}${conv ? "*" : ""}` }),
        Array.from({ length: 12 }, (_, i) => el("td", { title: cellLabel(y, i + 1, by.get(`${y}-${i + 1}`), d), text: valueOf(y, i + 1) })));
    })),
  );
  $("#cal-table").replaceChildren(t);
}

document.querySelectorAll("input[name=calmode]").forEach((r) => r.addEventListener("change", (e) => {
  state.calMode = e.target.value;
  if (state.calendar) renderCalendar();
}));

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
    }).bindTooltip(`${fmt(c.cell_days, 0)} fire cell-days<br><span style="color:#8b95a1">${c.lat.toFixed(3)}, ${c.lon.toFixed(3)}</span>`, { direction: "top" })
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
    el("div", { class: "events-head", "aria-hidden": "true" }, ["Start", "Days", "Area km²", "Peak FRP", "Cell-days"].map((t) => el("span", { text: t }))),
    evs.slice(0, 15).map((ev) => el("div", { role: "listitem" }, el("button", {
      type: "button", class: "event", "aria-pressed": "false",
      "aria-label": `Fire starting ${ev.start}, ${ev.duration_days} days, ${fmt(ev.area_km2)} square kilometres, peak FRP ${fmt(ev.peak_frp, 0)} megawatts. Show on map.`,
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
    .bindTooltip(`Fire from ${ev.start} to ${ev.end || "?"} · ${fmt(ev.duration_days, 0)} days · ${fmt(ev.area_km2, 0)} km²`, { permanent: false })
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
    const d = await getJSON("/brief", { bbox: bboxStr(state.bbox) });
    if (gen !== state.gen) return;
    const kids = [el("p", { class: "brief-text", text: d.text || "(empty briefing)" })];
    kids.push(d.verified
      ? el("span", { class: "badge ok" }, "✓ Verified against computed numbers")
      : el("span", { class: "badge warn" }, "! Some numbers could not be verified"));
    if (d.model) kids.push(el("span", { class: "small muted", style: "margin-left:8px", text: d.model === "template" ? "template briefing" : `model: ${d.model}` }));
    if (d.unverified_numbers?.length) kids.push(el("div", { class: "unverified", text: `Unverified numbers: ${d.unverified_numbers.join(", ")}` }));
    body("card-brief").replaceChildren(...kids);
  } catch (e) {
    if (gen === state.gen) setError("card-brief", e);
  } finally {
    btn.disabled = false;
  }
});

// downloads -------------------------------------------------------------
const pngCal = () => svgToPng($("#calendar svg"), `firex_calendar_${bboxStr(state.bbox || [])}.png`,
  `firex burning calendar · ${bboxStr(state.bbox || [])} · ${state.calMode === "anomaly" ? "anomaly vs. median" : "fire cell-days"}`).catch((e) => alert(e.message));
const pngSeason = () => svgToPng($("#season svg"), `firex_seasonality_${bboxStr(state.bbox || [])}.png`,
  `firex seasonality · ${bboxStr(state.bbox || [])}`).catch((e) => alert(e.message));
$("#png-calendar").addEventListener("click", pngCal);
$("#dl-png-cal").addEventListener("click", pngCal);
$("#png-season").addEventListener("click", pngSeason);
$("#dl-png-season").addEventListener("click", pngSeason);

// validation -------------------------------------------------------------
const METHOD_NAMES = { frp_strata: "FRP-stratified fit", ratio: "simple monthly ratio" };
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
    if (v.region_name) kids.push(el("div", { class: "small muted", text: `Validation region: ${v.region_name}${bbox ? " (contains your area’s centre)" : ""}` }));
    if (v.annual_abs_error_pct != null) {
      kids.push(el("p", { class: "headline" }, "Harmonized MODIS predicts real VIIRS annual totals within ",
        el("span", { class: "accent", text: `${fmt(v.annual_abs_error_pct, 1)}%` }), " on average."));
    }
    const bl = v.baseline;
    if (bl?.annual_abs_error_pct != null && v.annual_abs_error_pct != null) {
      kids.push(el("p", { class: "compare" }, "Our method", mName ? ` (${mName})` : "", ": ",
        el("strong", { text: `${fmt(v.annual_abs_error_pct, 1)}%` }), " annual error vs. ",
        el("strong", { text: `${fmt(bl.annual_abs_error_pct, 1)}%` }), ` for a ${METHOD_NAMES[bl.method] || bl.method}.`));
    }
    const row = (label, ours, base, f) => [el("dt", { text: label }),
      el("dd", { text: ours == null ? "–" : f(ours) + (base != null ? `   (ratio baseline ${f(base)})` : "") })];
    kids.push(el("dl", { class: "kv" },
      v.train_years ? [el("dt", { text: "Ratios fit on" }), el("dd", { text: `${v.train_years[0]}–${v.train_years[1]}` })] : null,
      v.test_years ? [el("dt", { text: "Tested on (held out)" }), el("dd", { text: `${v.test_years[0]}–${v.test_years[1]}` })] : null,
      v.mape_monthly_pct != null ? row("Monthly error (MAPE)", v.mape_monthly_pct, bl?.mape_monthly_pct, (x) => `${fmt(x, 1)}%`) : null,
      v.r2 != null ? row("R² (monthly)", v.r2, bl?.r2, (x) => x.toFixed(2)) : null,
    ));
    if (v.annual?.length) {
      const c = el("div", { style: "margin-top:10px" });
      c.innerHTML = validationSVG(v.annual);
      kids.push(c);
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
    if (state.bbox) setBbox(state.bbox); // refresh pressed state
  } catch (e) {
    host.replaceChildren(el("span", { class: "small muted", text: e.status === 404 ? "No presets available." : "Presets unavailable." }));
  }
}

async function boot() {
  const q = new URLSearchParams(location.search);
  const urlBbox = parseBbox(q.get("bbox"));
  const urlFrom = validYM(q.get("from")) ? q.get("from") : null;
  const urlTo = validYM(q.get("to")) ? q.get("to") : null;

  let health;
  try {
    health = await getJSON("/health");
  } catch (e) {
    $("#coverage").innerHTML = `<span class="dot bad"></span>API offline`;
    showBanner(`Can't reach the firex API at <code>${API.replace(/</g, "&lt;")}</code>. Start it with <code>uvicorn backend.app.main:app --port 8000</code>, or pass <code>?api=URL</code>.`, true);
    if (urlBbox) { setBbox(urlBbox); drawSelection(urlBbox); }
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
    if (p) p.innerHTML = "<strong>Learn the conversion.</strong> On the overlap years (2012 onward) we fit a VIIRS ÷ MODIS ratio for each calendar month, with a 10–90% band.";
  }
  if (!health.data_ready) {
    showBanner(`No harmonized data yet. Build it, then reload: <code>python -m pipeline.run data/raw/&lt;region&gt;</code> (or a demo: <code>python -m pipeline.demo &amp;&amp; python -m pipeline.run data/raw/demo</code>).`, true);
  } else if (health.synthetic) {
    showBanner(`<strong>Demo data:</strong> synthetic FIRMS-format hotspots. Run the pipeline on real FIRMS archives to replace.`);
  }

  loadRegions();
  if (!urlBbox) loadValidation();
  if (urlBbox && !bboxError(urlBbox)) {
    setBbox(urlBbox);
    if (health.data_ready) analyze();
    else drawSelection(urlBbox);
  }
}

boot();
