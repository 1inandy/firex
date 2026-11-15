"""Snap hotspots to a common grid and count fire cell-days (removes most of the pixel-size effect)."""
from __future__ import annotations

import numpy as np
import pandas as pd

from . import config


def cell_index(lat: pd.Series, lon: pd.Series, deg: float = config.GRID_DEG):
    i = np.floor(lat / deg).astype(int)
    j = np.floor(lon / deg).astype(int)
    return i, j


def to_grid(df: pd.DataFrame, deg: float = config.GRID_DEG) -> pd.DataFrame:
    """One row per (cell, day, sensor). Schema documented in AGENTS.md."""
    i, j = cell_index(df["latitude"], df["longitude"], deg)
    g = df.assign(cell_id=i.astype(str) + "_" + j.astype(str),
                  lat=(i + 0.5) * deg, lon=(j + 0.5) * deg, date=df["acq_date"])
    out = (g.groupby(["cell_id", "lat", "lon", "date", "sensor"], as_index=False)
             .agg(n_detections=("frp", "size"), frp_sum=("frp", "sum"), frp_max=("frp", "max")))
    return out


def monthly_cell_days(grid: pd.DataFrame) -> pd.DataFrame:
    """Fire cell-days per cell, month and sensor family (MODIS vs VIIRS)."""
    d = pd.to_datetime(grid["date"])
    fam = np.where(grid["sensor"] == "MODIS", "MODIS", "VIIRS")
    g = grid.assign(year=d.dt.year, month=d.dt.month, family=fam)
    # a cell-day counts once per family even if several VIIRS satellites saw it
    g = g.drop_duplicates(["cell_id", "date", "family"])
    return g.groupby(["cell_id", "lat", "lon", "year", "month", "family"], as_index=False).size() \
            .rename(columns={"size": "fire_cell_days"})
