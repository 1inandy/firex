"""Filter hotspots down to real vegetation/land fires with acceptable confidence."""
from __future__ import annotations

import pandas as pd

from . import config


def keep_confident(df: pd.DataFrame) -> pd.Series:
    is_modis = df["sensor"] == "MODIS"
    modis_ok = pd.to_numeric(df["confidence"], errors="coerce") >= config.MODIS_MIN_CONFIDENCE
    viirs_ok = df["confidence"].isin(config.VIIRS_KEEP_CONFIDENCE)
    return (is_modis & modis_ok) | (~is_modis & viirs_ok)


def clean(df: pd.DataFrame) -> pd.DataFrame:
    mask = keep_confident(df)
    # NRT rows have no type; keep them (static sources can be masked later with a static-source layer).
    typed = df["type"].notna()
    mask &= ~typed | df["type"].isin(config.KEEP_TYPES)
    out = df[mask]
    # Exact duplicates (same sensor, place, time) come from overlapping scans/files.
    # TODO(job 2): swath-edge "bow-tie" duplicates need a spatial tolerance, not just exact matches.
    return out.drop_duplicates(subset=["sensor", "satellite", "latitude", "longitude", "acq_date", "acq_time"])
