"""Snap hotspots to a common grid and count fire cell-days (removes most of the pixel-size effect)."""
from __future__ import annotations

import numpy as np
import pandas as pd

from . import config


def cell_index(lat: pd.Series, lon: pd.Series, deg: float = config.GRID_DEG):
    i = np.floor(np.asarray(lat, float) / deg).astype(int)
    j = np.floor(np.asarray(lon, float) / deg).astype(int)
    return i, j


def to_grid(df: pd.DataFrame, deg: float = config.GRID_DEG) -> pd.DataFrame:
    """One row per (cell, day, sensor). Schema documented in AGENTS.md."""
    i, j = cell_index(df["latitude"], df["longitude"], deg)
    g = df.assign(cell_id=pd.Series(i, index=df.index).astype(str) + "_" + pd.Series(j, index=df.index).astype(str),
                  lat=(i + 0.5) * deg, lon=(j + 0.5) * deg, date=df["acq_date"])
    out = (g.groupby(["cell_id", "lat", "lon", "date", "sensor"], as_index=False)
             .agg(n_detections=("frp", "size"), frp_sum=("frp", "sum"), frp_max=("frp", "max")))
    return out


def family(sensor: pd.Series) -> np.ndarray:
    """MODIS / VIIRS family; VIIRS satellites outside VIIRS_FAMILY_SENSORS map to None (excluded)."""
    s = np.asarray(sensor)
    return np.where(s == "MODIS", "MODIS", np.where(np.isin(s, list(config.VIIRS_FAMILY_SENSORS)), "VIIRS", None))


def cell_days_by_family(grid: pd.DataFrame) -> pd.DataFrame:
    """Unique (cell, date, family) rows: a cell-day counts once per family even if several satellites saw it."""
    g = grid.assign(family=family(grid["sensor"]))
    g = g[g["family"].notna()]
    return g.groupby(["cell_id", "lat", "lon", "date", "family"], as_index=False)["frp_max"].max()


def monthly_cell_days(grid: pd.DataFrame) -> pd.DataFrame:
    """Fire cell-days per cell, month and sensor family (MODIS vs VIIRS).

    `strong_cell_days` counts the cell-days whose max FRP ≥ STRONG_FRP_MW (used by frp_strata).
    """
    g = cell_days_by_family(grid)
    d = pd.to_datetime(g["date"])
    g = g.assign(year=d.dt.year, month=d.dt.month, strong=g["frp_max"] >= config.STRONG_FRP_MW)
    return (g.groupby(["cell_id", "lat", "lon", "year", "month", "family"], as_index=False)
             .agg(fire_cell_days=("strong", "size"), strong_cell_days=("strong", "sum")))
