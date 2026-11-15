"""Data access + analytics for the API. Pure-ish functions over the Parquet files built by pipeline.run."""
from __future__ import annotations

import calendar as cal
import datetime as dt
import json
import os

import duckdb
import numpy as np
import pandas as pd

from pipeline import config
from pipeline.grid import cell_days_by_family
from pipeline.harmonize import harmonize, ratios_for_region, raw_series, region_for_bbox

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
    return ratios_for_region(pd.read_parquet(p), region_for_bbox(a.bbox))


def last_full_month() -> tuple[int, int] | None:
    m = meta().get("last_full_month")
    if not m:
        return None
    y, mo = m.split("-")
    return int(y), int(mo)


def latest_date() -> dt.date | None:
    d = meta().get("max_date")
    return dt.date.fromisoformat(d) if d else None


# ---------- calendar ----------

def calendar_frame(a: Area, start: int, end: int) -> tuple[pd.DataFrame, pd.Series]:
    """Harmonized months in [start, end] (as year*12+month) with anomaly; also median per calendar month.

    Medians use complete months only (the in-progress month would drag them down). `raw`/`raw_source`
    carry the un-harmonized stitched record (MODIS before the overlap, VIIRS after) for comparison.
    """
    monthly = load_monthly(a)
    if monthly.empty:
        return pd.DataFrame(columns=["year", "month", "fire_cell_days", "lo", "hi", "source", "anomaly",
                                     "raw", "raw_source", "partial"]), pd.Series(dtype=float)
    h = harmonize(monthly, load_ratios(a))
    h = h.merge(raw_series(monthly), on=["year", "month"], how="left")
    h = _fill_missing_months(h)
    lf = last_full_month()
    key = h.year * 12 + h.month
    h = h.assign(partial=(key > lf[0] * 12 + lf[1]) if lf else False)
    h = h[(key >= start) & (key <= end)]
    med = h[~h.partial].groupby("month")["fire_cell_days"].median()
    h = h.assign(anomaly=h.fire_cell_days / h.month.map(med).replace(0, np.nan))
    return h.reset_index(drop=True), med


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
    src = np.where(out.year < config.OVERLAP_START_YEAR, "modis_converted", "viirs")
    raw_src = np.where(out.year < config.OVERLAP_START_YEAR, "modis", "viirs")
    out = out.assign(source=out.source.fillna(pd.Series(src)),
                     fire_cell_days=out.fire_cell_days.fillna(0.0), lo=out.lo.fillna(0.0), hi=out.hi.fillna(0.0))
    if "raw" in out:
        out = out.assign(raw=out.raw.fillna(0.0), raw_source=out.raw_source.fillna(pd.Series(raw_src)))
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
    trend = None
    if len(complete) >= 8 and complete.v.mean() > 0:
        slope = np.polyfit(complete.index.to_numpy(float), complete.v.to_numpy(float), 1)[0]
        trend = round(float(100 * slope * 10 / complete.v.mean()), 1)
    an = h[h.anomaly.notna() & (h.fire_cell_days >= 5)].nlargest(5, "anomaly")
    return {
        "total": round(float(h.fire_cell_days.sum()), 1),
        "peak_month": {"year": int(pk.year), "month": int(pk.month), "value": round(float(pk.fire_cell_days), 1)},
        "peak_year": None if py is None else {"year": int(py), "value": round(float(complete.v[py]), 1)},
        "typical_peak_month": int(seas.idxmax()) if len(seas) and seas.max() > 0 else None,
        "trend_per_decade_pct": trend,
        "top_anomalies": [{"year": int(r.year), "month": int(r.month), "value": round(float(r.fire_cell_days), 1),
                           "anomaly": round(float(r.anomaly), 2)} for r in an.itertuples()],
    }


def months_payload(h: pd.DataFrame) -> list[dict]:
    return [{"year": int(r.year), "month": int(r.month), "value": round(float(r.fire_cell_days), 2),
             "lo": round(float(r.lo), 2), "hi": round(float(r.hi), 2), "source": r.source,
             "anomaly": None if pd.isna(r.anomaly) else round(float(r.anomaly), 3),
             "partial": bool(r.partial),
             "raw": round(float(r.raw), 2) if "raw" in h and pd.notna(r.raw) else None,
             "raw_source": getattr(r, "raw_source", None)} for r in h.itertuples()]


def handoff(h: pd.DataFrame, years: int = 3) -> dict | None:
    """Mean annual total for `years` complete years either side of the MODIS→VIIRS handoff, raw and harmonized.

    `raw_jump` near 1 would mean no sensor artefact; the harmonized jump should be much closer to 1 than raw.
    """
    if h.empty or "raw" not in h:
        return None
    full = h[~h.partial]
    yearly = full.groupby("year").agg(raw=("raw", "sum"), harm=("fire_cell_days", "sum"), n=("month", "size"))
    yearly = yearly[yearly.n == 12]
    y0 = config.OVERLAP_START_YEAR
    before = yearly[(yearly.index >= y0 - years) & (yearly.index < y0)]
    after = yearly[(yearly.index >= y0) & (yearly.index < y0 + years)]
    if before.empty or after.empty or before.raw.mean() <= 0 or before.harm.mean() <= 0:
        return None
    return {"before_years": [int(before.index.min()), int(before.index.max())],
            "after_years": [int(after.index.min()), int(after.index.max())],
            "raw_jump": round(float(after.raw.mean() / before.raw.mean()), 2),
            "harmonized_jump": round(float(after.harm.mean() / before.harm.mean()), 2)}


# ---------- current month vs history ----------

def _window_cell_days(a: Area, month: int, last_day: int) -> pd.DataFrame:
    """Per-year family totals for days 1..last_day of `month` (fair comparison for a partial month)."""
    g = sql(
        f"SELECT cell_id, lat, lon, date, sensor, frp_max FROM read_parquet(?) WHERE {_bbox_sql()} "
        "AND month(date) = ? AND day(date) <= ?",
        [_require("cells_daily"), *_bbox_args(a.bbox), month, last_day])
    return a.filter(g)


def _nrt_cell_days(a: Area, since: dt.date, until: dt.date) -> pd.DataFrame:
    """Recent VIIRS SNPP cell-days from the FIRMS NRT API (only if FIRMS_MAP_KEY is set)."""
    key = os.environ.get("FIRMS_MAP_KEY")
    days = (until - since).days
    if not key or key == "your_map_key_here" or days <= 0:
        return pd.DataFrame()
    from pipeline.clean import clean
    from pipeline.firms import fetch_area
    from pipeline.grid import to_grid
    try:
        nrt = fetch_area("VIIRS_SNPP_NRT", a.bbox, days=min(days, 10), map_key=key)
    except Exception:  # network / quota problems must not break the card
        return pd.DataFrame()
    nrt = clean(nrt)
    nrt = nrt[pd.to_datetime(nrt.acq_date).dt.date > since]
    return a.filter(to_grid(nrt)[["cell_id", "lat", "lon", "date", "sensor", "frp_max"]]) if len(nrt) else pd.DataFrame()


def current(a: Area, today: dt.date | None = None) -> dict:
    last = latest_date()
    if last is None:
        raise NoData("No data built yet")
    today = today or dt.date.today()
    g = _window_cell_days(a, last.month, last.day)
    source = "archive"
    nrt = _nrt_cell_days(a, last, today) if last < today else pd.DataFrame()
    end_day, year, month = last.day, last.year, last.month
    if len(nrt) and today.year == year and today.month == month:
        # Extend the comparison window to today for every year, then add the live detections.
        end_day, source = today.day, "nrt"
        nrt = nrt[pd.to_datetime(nrt.date).dt.month == month]
        g = pd.concat([_window_cell_days(a, month, end_day), nrt], ignore_index=True)
    days_in_month = cal.monthrange(year, month)[1]
    if g.empty:
        return {"year": year, "month": month, "value": 0.0, "median": 0.0, "ratio": None, "highest_since": None,
                "rank": None, "n_years": 0, "partial": end_day < days_in_month, "days_covered": end_day,
                "source": source}
    fam = cell_days_by_family(g)
    d = pd.to_datetime(fam.date)
    fam = fam.assign(year=d.dt.year, month=d.dt.month, strong=fam.frp_max >= config.STRONG_FRP_MW)
    per = fam.groupby(["cell_id", "year", "month", "family"], as_index=False).agg(
        fire_cell_days=("strong", "size"), strong_cell_days=("strong", "sum"))
    h = harmonize(per, load_ratios(a))
    md = meta()
    first_year = pd.Timestamp(md.get("min_date", "2001-01-01")).year
    years = pd.DataFrame({"year": range(first_year, year + 1)})
    series = years.merge(h[["year", "fire_cell_days"]], on="year", how="left").fillna(0.0)
    series = series.set_index("year")["fire_cell_days"]
    value = float(series.get(year, 0.0))
    hist = series[series.index < year]
    median = float(hist.median()) if len(hist) else 0.0
    higher_before = hist[hist > value]
    rank = int((series > value).sum()) + 1
    return {
        "year": year, "month": month, "value": round(value, 1), "median": round(median, 1),
        "ratio": round(value / median, 2) if median > 0 else None,
        "highest_since": int(higher_before.index.max()) if len(higher_before) else None,
        "rank": rank, "n_years": int(len(series)),
        "partial": end_day < days_in_month, "days_covered": end_day, "source": source,
    }


# ---------- map cells ----------

def cells(a: Area, start: int, end: int, limit: int) -> list[dict]:
    df = sql(
        f"""SELECT lat, lon, SUM(v) AS cell_days FROM (
              SELECT cell_id, lat, lon, year, month, MAX(fire_cell_days) AS v FROM read_parquet(?)
              WHERE {_bbox_sql()} AND year * 12 + month BETWEEN ? AND ?
              GROUP BY cell_id, lat, lon, year, month)
            GROUP BY lat, lon ORDER BY cell_days DESC""",
        [_require("monthly"), *_bbox_args(a.bbox), start, end])
    df = a.filter(df).head(limit)
    return [{"lat": round(float(r.lat), 4), "lon": round(float(r.lon), 4), "cell_days": int(r.cell_days)}
            for r in df.itertuples()]


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
