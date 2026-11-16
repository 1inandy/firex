"""Build all derived data:  python -m pipeline.run data/raw/<region>"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pandas as pd

from . import config
from .clean import clean
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
            fam, end = PRODUCT_FAMILY.get(parts[0]), f"{parts[1]}-12-31"
        elif len(parts) == 3:
            fam, end = PRODUCT_FAMILY.get(parts[0]), parts[2]
        else:
            continue
        if fam:
            ends[fam] = max(ends.get(fam, end), end)
    fam = family(hotspots["sensor"])
    starts = pd.Series(pd.to_datetime(hotspots["acq_date"]).to_numpy()).groupby(fam).min()
    return {f: [str(starts[f].date()), ends[f]] for f in ends if f in starts.index}


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
    hotspots = clean(loaded)
    print(f"cleaned {len(loaded):,} -> {len(hotspots):,} hotspots ({time.time() - t0:.0f}s)")
    hotspots.to_parquet(config.GRID / "hotspots.parquet", index=False)
    grid = to_grid(hotspots)
    grid.to_parquet(config.GRID / "cells_daily.parquet", index=False)
    monthly = monthly_cell_days(grid)
    monthly.to_parquet(config.GRID / "monthly.parquet", index=False)
    ratios = fit_regional_ratios(monthly)
    ratios.to_parquet(config.HARMONIZED / "ratios.parquet", index=False)
    print(f"gridded + harmonized ({time.time() - t0:.0f}s)")
    events = build_events(grid)
    events.to_parquet(config.GRID / "events.parquet", index=False)
    print(f"{len(events):,} fire events ({time.time() - t0:.0f}s)")

    min_date = pd.to_datetime(hotspots["acq_date"]).min()
    cov = coverage(raw, hotspots) if not markers else {}
    record_end = max([c[1] for f, c in cov.items() if f in ("VIIRS", "NOAA20")],
                     default=str(pd.to_datetime(hotspots["acq_date"]).max().date()))
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
            v = validate(sub, last_full=last_full, bands=bands)
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
