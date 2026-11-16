// Color ramps + SVG charts. All visual styling is written as SVG attributes (not CSS classes)
// so that the serialized SVG renders identically when exported to PNG.

export const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
export const MONTHS_LONG = ["January", "February", "March", "April", "May", "June", "July", "August",
  "September", "October", "November", "December"];

const FONT = "Inter, system-ui, -apple-system, Segoe UI, Roboto, sans-serif";
const C = { bg: "#151a21", text: "#e6e9ee", muted: "#8b95a1", line: "#262e39", missing: "#3a414b", accent: "#f0a04b" };

// ───────────────────────── ramps ─────────────────────────
// viridis (matplotlib), 11 stops
const VIRIDIS = ["#440154", "#482475", "#414487", "#355f8d", "#2a788e", "#21918c", "#22a884",
  "#44bf70", "#7ad151", "#bddf26", "#fde725"];
// RdBu reversed: low = blue (cool / quieter), high = red (warm / more fire). Colorblind-safe diverging.
const RDBU_R = ["#053061", "#2166ac", "#4393c3", "#92c5de", "#d1e5f0", "#f7f7f7", "#fddbc7", "#f4a582",
  "#d6604d", "#b2182b", "#67001f"];

const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
const hex = (h) => [1, 3, 5].map((i) => parseInt(h.slice(i, i + 2), 16));
function interp(stops, t) {
  t = clamp(Number.isFinite(t) ? t : 0, 0, 1) * (stops.length - 1);
  const i = Math.min(Math.floor(t), stops.length - 2), f = t - i;
  const a = hex(stops[i]), b = hex(stops[i + 1]);
  return "#" + a.map((v, k) => Math.round(v + (b[k] - v) * f).toString(16).padStart(2, "0")).join("");
}
export const viridis = (t) => interp(VIRIDIS, t);
export const diverging = (t) => interp(RDBU_R, t);

/** Counts use a square-root scale (fire data is very skewed). */
export const countT = (v, max) => (max > 0 ? Math.sqrt(clamp(v, 0, max) / max) : 0);
/** Anomaly: log2(value / median), clipped to ⅛×…8×, centered on 1×. */
const ANOM_LIM = 3;
export const anomalyT = (a) => clamp((Math.log2(Math.max(a, 2 ** -8)) + ANOM_LIM) / (2 * ANOM_LIM), 0, 1);
const ANOM_TICKS = [1 / 8, 1 / 4, 1 / 2, 1, 2, 4, 8];
const anomLabel = (a) => (a >= 1 ? `${a}×` : `1/${Math.round(1 / a)}×`);

export function fmt(v, digits) {
  if (v == null || !Number.isFinite(v)) return "–";
  const d = digits ?? (Math.abs(v) >= 100 ? 0 : Math.abs(v) >= 10 ? 1 : Math.abs(v) >= 1 ? 1 : 2);
  return v.toLocaleString("en-US", { maximumFractionDigits: d, minimumFractionDigits: 0 });
}
export const fmtRatio = (r) => (r >= 10 ? Math.round(r).toString() : r.toFixed(1)) + "×";

/** Round ticks from 0. `cover` = extend one step past max so an axis contains all data. */
export function niceTicks(max, n = 4, cover = false) {
  if (!(max > 0)) return cover ? [0, 1] : [0];
  const raw = max / n, mag = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 2.5, 5, 10].map((s) => s * mag).find((s) => s >= raw) || raw;
  const steps = cover ? Math.ceil(max / step - 1e-9) : Math.floor(max / step + 1e-9);
  return Array.from({ length: steps + 1 }, (_, i) => +(i * step).toFixed(10));
}

const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const txt = (x, y, s, o = {}) =>
  `<text x="${x}" y="${y}" fill="${o.fill || C.muted}" font-size="${o.size || 11}" font-family="${FONT}"` +
  `${o.anchor ? ` text-anchor="${o.anchor}"` : ""}${o.weight ? ` font-weight="${o.weight}"` : ""}` +
  `${o.baseline ? ` dominant-baseline="${o.baseline}"` : ""}>${esc(s)}</text>`;

function gradientDef(id, fn, n = 24) {
  let s = `<linearGradient id="${id}" x1="0" x2="1" y1="0" y2="0">`;
  for (let i = 0; i <= n; i++) s += `<stop offset="${i / n}" stop-color="${fn(i / n)}"/>`;
  return s + "</linearGradient>";
}

// ───────────────────────── burning calendar ─────────────────────────
/**
 * Build the calendar SVG string. Returns { svg, index } where index maps "y-m" → month record.
 * Cells carry data-y / data-m for event delegation; tooltips are handled by the caller.
 */
export const OVERLAP_START = 2012;
/** Value shown for a month in a calendar mode ("counts" | "anomaly" | "raw"). */
export const calValue = (d, mode) => (mode === "raw" ? d?.raw : d?.value);

export function calendarSVG(data, mode, coverage, health) {
  const months = data.months || [];
  const by = new Map(months.map((m) => [`${m.year}-${m.month}`, m]));
  const years = [...new Set(months.map((m) => m.year))].sort((a, b) => a - b);
  if (!years.length) return { svg: "", years };
  // Counts and raw share one color scale so toggling between them shows the sensor jump honestly.
  const max = Math.max(0, ...months.map((m) => Math.max(m.value ?? 0, m.raw ?? 0)));
  const raw = mode === "raw";
  const peak = data.summary?.peak_month;
  const covTo = coverage?.to ? coverage.to.split("-").map(Number) : null;
  const covFrom = coverage?.from ? coverage.from.split("-").map(Number) : null;
  // Months with a value but no anomaly (zero median, or a month in progress): nothing to compare against.
  const noCmp = mode === "anomaly" && months.some((m) => m.value != null && m.anomaly == null);

  const L = 58, T = 26, cw = 36, ch = 17, gap = 2;
  const W = L + 12 * (cw + gap) + 10 + (months.some((m) => m.partial) || noCmp || raw ? 60 : 0); // room for the right-hand labels
  const gridH = years.length * (ch + gap);
  const legendY = T + gridH + 16;
  const n20 = months.some((m) => m.source === "noaa20_converted");
  const H = legendY + 64 + (n20 ? 13 : 0);
  const conv = years.filter((y) => months.some((m) => m.year === y && m.source === "modis_converted"));

  const modeDesc = mode === "anomaly" ? "anomaly versus monthly median" : raw ? `raw, un-harmonized fire cell-days (MODIS before ${OVERLAP_START}, VIIRS after)` : "fire cell-days";
  let s = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${W} ${H}" width="${W}" height="${H}" tabindex="-1" role="group" aria-label="Burning calendar, ${years[0]} to ${years.at(-1)}, colored by ${modeDesc}. Use arrow keys to move between months.">`;
  s += `<defs>${gradientDef("g-count", (t) => viridis(t))}${gradientDef("g-anom", diverging)}`;
  s += `<pattern id="hatch" width="4" height="4" patternUnits="userSpaceOnUse" patternTransform="rotate(45)"><rect width="4" height="4" fill="#2a313b"/><line x1="0" y1="0" x2="0" y2="4" stroke="${C.muted}" stroke-width="1.6"/></pattern>`;
  s += `<pattern id="nocmp" width="5" height="5" patternUnits="userSpaceOnUse" patternTransform="rotate(-45)"><rect width="5" height="5" fill="${C.missing}"/><line x1="0" y1="0" x2="0" y2="5" stroke="${C.muted}" stroke-width="1"/></pattern></defs>`;
  s += `<rect width="${W}" height="${H}" fill="${C.bg}"/>`;

  // month header
  MONTHS.forEach((m, i) => (s += txt(L + i * (cw + gap) + cw / 2, T - 9, m[0] + m.slice(1, 3), { anchor: "middle", size: 10.5 })));

  // MODIS-converted bracket on the left edge (harmonized views only)
  if (conv.length && !raw) {
    const r0 = years.indexOf(conv[0]), r1 = years.indexOf(conv.at(-1));
    s += `<rect x="2" y="${T + r0 * (ch + gap)}" width="5" height="${(r1 - r0 + 1) * (ch + gap) - gap}" fill="url(#hatch)" rx="1"/>`;
  }

  let peakRect = "";
  years.forEach((y, r) => {
    const yy = T + r * (ch + gap);
    const isConv = conv.includes(y);
    s += txt(L - 8, yy + ch / 2 + 0.5, y, { anchor: "end", size: 10.5, fill: isConv ? C.muted : C.text, baseline: "middle" });
    for (let mo = 1; mo <= 12; mo++) {
      const x = L + (mo - 1) * (cw + gap);
      const d = by.get(`${y}-${mo}`);
      const future = (covTo && (y > covTo[0] || (y === covTo[0] && mo > covTo[1]))) ||
        (covFrom && (y < covFrom[0] || (y === covFrom[0] && mo < covFrom[1]))); // outside the record
      let fill;
      const v = calValue(d, mode);
      if (!d || v == null) fill = future ? "none" : C.missing;
      else if (raw) fill = viridis(countT(v, max));
      else if (mode === "anomaly") fill = d.anomaly == null ? "url(#nocmp)" : diverging(anomalyT(d.anomaly));
      else fill = viridis(countT(d.value, max));
      if (future && (!d || v == null)) continue;
      s += `<rect class="cell" data-y="${y}" data-m="${mo}" x="${x}" y="${yy}" width="${cw}" height="${ch}" rx="2" fill="${fill}" tabindex="-1" role="img" aria-label="${esc(cellLabel(y, mo, d, data, mode, health))}"/>`;
      if (d?.partial) {
        s += `<rect x="${x + 1}" y="${yy + 1}" width="${cw - 2}" height="${ch - 2}" rx="1.5" fill="none" stroke="#ffffff" stroke-width="1.5" stroke-dasharray="3 2" pointer-events="none"/>`;
      }
      if (peak && peak.year === y && peak.month === mo) {
        peakRect = `<rect x="${x - 1.5}" y="${yy - 1.5}" width="${cw + 3}" height="${ch + 3}" rx="3" fill="none" stroke="#ffffff" stroke-width="2" pointer-events="none"/>` +
          `<rect x="${x + 0.5}" y="${yy + 0.5}" width="${cw - 1}" height="${ch - 1}" rx="1.5" fill="none" stroke="#0e1116" stroke-width="1" pointer-events="none"/>`;
      }
    }
  });
  if (!raw) s += peakRect;
  // Sensor handoff line between the last MODIS-only year and the first VIIRS year.
  const hi = years.indexOf(OVERLAP_START);
  if (raw && hi > 0) {
    const ly = T + hi * (ch + gap) - gap / 2;
    s += `<line x1="${L - 4}" x2="${L + 12 * (cw + gap) - gap + 4}" y1="${ly}" y2="${ly}" stroke="${C.accent}" stroke-width="2" stroke-dasharray="5 3"/>`;
    s += txt(L + 12 * (cw + gap) + 2, ly + 3.5, "VIIRS", { size: 9.5, fill: C.accent });
    s += txt(L + 12 * (cw + gap) + 2, ly - 5, "MODIS", { size: 9.5, fill: C.muted });
  }

  // legend
  const lx = L, lw = 12 * (cw + gap) - gap - 150;
  if (mode === "anomaly") {
    s += txt(lx, legendY, "Anomaly: value ÷ median for that month (log scale)", { size: 10.5 });
    s += `<rect x="${lx}" y="${legendY + 6}" width="${lw}" height="10" rx="2" fill="url(#g-anom)"/>`;
    ANOM_TICKS.forEach((a) => {
      const x = lx + anomalyT(a) * lw;
      s += `<line x1="${x}" x2="${x}" y1="${legendY + 16}" y2="${legendY + 20}" stroke="${C.muted}"/>`;
      s += txt(x, legendY + 31, anomLabel(a), { anchor: "middle", size: 10, fill: a === 1 ? C.text : C.muted });
    });
  } else {
    s += txt(lx, legendY, raw ? "Raw fire cell-days, as each sensor saw them (not harmonized)" : "Fire cell-days per month (square-root scale)", { size: 10.5 });
    s += `<rect x="${lx}" y="${legendY + 6}" width="${lw}" height="10" rx="2" fill="url(#g-count)"/>`;
    const ticks = niceTicks(max, 4);
    ticks.forEach((v, i) => {
      const x = lx + countT(v, max) * lw;
      if (i > 0 && x - (lx + countT(ticks[i - 1], max) * lw) < 22) return;
      s += `<line x1="${x}" x2="${x}" y1="${legendY + 16}" y2="${legendY + 20}" stroke="${C.muted}"/>`;
      s += txt(x, legendY + 31, fmt(v, 0), { anchor: "middle", size: 10 });
    });
  }
  // key: missing, peak, converted
  const kx = lx + lw + 18;
  s += `<rect x="${kx}" y="${legendY - 3}" width="12" height="9" rx="1.5" fill="${C.missing}"/>` + txt(kx + 17, legendY + 5, "No data", { size: 10 });
  if (!raw) s += `<rect x="${kx}" y="${legendY + 12}" width="12" height="9" rx="1.5" fill="none" stroke="#fff" stroke-width="1.6"/>` + txt(kx + 17, legendY + 20, "Peak month", { size: 10 });
  const hasPartial = months.some((m) => m.partial);
  if (hasPartial) {
    s += `<rect x="${kx + 72 + 18}" y="${legendY - 3}" width="12" height="9" rx="1.5" fill="none" stroke="#fff" stroke-width="1.3" stroke-dasharray="3 2"/>` + txt(kx + 72 + 35, legendY + 5, "In progress", { size: 10 });
  }
  if (noCmp) {
    s += `<rect x="${kx + 72 + 18}" y="${legendY + 12}" width="12" height="9" rx="1.5" fill="url(#nocmp)"/>` + txt(kx + 72 + 35, legendY + 20, "No comparison", { size: 10 });
  }
  if (raw && hi > 0) {
    s += `<line x1="${kx}" x2="${kx + 12}" y1="${legendY + 16}" y2="${legendY + 16}" stroke="${C.accent}" stroke-width="2" stroke-dasharray="4 2"/>` + txt(kx + 17, legendY + 20, "Sensor switch", { size: 10 });
    const ho = data.handoff;
    s += txt(lx, legendY + 50, `Raw MODIS before ${OVERLAP_START}, raw VIIRS after. ` + (ho?.viirs_per_modis != null && ho.overlap_years
      ? `Flown together (${ho.overlap_years[0]}–${ho.overlap_years[1]}), VIIRS logged ${ho.viirs_per_modis.toFixed(2)}× MODIS here.`
      : "Part of the jump at the line is the sensor change."), { size: 9.5 });
  } else if (conv.length) {
    s += `<rect x="${kx + 3}" y="${legendY + 27}" width="5" height="10" fill="url(#hatch)"/>` + txt(kx + 17, legendY + 35, `MODIS → VIIRS-equiv.`, { size: 10 });
    s += txt(lx, legendY + 50, `Hatched years (${conv[0]}–${conv.at(-1)}): MODIS converted to VIIRS-equivalent; later years VIIRS measured.`, { size: 9.5 });
  }
  if (n20) {
    const end = health?.snpp_end ? ` (after ${health.snpp_end})` : "";
    // NOAA-20 months while Suomi NPP was still flying are outage fill-ins, not the post-S-NPP record.
    const late = months.some((m) => m.source === "noaa20_converted" && !snppEra(m.year, m.month, health));
    s += txt(lx, legendY + 63, !late ? `Months with a Suomi NPP outage use ${raw ? "raw NOAA-20 VIIRS" : "NOAA-20, converted to Suomi NPP units"}.`
      : raw ? `Suomi NPP stopped on 2 Nov 2026: later months${end} are raw NOAA-20 VIIRS.`
      : `Suomi NPP stopped on 2 Nov 2026: later months${end} are NOAA-20 converted to Suomi NPP units.`, { size: 9.5 });
  }
  s += "</svg>";
  return { svg: s, years, by };
}

export const RAW_NAMES = { modis: "MODIS", viirs: "VIIRS (Suomi NPP)", noaa20: "VIIRS (NOAA-20)" };
const band = (d) => (d.lo != null && d.hi != null && d.lo !== d.hi ? ` · 10–90% band ${fmt(d.lo)}–${fmt(d.hi)}` : "");
/** True for months before Aqua's first full month (`/health` → aqua_first_month): MODIS is Terra only. */
const terraOnly = (y, mo, health) =>
  !!health?.aqua_first_month && `${y}-${String(mo).padStart(2, "0")}` < health.aqua_first_month;

/** True for a month Suomi NPP was still flying (`/health` → snpp_end): NOAA-20 there means an S-NPP outage. */
export const snppEra = (y, mo, health) =>
  !!health?.snpp_end && `${y}-${String(mo).padStart(2, "0")}` <= health.snpp_end;
const SENSOR_SHORT = { modis: "MODIS", viirs: "S-NPP", noaa20: "NOAA-20", modis_converted: "MODIS", noaa20_converted: "NOAA-20" };
/** " · S-NPP observed 39% of days (missing days filled)" when the month's sensor had an outage; else "". */
function observedNote(d, src, filled = true) {
  if (d.observed == null || d.observed >= 1) return "";
  return ` · ${SENSOR_SHORT[src] || "Sensor"} observed ${Math.round(d.observed * 100)}% of days` +
    (filled ? " (missing days filled)" : " (missing days not filled here)");
}

/** One line saying where a harmonized month's value comes from. */
export function sourceNote(d, y, mo, health) {
  const obs = observedNote(d, d.source);
  if (d.source === "modis_converted") {
    return `MODIS converted to VIIRS-equivalent${band(d)}` +
      (terraOnly(y, mo, health) ? ` · Terra only (Aqua data starts ${health.aqua_first_month}), scaled up to Terra+Aqua first` : "") + obs;
  }
  if (d.source === "noaa20_converted") {
    return (snppEra(y, mo, health) ? "S-NPP outage: NOAA-20 used, converted to Suomi NPP units"
      : "NOAA-20 VIIRS converted to Suomi NPP units (Suomi NPP stopped on 2 Nov 2026)") + band(d) + obs;
  }
  return "VIIRS (Suomi NPP) measured" + obs;
}
/** Same, for the raw (un-harmonized) view. */
export function rawNote(d, y, mo, health) {
  const obs = observedNote(d, d.raw_source, false);
  if (d.raw_source === "modis") {
    return "Not converted: MODIS sees fewer, larger pixels" +
      (terraOnly(y, mo, health) ? ` · Terra only (Aqua data starts ${health.aqua_first_month})` : "") + obs;
  }
  if (d.raw_source === "noaa20") {
    return (snppEra(y, mo, health) ? "S-NPP outage: NOAA-20 VIIRS used, as measured" : "NOAA-20 VIIRS as measured (Suomi NPP stopped on 2 Nov 2026)") + obs;
  }
  return "VIIRS (Suomi NPP) measured" + obs;
}

export function cellLabel(y, mo, d, data, mode = "counts", health = null) {
  const name = `${MONTHS_LONG[mo - 1]} ${y}`;
  if (mode === "raw") {
    if (!d || d.raw == null) return `${name}: no data`;
    return `${name}: ${fmt(d.raw)} raw ${RAW_NAMES[d.raw_source] || d.raw_source} fire cell-days${d.partial ? " so far" : ""}; harmonized ${fmt(d.value)}. ${rawNote(d, y, mo, health)}`;
  }
  if (!d || d.value == null) return `${name}: no data`;
  const med = data.median_by_month?.[mo] ?? data.median_by_month?.[String(mo)];
  let s = `${name}: ${fmt(d.value)} fire cell-days`;
  if (d.partial) s += " so far (month in progress)";
  if (d.anomaly != null) s += `, ${fmtRatio(d.anomaly)} the ${MONTHS[mo - 1]} median of ${fmt(med)}${d.partial ? " (provisional)" : ""}`;
  else if (med === 0) s += `, no comparison: ${MONTHS[mo - 1]} median is 0`;
  else s += ", no comparison with the median";
  return `${s}. ${sourceNote(d, y, mo, health)}`;
}

// ───────────────────────── seasonality ─────────────────────────
export function seasonalitySVG(seasonality, currentYear, currentPts) {
  const W = 600, H = 250, L = 46, R = 14, T = 30, B = 28;
  const pw = W - L - R, ph = H - T - B;
  const sea = [...(seasonality || [])].sort((a, b) => a.month - b.month);
  const ymax = Math.max(1, ...sea.map((d) => d.p90 ?? 0), ...currentPts.map((d) => d.value ?? 0));
  const ticks = niceTicks(ymax * 1.05, 4, true);
  const top = ticks.at(-1);
  const x = (m) => L + ((m - 1) / 11) * pw;
  const y = (v) => T + ph - (v / top) * ph;

  let s = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${W} ${H}" width="${W}" height="${H}" role="img" aria-label="${esc(seasonAria(sea, currentYear, currentPts))}">`;
  s += `<rect width="${W}" height="${H}" fill="${C.bg}"/>`;
  ticks.forEach((v) => {
    s += `<line x1="${L}" x2="${W - R}" y1="${y(v)}" y2="${y(v)}" stroke="${C.line}"/>`;
    s += txt(L - 6, y(v) + 3.5, fmt(v, 0), { anchor: "end", size: 10 });
  });
  MONTHS.forEach((m, i) => (s += txt(x(i + 1), H - 9, m, { anchor: "middle", size: 10 })));
  if (sea.length) {
    const band = sea.map((d) => `${x(d.month)},${y(d.p90)}`).join(" ") + " " +
      [...sea].reverse().map((d) => `${x(d.month)},${y(d.p10)}`).join(" ");
    s += `<polygon points="${band}" fill="#5b7a99" fill-opacity="0.32"/>`;
    s += `<polyline points="${sea.map((d) => `${x(d.month)},${y(d.median)}`).join(" ")}" fill="none" stroke="#c9d1db" stroke-width="2" stroke-dasharray="5 3"/>`;
  }
  const pts = currentPts.filter((d) => d.value != null).sort((a, b) => a.month - b.month);
  if (pts.length) {
    s += `<polyline points="${pts.map((d) => `${x(d.month)},${y(d.value)}`).join(" ")}" fill="none" stroke="${C.accent}" stroke-width="2.5"/>`;
    pts.forEach((d) => (s += `<circle cx="${x(d.month)}" cy="${y(d.value)}" r="3.5" fill="${C.accent}" stroke="${C.bg}" stroke-width="1.5"><title>${MONTHS[d.month - 1]} ${currentYear}: ${fmt(d.value)}</title></circle>`));
  }
  // legend
  let lx = L;
  s += `<rect x="${lx}" y="9" width="16" height="9" fill="#5b7a99" fill-opacity="0.45"/>` + txt(lx + 21, 17, "10–90% of years", { size: 10.5 });
  lx += 120;
  s += `<line x1="${lx}" x2="${lx + 16}" y1="13.5" y2="13.5" stroke="#c9d1db" stroke-width="2" stroke-dasharray="5 3"/>` + txt(lx + 21, 17, "Median", { size: 10.5 });
  lx += 76;
  if (pts.length) s += `<line x1="${lx}" x2="${lx + 16}" y1="13.5" y2="13.5" stroke="${C.accent}" stroke-width="2.5"/>` + txt(lx + 21, 17, String(currentYear), { size: 10.5, fill: C.text });
  s += txt(W - R, 17, "fire cell-days", { anchor: "end", size: 10 });
  return s + "</svg>";
}

function seasonAria(sea, yr, pts) {
  if (!sea.length) return "Seasonality chart: no data";
  const pk = sea.reduce((a, b) => (b.median > a.median ? b : a));
  return `Seasonality: median fire activity peaks in ${MONTHS_LONG[pk.month - 1]} at ${fmt(pk.median)} fire cell-days. ` +
    (pts.length ? `${yr} so far: ` + pts.map((d) => `${MONTHS[d.month - 1]} ${fmt(d.value)}`).join(", ") : "");
}

// ───────────────────────── validation ─────────────────────────
export function validationSVG(annual) {
  const W = 600, H = 220, L = 52, R = 14, T = 30, B = 26;
  const pw = W - L - R, ph = H - T - B;
  const rows = [...annual].sort((a, b) => a.year - b.year);
  const ymax = Math.max(1, ...rows.flatMap((r) => [r.predicted, r.observed]));
  const ticks = niceTicks(ymax * 1.05, 4, true), top = ticks.at(-1);
  const n = rows.length, gw = pw / Math.max(n, 1), bw = Math.min(22, gw * 0.32);
  const y = (v) => T + ph - (v / top) * ph;
  let s = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${W} ${H}" width="${W}" height="${H}" role="img" aria-label="Annual totals, observed VIIRS versus MODIS converted with a fit that never saw that year (leave-one-year-out). ${esc(rows.map((r) => `${r.year}: observed ${fmt(r.observed)}, predicted ${fmt(r.predicted)}`).join("; "))}">`;
  s += `<rect width="${W}" height="${H}" fill="${C.bg}"/>`;
  ticks.forEach((v) => {
    s += `<line x1="${L}" x2="${W - R}" y1="${y(v)}" y2="${y(v)}" stroke="${C.line}"/>`;
    s += txt(L - 6, y(v) + 3.5, fmt(v, 0), { anchor: "end", size: 10 });
  });
  const drift = rows.filter((r) => r.modis_drift?.length);
  if (drift.length) {
    const i0 = rows.indexOf(drift[0]);
    s += `<defs><pattern id="vhatch" width="6" height="6" patternUnits="userSpaceOnUse" patternTransform="rotate(45)"><line x1="0" y1="0" x2="0" y2="6" stroke="#3a414b" stroke-width="2"/></pattern></defs>`;
    s += `<rect x="${L + gw * i0}" y="${T}" width="${gw * drift.length}" height="${ph}" fill="url(#vhatch)" opacity="0.6"/>`;
  }
  rows.forEach((r, i) => {
    const cx = L + gw * (i + 0.5);
    s += `<rect x="${cx - bw - 1}" y="${y(r.observed)}" width="${bw}" height="${T + ph - y(r.observed)}" fill="#c9d1db"><title>${r.year} observed VIIRS: ${fmt(r.observed)}</title></rect>`;
    s += `<rect x="${cx + 1}" y="${y(r.predicted)}" width="${bw}" height="${T + ph - y(r.predicted)}" fill="#56b4e9"><title>${r.year} predicted from MODIS: ${fmt(r.predicted)}</title></rect>`;
    s += txt(cx, H - 9, r.year + (r.modis_drift?.length ? "*" : ""), { anchor: "middle", size: 10 });
    if (r.error_pct != null) s += txt(cx, Math.min(y(r.observed), y(r.predicted)) - 4, `${r.error_pct > 0 ? "+" : ""}${r.error_pct.toFixed(1)}%`, { anchor: "middle", size: 9.5 });
  });
  s += `<rect x="${L}" y="9" width="12" height="9" fill="#c9d1db"/>` + txt(L + 17, 17, "Observed VIIRS", { size: 10.5 });
  s += `<rect x="${L + 120}" y="9" width="12" height="9" fill="#56b4e9"/>` + txt(L + 137, 17, "Predicted from MODIS (year held out)", { size: 10.5 });
  s += txt(W - R, 17, "fire cell-days / year", { anchor: "end", size: 10 });
  return s + "</svg>";
}

// ───────────────────────── PNG export ─────────────────────────
export async function svgToPng(svgEl, filename, title) {
  if (!svgEl) return;
  const clone = svgEl.cloneNode(true);
  const vb = clone.viewBox.baseVal;
  const pad = title ? 26 : 0;
  const w = vb.width, h = vb.height + pad;
  clone.setAttribute("width", vb.width);
  clone.setAttribute("height", vb.height);
  clone.setAttribute("y", pad);
  clone.querySelectorAll("[tabindex]").forEach((n) => n.removeAttribute("tabindex"));
  const wrap = `<svg xmlns="http://www.w3.org/2000/svg" width="${w}" height="${h}" viewBox="0 0 ${w} ${h}">` +
    `<rect width="${w}" height="${h}" fill="${C.bg}"/>` +
    (title ? txt(10, 17, title, { size: 12, fill: C.text, weight: 600 }) : "") +
    new XMLSerializer().serializeToString(clone) + "</svg>";
  const url = URL.createObjectURL(new Blob([wrap], { type: "image/svg+xml;charset=utf-8" }));
  try {
    const img = new Image();
    await new Promise((res, rej) => { img.onload = res; img.onerror = () => rej(new Error("SVG render failed")); img.src = url; });
    const scale = 2, canvas = document.createElement("canvas");
    canvas.width = w * scale; canvas.height = h * scale;
    const ctx = canvas.getContext("2d");
    ctx.scale(scale, scale);
    ctx.drawImage(img, 0, 0, w, h);
    const blob = await new Promise((res) => canvas.toBlob(res, "image/png"));
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = filename;
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 2000);
  } finally {
    URL.revokeObjectURL(url);
  }
}
