"""Build all derived data:  python -m pipeline.run data/raw/<region>"""
from __future__ import annotations

import sys
from pathlib import Path

from . import config
from .clean import clean
from .firms import load_archive
from .grid import monthly_cell_days, to_grid
from .harmonize import fit_monthly_ratio


def main(raw_dir: str) -> None:
    config.GRID.mkdir(parents=True, exist_ok=True)
    config.HARMONIZED.mkdir(parents=True, exist_ok=True)
    hotspots = clean(load_archive(Path(raw_dir)))
    hotspots.to_parquet(config.GRID / "hotspots.parquet", index=False)
    grid = to_grid(hotspots)
    grid.to_parquet(config.GRID / "cells_daily.parquet", index=False)
    monthly = monthly_cell_days(grid)
    monthly.to_parquet(config.GRID / "monthly.parquet", index=False)
    fit_monthly_ratio(monthly).to_parquet(config.HARMONIZED / "ratios.parquet", index=False)
    print(f"{len(hotspots):,} hotspots -> {len(grid):,} cell-days -> {config.DATA}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else str(config.RAW))
