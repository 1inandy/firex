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
                  lat=(i + 0.5) * deg, lon=(j + 0.5) * deg, date=df["acq_date"],
                  terra=(df["satellite"].astype(str) == "Terra").astype(int) if "satellite" in df else 0)
    out = (g.groupby(["cell_id", "lat", "lon", "date", "sensor"], as_index=False)
             .agg(n_detections=("frp", "size"), frp_sum=("frp", "sum"), frp_max=("frp", "max"),
                  n_terra=("terra", "sum")))
    return out


def family(sensor: pd.Series) -> np.ndarray:
    """MODIS, VIIRS (S-NPP) or NOAA20 family; other satellites map to None (excluded)."""
    s = np.asarray(sensor)
    return np.select([s == "MODIS", np.isin(s, list(config.VIIRS_FAMILY_SENSORS)), np.isin(s, list(config.NOAA20_SENSORS))],
                     ["MODIS", "VIIRS", "NOAA20"], None)


def cell_days_by_family(grid: pd.DataFrame) -> pd.DataFrame:
    """Unique (cell, date, family) rows: a cell-day counts once per family even if several satellites saw it."""
    g = grid.assign(family=family(grid["sensor"]))
    g = g[g["family"].notna()]
    agg = {"frp_max": "max", **({"n_terra": "sum"} if "n_terra" in g else {})}
    return g.groupby(["cell_id", "lat", "lon", "date", "family"], as_index=False).agg(agg)


def monthly_cell_days(grid: pd.DataFrame) -> pd.DataFrame:
    """Fire cell-days per cell, month and sensor family (MODIS vs VIIRS).

    `strong_cell_days` counts the cell-days whose max FRP ≥ STRONG_FRP_MW (used by frp_strata);
    `terra_cell_days` the MODIS cell-days that Terra saw (used to calibrate the Terra-only months).
    """
    g = cell_days_by_family(grid)
    d = pd.to_datetime(g["date"])
    g = g.assign(year=d.dt.year, month=d.dt.month, strong=g["frp_max"] >= config.STRONG_FRP_MW,
                 terra=g["n_terra"] > 0 if "n_terra" in g else False)
    return (g.groupby(["cell_id", "lat", "lon", "year", "month", "family"], as_index=False)
             .agg(fire_cell_days=("strong", "size"), strong_cell_days=("strong", "sum"),
                  terra_cell_days=("terra", "sum")))
