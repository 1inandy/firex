"""Filter hotspots down to real vegetation/land fires with acceptable confidence, without duplicates."""
from __future__ import annotations

import numpy as np
import pandas as pd

from . import config

KM_PER_DEG = 111.32


def keep_confident(df: pd.DataFrame) -> pd.Series:
    is_modis = df["sensor"] == "MODIS"
    modis_ok = pd.to_numeric(df["confidence"], errors="coerce") >= config.MODIS_MIN_CONFIDENCE
    viirs_ok = df["confidence"].isin(config.VIIRS_KEEP_CONFIDENCE)
    return (is_modis & modis_ok) | (~is_modis & viirs_ok)


def _minutes(acq_time: pd.Series) -> np.ndarray:
    t = pd.to_numeric(acq_time, errors="coerce").fillna(0).to_numpy().astype(int)
    return (t // 100) * 60 + t % 100


def swath_duplicates(df: pd.DataFrame) -> pd.Series:
    """True for detections that repeat an earlier one from the same overpass less than a pixel away.

    Neighbouring scan lines overlap toward the swath edge (the "bow-tie" effect), so one fire can be
    reported twice by the same satellite within seconds. We link detections with the same satellite,
    date and day/night, at most DEDUPE_MINUTES apart and closer than the sensor's pixel size
    (DEDUPE_KM), and keep only the first of each linked pair.
    """
    if df.empty:
        return pd.Series(False, index=df.index)
    fam = np.where(df["sensor"] == "MODIS", "MODIS", "VIIRS")
    tol = np.where(fam == "MODIS", config.DEDUPE_KM["MODIS"], config.DEDUPE_KM["VIIRS"])
    tol_deg = tol / KM_PER_DEG
    lat = df["latitude"].to_numpy(float)
    lon = df["longitude"].to_numpy(float)
    coslat = np.cos(np.radians(lat))
    # Hash points into cells of size = tolerance, so true neighbours are in the same or adjacent cell.
    pts = pd.DataFrame({
        "row": np.arange(len(df)),
        "key": pd.factorize(pd.Series(df["sensor"].astype(str).to_numpy()) + "|"
                            + df["satellite"].astype(str).to_numpy() + "|"
                            + df["acq_date"].astype(str).to_numpy() + "|"
                            + df["daynight"].astype(str).to_numpy())[0],
        "ci": np.floor(lat / tol_deg).astype(np.int64),
        "cj": np.floor(lon * coslat / tol_deg).astype(np.int64),
    })
    minutes = _minutes(df["acq_time"])
    drop = np.zeros(len(df), dtype=bool)
    for di in (-1, 0, 1):
        for dj in (-1, 0, 1):
            shifted = pts.assign(ci=pts.ci + di, cj=pts.cj + dj)
            pairs = pts.merge(shifted, on=["key", "ci", "cj"], suffixes=("_a", "_b"))
            pairs = pairs[pairs.row_a < pairs.row_b]
            if pairs.empty:
                continue
            a, b = pairs.row_a.to_numpy(), pairs.row_b.to_numpy()
            dkm = KM_PER_DEG * np.hypot(lat[a] - lat[b], (lon[a] - lon[b]) * coslat[a])
            close = (dkm < tol[a]) & (np.abs(minutes[a] - minutes[b]) <= config.DEDUPE_MINUTES)
            drop[b[close]] = True
    return pd.Series(drop, index=df.index)


def clean(df: pd.DataFrame) -> pd.DataFrame:
    mask = keep_confident(df)
    # NRT rows have no type; keep them (static sources can be masked later with a static-source layer).
    typed = df["type"].notna()
    mask &= ~typed | df["type"].isin(config.KEEP_TYPES)
    out = df[mask]
    # Exact duplicates (same sensor, place, time) come from overlapping files.
    out = out.drop_duplicates(subset=["sensor", "satellite", "latitude", "longitude", "acq_date", "acq_time"])
    out = out[~swath_duplicates(out)]
    return out.reset_index(drop=True)
