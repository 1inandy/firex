"""Data access + analytics for the API. Pure-ish functions over the Parquet files built by pipeline.run."""
from __future__ import annotations

import calendar as cal
import datetime as dt
import collections
import time
import json
import os

import duckdb
import numpy as np
import pandas as pd

from pipeline import config
from pipeline.grid import cell_days_by_family
from pipeline.harmonize import (harmonize, harmonize_cells, ratios_for_region, raw_series, record_source,
                                region_for_bbox)

from .area import Area, Bbox


class NoData(Exception):
    pass


def sql(query: str, args: list) -> pd.DataFrame:
    """Run a query on a fresh in-memory connection. FastAPI serves sync routes from a thread pool, and
    duckdb's module-level default connection can't run two queries at once."""
    with duckdb.connect() as con:
        return con.execute(query, args).df()


def _path(name: str):
    return {"monthly": config.GRID / "monthly.parquet", "hotspots": config.GRID / "hotspots.parquet",
            "cells_daily": config.GRID / "cells_daily.parquet", "events": config.GRID / "events.parquet",
            "static": config.GRID / "static_sources.parquet", "observed": config.GRID / "observed.parquet",
            "ratios": config.HARMONIZED / "ratios.parquet",
            "validation": config.HARMONIZED / "validation.json"}[name]


def _require(name: str):
    p = _path(name)
    if not p.exists():
        raise NoData(f"{p.name} missing: run `python -m pipeline.demo && python -m pipeline.run data/raw/demo` "
                     "or the pipeline on real FIRMS archives")
    return str(p)


def meta() -> dict:
    if config.META.exists():
        return json.loads(config.META.read_text())
    return {}


def _bbox_sql(lat: str = "lat", lon: str = "lon") -> str:
    return f"{lon} BETWEEN ? AND ? AND {lat} BETWEEN ? AND ?"


def _bbox_args(b: Bbox) -> list[float]:
    w, s, e, n = b
    return [w, e, s, n]


def load_monthly(a: Area) -> pd.DataFrame:
    df = sql(f"SELECT * FROM read_parquet(?) WHERE {_bbox_sql()}", [_require("monthly"), *_bbox_args(a.bbox)])
    return a.filter(df)


def load_ratios(a: Area) -> pd.DataFrame | None:
    p = _path("ratios")
    if not p.exists():
        return None
    return ratios_for_region(sql("SELECT * FROM read_parquet(?)", [str(p)]), region_for_bbox(a.bbox))


def last_full_month() -> tuple[int, int] | None:
    m = meta().get("last_full_month")
    if not m:
        return None
    y, mo = m.split("-")
    return int(y), int(mo)


def observed() -> pd.DataFrame | None:
    """Share of each month's days each sensor group observed (outages, coverage); None if not built."""
    p = _path("observed")
    return sql("SELECT * FROM read_parquet(?)", [str(p)]) if p.exists() else None


def latest_date() -> dt.date | None:
    d = meta().get("max_date")
    return dt.date.fromisoformat(d) if d else None


# ---------- calendar ----------

def calendar_frame(a: Area, start: int, end: int) -> tuple[pd.DataFrame, pd.Series]:
    """Harmonized months in [start, end] (as year*12+month) with anomaly; also median per calendar month.

    The baseline (median per calendar month) uses every complete month of the whole record, whatever range
    is displayed, so a month's anomaly doesn't change with the view. A partial (in-progress) month gets no
    anomaly here; /current compares it with the same days of past years. `raw`/`raw_source` carry the
    un-harmonized stitched record (MODIS, then S-NPP, then NOAA-20) for comparison.
    """
    monthly = load_monthly(a)
    if monthly.empty:
        return pd.DataFrame(columns=["year", "month", "fire_cell_days", "lo", "hi", "source", "anomaly",
                                     "raw", "raw_source", "partial"]), pd.Series(dtype=float)
    obs = observed()
    h = _small_area_band(harmonize(monthly, load_ratios(a), obs=obs), a)
    h = h.merge(raw_series(monthly, obs), on=["year", "month"], how="left")
    h = _fill_missing_months(h)
    lf = last_full_month()
    key = h.year * 12 + h.month
    h = h.assign(partial=(key > lf[0] * 12 + lf[1]) if lf else False)
    med = h[~h.partial].groupby("month")["fire_cell_days"].median()
    h = h.assign(anomaly=(h.fire_cell_days / h.month.map(med).replace(0, np.nan)).where(~h.partial))
    h = h[(key >= start) & (key <= end)]
    return h.reset_index(drop=True), med


SMALL_AREA_KM2 = {0.25: 3_000, 1.0: 100_000}  # areas below this use the band measured on tiles of that size


def _small_area_band(h: pd.DataFrame, a: Area) -> pd.DataFrame:
    """Widen converted months' 10–90% band for small areas, using the out-of-sample band measured on
    1° / 0.25° tiles (a regional band is far too narrow for a single valley or county)."""
    from .area import area_km2
    km2 = area_km2(a)
    try:
        sa = {x["tile_deg"]: (x["band"], x.get("band_add_per_tile", 0.0)) for x in validation(a).get("small_area", [])}
    except NoData:
        return h
    deg = next((d for d in sorted(SMALL_AREA_KM2) if km2 < SMALL_AREA_KM2[d] and d in sa), None)
    if deg is None or h.empty:
        return h
    (lo, hi), add = sa[deg]
    lat = (a.bbox[1] + a.bbox[3]) / 2
    tiles = km2 / ((deg * 111.32) ** 2 * max(np.cos(np.radians(lat)), 0.1))  # area in tile units
    conv = h.source.eq("modis_converted")
    return h.assign(lo=np.where(conv, np.minimum(h.lo, h.fire_cell_days * lo), h.lo),
                    hi=np.where(conv, np.maximum(h.hi, h.fire_cell_days * hi) + add * min(tiles, 1.0), h.hi))


def _fill_missing_months(h: pd.DataFrame) -> pd.DataFrame:
    """Months with no detections at all are real zeros, not missing data (within data coverage)."""
    md = meta()
    if not md.get("min_date") or h.empty:
        return h
    first = pd.Period(md["min_date"], "M")
    last = pd.Period(md["max_date"], "M")
    idx = pd.period_range(first, last, freq="M")
    full = pd.DataFrame({"year": idx.year, "month": idx.month})
    out = full.merge(h, on=["year", "month"], how="left")
    src, frac = record_source(out.year, out.month, observed())
    raw_src = pd.Series(src).map({"modis_converted": "modis", "viirs": "viirs", "noaa20_converted": "noaa20"})
    out = out.assign(source=out.source.fillna(pd.Series(src)), observed=out.observed.fillna(pd.Series(frac)),
                     fire_cell_days=out.fire_cell_days.fillna(0.0), lo=out.lo.fillna(0.0), hi=out.hi.fillna(0.0))
    if "raw" in out:
        out = out.assign(raw=out.raw.fillna(0.0), raw_source=out.raw_source.fillna(raw_src))
    return out


def seasonality(h: pd.DataFrame) -> list[dict]:
    full = h[~h.partial]
    g = full.groupby("month")["fire_cell_days"]
    return [{"month": int(m), "median": round(float(g.median()[m]), 2),
             "p10": round(float(g.quantile(0.1)[m]), 2), "p90": round(float(g.quantile(0.9)[m]), 2)}
            for m in sorted(g.groups)]


def summary(h: pd.DataFrame) -> dict:
    if h.empty:
        return {"total": 0.0, "peak_month": None, "peak_year": None, "typical_peak_month": None,
                "trend_per_decade_pct": None, "top_anomalies": []}
    full = h[~h.partial]
    pk = h.loc[h.fire_cell_days.idxmax()]
    yearly = full.groupby("year").agg(v=("fire_cell_days", "sum"), n=("month", "size"))
    complete = yearly[yearly.n == 12]
    py = complete.v.idxmax() if len(complete) else None
    seas = full.groupby("month")["fire_cell_days"].median()
    trend, t_stat = None, None
    if len(complete) >= 8 and complete.v.mean() > 0:
        x, y = complete.index.to_numpy(float), complete.v.to_numpy(float)
        slope, icept = np.polyfit(x, y, 1)
        resid = y - (slope * x + icept)
        se = np.sqrt((resid ** 2).sum() / (len(x) - 2) / ((x - x.mean()) ** 2).sum())
        trend = round(float(100 * slope * 10 / complete.v.mean()), 1)
        t_stat = round(float(slope / se), 2) if se > 0 else None
    an = h[h.anomaly.notna() & (h.fire_cell_days >= 5)].nlargest(5, "anomaly")
    return {
        "total": round(float(h.fire_cell_days.sum()), 1),
        "peak_month": {"year": int(pk.year), "month": int(pk.month), "value": round(float(pk.fire_cell_days), 1)},
        "peak_year": None if py is None else {"year": int(py), "value": round(float(complete.v[py]), 1)},
        "typical_peak_month": int(seas.idxmax()) if len(seas) and seas.max() > 0 else None,
        "trend_per_decade_pct": trend,
        "trend_t": t_stat,  # slope ÷ its standard error; |t| ≥ 2 ≈ significant at 5% (fire years vary a lot)
        "trend_significant": bool(t_stat is not None and abs(t_stat) >= 2),
        "top_anomalies": [{"year": int(r.year), "month": int(r.month), "value": round(float(r.fire_cell_days), 1),
                           "anomaly": round(float(r.anomaly), 2)} for r in an.itertuples()],
    }


def months_payload(h: pd.DataFrame) -> list[dict]:
    return [{"year": int(r.year), "month": int(r.month), "value": round(float(r.fire_cell_days), 2),
             "lo": round(float(r.lo), 2), "hi": round(float(r.hi), 2), "source": r.source,
             "anomaly": None if pd.isna(r.anomaly) else round(float(r.anomaly), 3),
             "partial": bool(r.partial),
             "observed": round(float(r.observed), 3) if "observed" in h and pd.notna(r.observed) else 1.0,
             "raw": round(float(r.raw), 2) if "raw" in h and pd.notna(r.raw) else None,
             "raw_source": getattr(r, "raw_source", None)} for r in h.itertuples()]


def handoff(h: pd.DataFrame, a: Area, years: int = 3) -> dict | None:
    """Why the raw record jumps at 2012, measured where both sensors watched the same fires.

    `viirs_per_modis`: VIIRS ÷ MODIS fire cell-days in this area over the same months of the conversion's
    fit years. That simultaneous ratio is the sensor difference. `raw_jump` / `harmonized_jump` compare
    `years` complete years either side of the handoff for context: they also include real year-to-year change.
    """
    if h.empty or "raw" not in h:
        return None
    y0, y1 = config.CONVERSION_FIT_YEARS
    m = load_monthly(a)
    ov = m[m.year.between(y0, y1)].groupby("family").fire_cell_days.sum()
    if ov.get("MODIS", 0) <= 0 or ov.get("VIIRS", 0) <= 0:
        return None
    full = h[~h.partial]
    yearly = full.groupby("year").agg(raw=("raw", "sum"), harm=("fire_cell_days", "sum"), n=("month", "size"))
    yearly = yearly[yearly.n == 12]
    s0 = config.OVERLAP_START_YEAR
    before = yearly[(yearly.index >= s0 - years) & (yearly.index < s0)]
    after = yearly[(yearly.index >= s0) & (yearly.index < s0 + years)]
    jumps = {}
    if len(before) and len(after) and before.raw.mean() > 0 and before.harm.mean() > 0:
        jumps = {"before_years": [int(before.index.min()), int(before.index.max())],
                 "after_years": [int(after.index.min()), int(after.index.max())],
                 "raw_jump": round(float(after.raw.mean() / before.raw.mean()), 2),
                 "harmonized_jump": round(float(after.harm.mean() / before.harm.mean()), 2)}
    return {"overlap_years": [y0, y1], "viirs_per_modis": round(float(ov["VIIRS"] / ov["MODIS"]), 2), **jumps}


# ---------- current month vs history ----------

def _window_cell_days(a: Area, month: int, last_day: int) -> pd.DataFrame:
    """Per-year family totals for days 1..last_day of `month` (fair comparison for a partial month)."""
    g = sql(
        f"SELECT cell_id, lat, lon, date, sensor, frp_max FROM read_parquet(?) WHERE {_bbox_sql()} "
        "AND month(date) = ? AND day(date) <= ?",
        [_require("cells_daily"), *_bbox_args(a.bbox), month, last_day])
    return a.filter(g)


NRT_SOURCE = "VIIRS_NOAA20_NRT"  # S-NPP ended 2026-11-02; NOAA-20 is converted to S-NPP units by harmonize()
NRT_CHUNK_DAYS = 5  # the FIRMS area API serves 1–5 days per call


_NRT_CACHE: "collections.OrderedDict" = collections.OrderedDict()
NRT_CACHE_SECONDS, NRT_CACHE_MAX = 3600, 64


def _nrt_fetch(bbox: tuple[float, float, float, float], since: dt.date, until: dt.date, key: str):
    """NOAA-20 NRT hotspots for [since, until] in 5-day calls. Returns (frame, last day actually covered).
    Complete answers are cached for an hour; partial ones (FIRMS failed midway) are retried next time."""
    hit = _NRT_CACHE.get((bbox, since, until))
    if hit and time.time() - hit[0] < NRT_CACHE_SECONDS:
        return hit[1]
    out = _nrt_fetch_uncached(bbox, since, until, key)
    if out[1] >= until:
        now = time.time()
        for k in [k for k, (t, _) in _NRT_CACHE.items() if now - t >= NRT_CACHE_SECONDS]:
            del _NRT_CACHE[k]
        _NRT_CACHE[(bbox, since, until)] = (now, out)
        while len(_NRT_CACHE) > NRT_CACHE_MAX:
            _NRT_CACHE.popitem(last=False)
    return out


def _nrt_fetch_uncached(bbox, since: dt.date, until: dt.date, key: str):
    from pipeline.firms import fetch_area
    parts, covered = [], since - dt.timedelta(days=1)
    d = since
    while d <= until:
        n = min(NRT_CHUNK_DAYS, (until - d).days + 1)
        try:
            parts.append(fetch_area(NRT_SOURCE, bbox, days=n, date=d.isoformat(), map_key=key))
        except Exception:  # network, quota or an empty reply: stop, and report coverage only up to here
            break
        covered = d + dt.timedelta(days=n - 1)
        d += dt.timedelta(days=n)
    return (pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()), covered


def _nrt_cell_days(a: Area, since: dt.date, until: dt.date) -> tuple[pd.DataFrame, dt.date | None]:
    """Recent cell-days from FIRMS NRT (needs FIRMS_MAP_KEY), cleaned and with static sources masked."""
    key = os.environ.get("FIRMS_MAP_KEY")
    if not key or key == "your_map_key_here" or until < since:
        return pd.DataFrame(), None
    from pipeline.clean import clean, mask_static
    from pipeline.grid import to_grid
    raw, covered = _nrt_fetch(tuple(a.bbox), since, until, key)
    if covered < since:
        return pd.DataFrame(), None
    if raw.empty:
        return pd.DataFrame(), covered
    p = _path("static")
    static = sql("SELECT * FROM read_parquet(?)", [str(p)]) if p.exists() else pd.DataFrame(columns=["ki", "kj"])
    nrt = mask_static(clean(raw), static)
    nrt = nrt[pd.to_datetime(nrt.acq_date).dt.date.between(since, covered)]
    if nrt.empty:
        return pd.DataFrame(), covered
    g = to_grid(nrt)[["cell_id", "lat", "lon", "date", "sensor", "frp_max"]]
    return a.filter(g.assign(date=pd.to_datetime(g.date))), covered  # same dtype as the archive rows


OBS_COVERAGE = {"MODIS": "MODIS", "TERRA": "MODIS", "AQUA": "MODIS", "VIIRS": "VIIRS", "NOAA20": "NOAA20"}


def _window_obs(month: int, end_day: int, years) -> pd.DataFrame | None:
    """Share of days 1..end_day of `month` that each sensor group observed, per year (coverage minus the
    outages recorded by the pipeline). The same-days comparison then picks and fills sensors like the calendar."""
    md = meta()
    cov, gaps = md.get("coverage") or {}, md.get("outages") or {}
    if not cov:
        return None
    rows = []
    for y in years:
        days = pd.date_range(dt.date(y, month, 1), periods=end_day, freq="D")
        for g, ck in OBS_COVERAGE.items():
            c = cov.get(ck)
            if c is None:
                continue
            ok = (days >= pd.Timestamp(c[0])) & (days <= pd.Timestamp(c[1]))
            if g == "AQUA":
                ok &= days >= pd.Timestamp(dt.date(*config.AQUA_FIRST_MONTH, 4))
            for lo, hi in gaps.get(g, []):
                ok &= ~((days >= pd.Timestamp(lo)) & (days <= pd.Timestamp(hi)))
            rows.append({"family": g, "year": y, "month": month, "frac": float(ok.mean())})
    return pd.DataFrame(rows)


def current(a: Area, today: dt.date | None = None) -> dict:
    """This month so far vs the same days of the same month in every past year.

    Anchored to today: the month containing yesterday (the last complete day). Days after the archive ends
    come from the FIRMS NOAA-20 NRT feed. Without it (no key, or FIRMS unreachable) the card falls back to
    the latest month in the archive and says why (`live: false`, `note`).
    """
    last = latest_date()
    if last is None:
        raise NoData("No data built yet")
    yesterday = (today or dt.date.today()) - dt.timedelta(days=1)
    nrt, note, live = pd.DataFrame(), None, False
    if yesterday <= last:  # the archive already covers the current month so far
        year, month, end_day = yesterday.year, yesterday.month, yesterday.day
    else:
        nrt, covered = _nrt_cell_days(a, max(yesterday.replace(day=1), last + dt.timedelta(days=1)), yesterday)
        if covered is not None:
            year, month, end_day, live = covered.year, covered.month, covered.day, True
        else:
            note = ("Live FIRMS data unavailable (no FIRMS_MAP_KEY on the server, or FIRMS unreachable): "
                    f"showing the latest archived month instead of {cal.month_name[yesterday.month]} {yesterday.year}.")
            year, month = last_full_month() or (last.year, last.month)
            end_day = cal.monthrange(year, month)[1]
    g = _window_cell_days(a, month, end_day)
    if len(nrt):
        g = pd.concat([g, nrt[pd.to_datetime(nrt.date).dt.month == month]], ignore_index=True)
    days_in_month = cal.monthrange(year, month)[1]
    base = {"year": year, "month": month, "partial": end_day < days_in_month, "days_covered": end_day,
            "live": live, "source": "nrt" if live else "archive", "note": note}
    if g.empty:
        return {**base, "value": 0.0, "median": 0.0, "ratio": None, "highest_since": None, "rank": None, "n_years": 0}
    start = pd.Timestamp(meta().get("min_date", "2001-01-01"))
    first_year = start.year + int(month < start.month)  # the record starts Nov 2000: earlier months unobserved
    fam = cell_days_by_family(g)
    d = pd.to_datetime(fam.date)
    fam = fam.assign(year=d.dt.year, month=d.dt.month, strong=fam.frp_max >= config.STRONG_FRP_MW)
    per = fam.groupby(["cell_id", "year", "month", "family"], as_index=False).agg(
        fire_cell_days=("strong", "size"), strong_cell_days=("strong", "sum"))
    obs = _window_obs(month, end_day, range(first_year, year + 1))
    if live:  # the live month comes from NOAA-20 NRT
        lv = pd.DataFrame({"family": ["VIIRS", "NOAA20"], "year": year, "month": month, "frac": [0.0, 1.0]})
        obs = lv if obs is None else pd.concat([obs[~((obs.year == year) & (obs.month == month))], lv])
    h = harmonize(per, load_ratios(a), obs=obs)
    years = pd.DataFrame({"year": range(first_year, year + 1)})
    series = years.merge(h.groupby("year", as_index=False)["fire_cell_days"].sum(), on="year", how="left").fillna(0.0)
    series = series.set_index("year")["fire_cell_days"]
    value = float(series.get(year, 0.0))
    hist = series[series.index < year]
    median = float(hist.median()) if len(hist) else 0.0
    higher_before = hist[hist > value]
    return {
        **base, "value": round(value, 1), "median": round(median, 1),
        "ratio": round(value / median, 2) if median > 0 else None,
        "highest_since": int(higher_before.index.max()) if len(higher_before) else None,
        "rank": int((series > value).sum()) + 1, "n_years": int(len(series)),
    }


# ---------- map cells ----------

def cells(a: Area, start: int, end: int, limit: int) -> list[dict]:
    """Grid cells with fire in [start, end], summed in the calendar's harmonized units, largest first."""
    m = load_monthly(a)
    m = m[(m.year * 12 + m.month).between(start, end)]
    ratios = load_ratios(a)
    if m.empty or ratios is None:
        return []
    v = harmonize_cells(m, ratios, observed()).groupby(["lat", "lon"], as_index=False)["value"].sum()
    v = v[v.value > 0].nlargest(limit, "value")
    return [{"lat": round(float(r.lat), 4), "lon": round(float(r.lon), 4), "cell_days": round(float(r.value), 1)}
            for r in v.itertuples()]


# ---------- events ----------

def _event_dict(r) -> dict:
    return {"id": r.event_id, "start": str(r.start)[:10], "end": str(r.end)[:10], "duration_days": int(r.duration_days),
            "cell_days": int(r.cell_days), "area_km2": float(r.area_km2), "peak_frp": round(float(r.peak_frp), 1),
            "lat": round(float(r.lat), 4), "lon": round(float(r.lon), 4),
            "bbox": [round(float(r.west), 4), round(float(r.south), 4), round(float(r.east), 4), round(float(r.north), 4)]}


def load_events(a: Area) -> pd.DataFrame:
    """Events whose centroid lies in the area."""
    ev = sql(f"SELECT * FROM read_parquet(?) WHERE {_bbox_sql()}", [_require("events"), *_bbox_args(a.bbox)])
    return a.filter(ev)


def events(a: Area, start: str, end: str, limit: int) -> list[dict]:
    ev = load_events(a)
    s, e = pd.Timestamp(start), pd.Timestamp(end)
    ev = ev[(pd.to_datetime(ev.end) >= s) & (pd.to_datetime(ev.start) <= e)]
    ev = ev.sort_values(["cell_days", "peak_frp"], ascending=False).head(limit)
    return [_event_dict(r) for r in ev.itertuples()]


def precedent(a: Area, month: int) -> dict:
    ev = load_events(a)
    ev = ev[pd.to_datetime(ev.start).dt.month == month]
    lf = latest_date()
    if lf is not None:  # events still burning at the end of the record have unknown final duration
        ev = ev[pd.to_datetime(ev.end) < pd.Timestamp(lf) - pd.Timedelta(days=config.EVENT_GAP_DAYS)]
    if ev.empty:
        return {"month": month, "n_events": 0, "median_duration_days": None, "p90_duration_days": None,
                "median_area_km2": None, "largest": None}
    big = ev.loc[ev.cell_days.idxmax()]
    return {
        "month": month, "n_events": int(len(ev)),
        "median_duration_days": float(ev.duration_days.median()),
        "p90_duration_days": round(float(ev.duration_days.quantile(0.9)), 1),
        "median_area_km2": round(float(ev.area_km2.median()), 1),
        "largest": _event_dict(big),
    }


# ---------- validation ----------

def validation(a: Area | None) -> dict:
    p = _path("validation")
    if not p.exists():
        raise NoData("validation.json missing: rerun the pipeline")
    report = json.loads(p.read_text())
    region = region_for_bbox(a.bbox) if a else next(iter(report), "global")
    if region not in report:
        region = next(iter(report))
    return {"region": region, "region_name": config.REGIONS.get(region, {}).get("name", region), **report[region]}
