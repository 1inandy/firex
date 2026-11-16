"""Build all derived data:  python -m pipeline.run data/raw/<region>"""
from __future__ import annotations

import datetime as dt
import json
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

from . import config
from .clean import clean, mask_static, static_keys
from .events import build_events
from .firms import load_archive
from .grid import family, monthly_cell_days, to_grid
from .harmonize import calibrate_bands, fit_regional_ratios, validate


PRODUCT_FAMILY = {"modis": "MODIS", "viirs-snpp": "VIIRS", "viirs-jpss1": "NOAA20",
                  "VIIRS_SNPP_NRT": "VIIRS", "VIIRS_SNPP_SP": "VIIRS",
                  "VIIRS_NOAA20_NRT": "NOAA20", "VIIRS_NOAA20_SP": "NOAA20"}


def coverage(raw: Path, hotspots: pd.DataFrame) -> dict[str, list[str]]:
    """Per family: [first detection date, last day covered by the input files].

    The end comes from what was downloaded (yearly file -> Dec 31, API file <source>_<from>_<to> -> <to>), not
    from the last detection, so a quiet final day isn't mistaken for missing data.
    """
    ends: dict[str, str] = {}
    for f in raw.rglob("*.csv.gz"):
        parts = f.name.removesuffix(".csv.gz").rsplit("_", 2)
        if len(parts) == 2 and parts[1].isdigit():
            if int(parts[1]) >= dt.date.today().year:
                continue  # a current-year file may stop mid-year: let the detections decide
            fam, end = PRODUCT_FAMILY.get(parts[0]), f"{parts[1]}-12-31"
        elif len(parts) == 3:
            fam, end = PRODUCT_FAMILY.get(parts[0]), parts[2]
        else:
            continue
        if fam:
            ends[fam] = max(ends.get(fam, end), end)
    dates = pd.Series(pd.to_datetime(hotspots["acq_date"]).to_numpy()).groupby(family(hotspots["sensor"]))
    starts, last = dates.min(), dates.max()
    # Files without a parseable coverage (e.g. synthetic demo data): fall back to the detections.
    return {f: [str(starts[f].date()), ends.get(f, str(last[f].date()))] for f in starts.index}


OBS_GROUPS = {  # sensor group -> how to pick its detections
    "MODIS": lambda h: h.sensor == "MODIS", "TERRA": lambda h: h.satellite == "Terra",
    "AQUA": lambda h: h.satellite == "Aqua", "VIIRS": lambda h: h.sensor == "VIIRS_SNPP",
    "NOAA20": lambda h: h.sensor == "VIIRS_NOAA20",
}
INDEPENDENT = {"MODIS": ("VIIRS", "NOAA20"), "TERRA": ("AQUA", "VIIRS", "NOAA20"),
               "AQUA": ("TERRA", "VIIRS", "NOAA20"), "VIIRS": ("MODIS", "NOAA20"), "NOAA20": ("MODIS", "VIIRS")}
OUTAGE_MIN_EXPECTED = 20.0
COVERAGE_OF = {"MODIS": "MODIS", "TERRA": "MODIS", "AQUA": "MODIS", "VIIRS": "VIIRS", "NOAA20": "NOAA20"}


def observed(hotspots: pd.DataFrame, cov: dict, start: str, end: str) -> tuple[pd.DataFrame, dict]:
    """Share of each month's days on which each sensor group was delivering data, and its outages.

    An outage is a run of 2+ days inside the group's coverage with no detection from it anywhere in the data,
    although independent satellites saw enough fire that it should have made OUTAGE_MIN_EXPECTED or more
    (its usual share of their detections over the surrounding ±30 days). Quiet days are not outages. (Before Aqua, Terra has no independent
    witness, so Terra-only outages can't be detected.)
    Months are measured against the record span, so the in-progress last month is not an outage.
    """
    days = pd.date_range(start, end, freq="D")
    dates = pd.to_datetime(hotspots["acq_date"])
    count = {g: dates[pick(hotspots)].value_counts().reindex(days, fill_value=0).to_numpy()
             for g, pick in OBS_GROUPS.items()}
    rows, gaps = [], {}
    for g in OBS_GROUPS:
        c = cov.get(COVERAGE_OF[g])
        if c is None or not count[g].any():
            continue
        lo = max(pd.Timestamp(c[0]), days[count[g] > 0].min()) if g == "AQUA" else pd.Timestamp(c[0])
        in_cov = (days >= lo) & (days <= pd.Timestamp(c[1]))
        silent = in_cov & (count[g] == 0)
        others = np.sum([count[k] for k in INDEPENDENT[g]], axis=0)
        gap = np.zeros(len(days), dtype=bool)
        idx = np.flatnonzero(silent)
        for r in (np.split(idx, np.flatnonzero(np.diff(idx) > 1) + 1) if len(idx) else []):
            if len(r) < 2:
                continue
            # Detections expected from this sensor, given what its witnesses saw and its usual share of them
            # over the surrounding ±30 days. Fire counts are bursty, so the bar is high: ≥ 20 expected, none seen.
            win = np.r_[max(r[0] - 30, 0):r[0], r[-1] + 1:min(r[-1] + 31, len(days))]
            share = count[g][win].sum() / max(others[win].sum(), 1)
            if share * others[r].sum() >= OUTAGE_MIN_EXPECTED:
                gap[r] = True
        f = pd.DataFrame({"year": days.year, "month": days.month, "ok": in_cov & ~gap})
        f = f.groupby(["year", "month"], as_index=False).ok.mean().rename(columns={"ok": "frac"})
        rows.append(f.assign(family=g))
        idx = np.flatnonzero(gap)
        runs = np.split(idx, np.flatnonzero(np.diff(idx) > 1) + 1) if len(idx) else []
        gaps[g] = [[str(days[r[0]].date()), str(days[r[-1]].date())] for r in runs]
    return pd.concat(rows, ignore_index=True)[["family", "year", "month", "frac"]], gaps


def write_hotspots(df: pd.DataFrame, path: Path) -> None:
    """Compact hotspot table for the CSV export: exact 5-decimal coordinates as fixed-point numbers, rows
    sorted by date and place, ZSTD. About half the size of the default, so the deploy stays small."""
    import duckdb
    with duckdb.connect() as con:
        con.register("h", df)
        con.execute(f"""COPY (SELECT latitude::DECIMAL(8,5) AS latitude, longitude::DECIMAL(8,5) AS longitude,
                                     acq_date::DATE AS acq_date, acq_time, sensor, satellite, confidence,
                                     frp::DECIMAL(8,2) AS frp, daynight, type::TINYINT AS type
                              FROM h ORDER BY acq_date, sensor, latitude, longitude)
                        TO '{path}' (FORMAT parquet, COMPRESSION zstd, COMPRESSION_LEVEL 19)""")


def _last_full_month(day: str) -> tuple[int, int]:
    d = pd.Timestamp(day)
    period = d.to_period("M")
    return (period.year, period.month) if d == d + pd.offsets.MonthEnd(0) else ((period - 1).year, (period - 1).month)


def main(raw_dir: str) -> None:
    t0 = time.time()
    raw = Path(raw_dir)
    markers = list(raw.rglob("SYNTHETIC"))
    real = [f for f in raw.rglob("*.csv*") if not any(m.parent in f.parents for m in markers)]
    if markers and real:
        raise SystemExit(f"{raw} mixes synthetic ({markers[0].parent}) and real FIRMS files; build them separately")
    config.GRID.mkdir(parents=True, exist_ok=True)
    config.HARMONIZED.mkdir(parents=True, exist_ok=True)
    loaded = load_archive(raw)
    static = static_keys(loaded)  # where the archive says gas flares / industry / volcanoes are
    static.to_parquet(config.GRID / "static_sources.parquet", index=False)
    hotspots = mask_static(clean(loaded), static)
    print(f"cleaned {len(loaded):,} -> {len(hotspots):,} hotspots ({time.time() - t0:.0f}s)")
    write_hotspots(hotspots, config.GRID / "hotspots.parquet")
    grid = to_grid(hotspots)
    grid.to_parquet(config.GRID / "cells_daily.parquet", index=False)
    monthly = monthly_cell_days(grid)
    monthly.to_parquet(config.GRID / "monthly.parquet", index=False)
    cov = coverage(raw, hotspots)
    min_date = pd.to_datetime(hotspots["acq_date"]).min()
    record_end = max([c[1] for f, c in cov.items() if f in ("VIIRS", "NOAA20")],
                     default=str(pd.to_datetime(hotspots["acq_date"]).max().date()))
    obs, gaps = observed(hotspots, cov, str(min_date.date()), record_end)
    obs.to_parquet(config.GRID / "observed.parquet", index=False)
    ratios = fit_regional_ratios(monthly, obs=obs)
    ratios.to_parquet(config.HARMONIZED / "ratios.parquet", index=False)
    print(f"gridded + harmonized ({time.time() - t0:.0f}s)")
    events = build_events(grid)
    events.to_parquet(config.GRID / "events.parquet", index=False)
    print(f"{len(events):,} fire events ({time.time() - t0:.0f}s)")

    last_full = _last_full_month(record_end)
    snpp_end = _last_full_month(cov["VIIRS"][1]) if "VIIRS" in cov else None
    report, parts = {}, []
    for rid, reg in [*config.REGIONS.items(), ("global", None)]:
        if reg:
            w, s, e, n = reg["bbox"]
            sub = monthly[monthly.lon.between(w, e) & monthly.lat.between(s, n)]
        else:
            sub = monthly
        bands: dict = {}
        try:
            v = validate(sub, last_full=last_full, bands=bands, obs=obs)
        except ValueError:
            continue
        if reg:
            report[rid] = v
        table = ratios[ratios.region == rid].drop(columns="region")
        parts.append(calibrate_bands(table, bands).assign(region=rid))
    if parts:  # ship the leave-one-year-out bands, not the in-sample ones
        ratios = pd.concat(parts, ignore_index=True)
        ratios.to_parquet(config.HARMONIZED / "ratios.parquet", index=False)
    (config.HARMONIZED / "validation.json").write_text(json.dumps(report))
    meta = {
        "synthetic": bool(markers),
        "min_date": str(min_date.date()), "max_date": record_end,
        "last_full_month": f"{last_full[0]}-{last_full[1]:02d}",
        "snpp_end": f"{snpp_end[0]}-{snpp_end[1]:02d}" if snpp_end else None,
        "coverage": cov,
        "outages": gaps,
        "n_hotspots": int(len(hotspots)), "n_events": int(len(events)),
        "built": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    config.META.write_text(json.dumps(meta, indent=2))
    for rid, v in report.items():
        print(f"validation {rid}: annual |err| {v['annual_abs_error_pct']}%, monthly MAPE "
              f"{v['mape_monthly_pct']}%, r2 {v['r2']}")
    print(f"done in {time.time() - t0:.0f}s -> {config.DATA}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else str(config.RAW))
