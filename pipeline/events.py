"""Group gridded hotspots into individual fire events (space-time connected components).

Two fire cell-days belong to the same event when their grid cells touch (8-neighbourhood) and they
are at most EVENT_GAP_DAYS apart. This is DBSCAN-like with a fixed neighbourhood but needs no extra
dependency and runs in seconds on millions of cell-days.

Caveat: from 2012 VIIRS sees more small fire pixels, so event areas after 2012 tend to be larger
than MODIS-only events of the same real size. Precedent stats should be read with that in mind.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from . import config
from .grid import cell_index

KM_PER_DEG = 111.32


def _components(n: int, a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """Connected-component labels for n nodes and undirected edges (a, b), via min-label propagation."""
    labels = np.arange(n)
    if len(a) == 0:
        return labels
    while True:
        prev = labels.copy()
        m = np.minimum(labels[a], labels[b])
        np.minimum.at(labels, a, m)
        np.minimum.at(labels, b, m)
        labels = labels[labels]  # pointer jumping shortens chains quickly
        labels = labels[labels]
        if np.array_equal(labels, prev):
            return labels


def label_cell_days(grid: pd.DataFrame, deg: float = config.GRID_DEG,
                    gap_days: int = config.EVENT_GAP_DAYS) -> pd.DataFrame:
    """Unique (cell, date) rows from a cells_daily frame with an `event` label column."""
    cd = (grid.groupby(["cell_id", "lat", "lon", "date"], as_index=False)
              .agg(n_detections=("n_detections", "sum"), frp_max=("frp_max", "max")))
    i, j = cell_index(cd["lat"], cd["lon"], deg)
    day = (pd.to_datetime(cd["date"]) - pd.Timestamp("2000-01-01")).dt.days.to_numpy()
    nodes = pd.DataFrame({"node": np.arange(len(cd)), "i": i, "j": j, "day": day})
    a_parts, b_parts = [], []
    for di in (-1, 0, 1):
        for dj in (-1, 0, 1):
            for dd in range(0, gap_days + 1):
                if dd == 0 and (di, dj) <= (0, 0):  # same-day pairs once per direction, no self loops
                    continue
                shifted = nodes.assign(i=nodes.i + di, j=nodes.j + dj, day=nodes.day + dd)
                e = nodes.merge(shifted, on=["i", "j", "day"], suffixes=("_a", "_b"))
                a_parts.append(e.node_a.to_numpy())
                b_parts.append(e.node_b.to_numpy())
    a = np.concatenate(a_parts) if a_parts else np.array([], int)
    b = np.concatenate(b_parts) if b_parts else np.array([], int)
    return cd.assign(event=_components(len(cd), a, b))


def summarize_events(labelled: pd.DataFrame, deg: float = config.GRID_DEG,
                     min_cell_days: int = config.EVENT_MIN_CELL_DAYS) -> pd.DataFrame:
    """One row per event: start, end, duration, size, intensity, centroid, bbox."""
    d = labelled.assign(date=pd.to_datetime(labelled["date"]))
    cell_km2 = (deg * KM_PER_DEG) ** 2
    g = d.groupby("event")
    ev = pd.DataFrame({
        "start": g["date"].min(), "end": g["date"].max(), "cell_days": g.size(),
        "n_cells": g["cell_id"].nunique(), "peak_frp": g["frp_max"].max(),
        "lat": g["lat"].mean(), "lon": g["lon"].mean(),
        "west": g["lon"].min() - deg / 2, "east": g["lon"].max() + deg / 2,
        "south": g["lat"].min() - deg / 2, "north": g["lat"].max() + deg / 2,
    })
    ev = ev[ev.cell_days >= min_cell_days].reset_index(drop=True)
    ev["duration_days"] = (ev["end"] - ev["start"]).dt.days + 1
    ev["area_km2"] = (ev["n_cells"] * cell_km2 * np.cos(np.radians(ev["lat"]))).round(1)
    ev = ev.sort_values(["start", "lat"]).reset_index(drop=True)
    ev.insert(0, "event_id", [f"e{k}" for k in range(len(ev))])
    ev["start"] = ev["start"].dt.date
    ev["end"] = ev["end"].dt.date
    return ev


def build_events(grid: pd.DataFrame) -> pd.DataFrame:
    return summarize_events(label_cell_days(grid))
