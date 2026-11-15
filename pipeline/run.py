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
from .grid import monthly_cell_days, to_grid
from .harmonize import fit_regional_ratios, validate


def main(raw_dir: str) -> None:
    t0 = time.time()
    raw = Path(raw_dir)
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

    max_date = pd.to_datetime(hotspots["acq_date"]).max()
    min_date = pd.to_datetime(hotspots["acq_date"]).min()
    period = max_date.to_period("M")
    if max_date != max_date + pd.offsets.MonthEnd(0):
        period -= 1  # the latest month is still in progress
    last_full = (period.year, period.month)
    report = {}
    for rid, reg in config.REGIONS.items():
        w, s, e, n = reg["bbox"]
        sub = monthly[monthly.lon.between(w, e) & monthly.lat.between(s, n)]
        try:
            report[rid] = validate(sub, last_full=last_full)
        except ValueError:
            continue
    (config.HARMONIZED / "validation.json").write_text(json.dumps(report))
    meta = {
        "synthetic": (raw / "SYNTHETIC").exists(),
        "min_date": str(min_date.date()), "max_date": str(max_date.date()),
        "last_full_month": f"{last_full[0]}-{last_full[1]:02d}",
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
