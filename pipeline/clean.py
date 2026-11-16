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
    """True for detections that repeat a kept one from the same overpass less than a pixel away.

    Neighbouring scan lines overlap toward the swath edge (the "bow-tie" effect), so one fire can be
    reported twice by the same satellite within seconds. Detections with the same satellite, date and
    day/night, at most DEDUPE_MINUTES apart and closer than the sensor's pixel size (DEDUPE_KM), are
    duplicates. Brighter detections (higher FRP) are kept first, and a detection is only dropped against
    one that is kept, so a chain of overlapping pixels never erases a pixel that is a full pixel away.
    """
    if df.empty:
        return pd.Series(False, index=df.index)
    fam = np.where(df["sensor"] == "MODIS", "MODIS", "VIIRS")
    tol = np.where(fam == "MODIS", config.DEDUPE_KM["MODIS"], config.DEDUPE_KM["VIIRS"])
    tol_deg = tol / KM_PER_DEG
    lat = df["latitude"].to_numpy(float)
    lon = df["longitude"].to_numpy(float)
    coslat = np.cos(np.radians(lat))
    minutes = _minutes(df["acq_time"])
    key = pd.factorize(pd.Series(df["sensor"].astype(str).to_numpy()) + "|" + df["satellite"].astype(str).to_numpy()
                       + "|" + df["acq_date"].astype(str).to_numpy() + "|" + df["daynight"].astype(str).to_numpy())[0]
    # Priority: brightest first, then a fixed spatial order, so the result doesn't depend on input order.
    order = np.lexsort((lon, lat, -df["frp"].to_numpy(float), minutes, key))
    rank = np.empty(len(df), dtype=np.int64)
    rank[order] = np.arange(len(df))
    # Hash into cells one tolerance tall and wide enough in longitude at the highest latitude present,
    # so any two points closer than the tolerance land in the same or adjacent cells.
    lon_width = tol_deg / np.cos(np.radians(min(np.abs(lat).max(), 80.0)))
    pts = pd.DataFrame({"row": np.arange(len(df)), "key": key,
                        "ci": np.floor(lat / tol_deg).astype(np.int64),
                        "cj": np.floor(lon / lon_width).astype(np.int64)})
    pairs = []
    for di in (-1, 0, 1):
        for dj in (-1, 0, 1):
            m = pts.merge(pts.assign(ci=pts.ci + di, cj=pts.cj + dj), on=["key", "ci", "cj"], suffixes=("_a", "_b"))
            a, b = m.row_a.to_numpy(), m.row_b.to_numpy()
            keep = rank[a] < rank[b]
            a, b = a[keep], b[keep]
            dkm = KM_PER_DEG * np.hypot(lat[a] - lat[b], (lon[a] - lon[b]) * (coslat[a] + coslat[b]) / 2)
            close = (dkm < tol[a]) & (np.abs(minutes[a] - minutes[b]) <= config.DEDUPE_MINUTES)
            pairs.append(np.c_[a[close], b[close]])
    pairs = np.concatenate(pairs)
    drop = np.zeros(len(df), dtype=bool)
    # Visit pairs by the rank of the later detection: its earlier partner's fate is already decided.
    for a, b in pairs[np.argsort(rank[pairs[:, 1]], kind="stable")]:
        if not drop[a]:
            drop[b] = True
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


STATIC_KEY_DEG = 0.01  # ~1 km: the size of a MODIS pixel


def static_keys(df: pd.DataFrame) -> pd.DataFrame:
    """Grid keys of archive detections typed as volcano, static land source or offshore (types 1–3)."""
    s = df[pd.to_numeric(df["type"], errors="coerce").isin([1, 2, 3])]
    return pd.DataFrame({"ki": np.round(s.latitude / STATIC_KEY_DEG).astype(np.int64),
                         "kj": np.round(s.longitude / STATIC_KEY_DEG).astype(np.int64)}).drop_duplicates()


def mask_static(df: pd.DataFrame, keys: pd.DataFrame) -> pd.DataFrame:
    """Drop untyped (near-real-time) detections within ~1 km of a known static source.

    NRT data has no `type`, so gas flares and industrial heat would count as fires; the archive's typed
    detections tell us where those sources are.
    """
    if df.empty or keys.empty:
        return df
    untyped = df["type"].isna().to_numpy()
    ki = np.round(df.latitude.to_numpy(float) / STATIC_KEY_DEG).astype(np.int64)
    kj = np.round(df.longitude.to_numpy(float) / STATIC_KEY_DEG).astype(np.int64)
    known = set(zip(keys.ki.to_numpy(), keys.kj.to_numpy()))
    near = np.array([any((i + di, j + dj) in known for di in (-1, 0, 1) for dj in (-1, 0, 1))
                     for i, j in zip(ki, kj)], dtype=bool)
    return df[~(untyped & near)]
