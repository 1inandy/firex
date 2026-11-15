"""Load NASA FIRMS hotspot data: archive CSVs from disk, or recent data from the area API."""
from __future__ import annotations

import io
import os
from pathlib import Path

import pandas as pd
import requests

API = "https://firms.modaps.eosdis.nasa.gov/api/area/csv"


def sensor_label(instrument: str, satellite: str) -> str:
    instrument = str(instrument).upper()
    satellite = str(satellite).upper()
    if instrument == "MODIS":
        return "MODIS"
    if satellite in {"N", "NPP", "SUOMI NPP", "SNPP"}:
        return "VIIRS_SNPP"
    if satellite in {"N20", "1", "NOAA-20", "NOAA20", "J1"}:
        return "VIIRS_NOAA20"
    if satellite in {"N21", "2", "NOAA-21", "NOAA21", "J2"}:
        return "VIIRS_NOAA21"
    return f"VIIRS_{satellite}"


def normalize(df: pd.DataFrame) -> pd.DataFrame:
    """Bring a raw FIRMS frame (MODIS or VIIRS) to a common schema."""
    df = df.copy()
    df["acq_date"] = pd.to_datetime(df["acq_date"]).dt.date
    df["sensor"] = [sensor_label(i, s) for i, s in zip(df["instrument"], df["satellite"])]
    df["confidence"] = df["confidence"].astype(str).str.lower()
    if "type" not in df.columns:  # NRT files have no type column
        df["type"] = pd.NA
    keep = ["latitude", "longitude", "acq_date", "acq_time", "sensor", "satellite",
            "confidence", "frp", "daynight", "type"]
    return df[keep]


def load_archive(path: Path) -> pd.DataFrame:
    """Load every FIRMS CSV under `path` (archive downloads or yearly country files)."""
    files = sorted(Path(path).rglob("*.csv"))
    if not files:
        raise FileNotFoundError(f"No CSV files under {path}")
    return pd.concat([normalize(pd.read_csv(f)) for f in files], ignore_index=True)


def fetch_area(source: str, bbox: tuple[float, float, float, float], days: int = 1,
               date: str | None = None, map_key: str | None = None) -> pd.DataFrame:
    """Recent hotspots from the FIRMS area API. source e.g. 'VIIRS_NOAA20_NRT', 'MODIS_NRT'.

    The API only serves a few days per call and is rate-limited; use archive downloads for history.
    """
    key = map_key or os.environ["FIRMS_MAP_KEY"]
    w, s, e, n = bbox
    url = f"{API}/{key}/{source}/{w},{s},{e},{n}/{days}"
    if date:
        url += f"/{date}"
    r = requests.get(url, timeout=60)
    r.raise_for_status()
    if not r.text.strip() or "Invalid" in r.text[:100]:
        raise RuntimeError(r.text[:200])
    return normalize(pd.read_csv(io.StringIO(r.text)))
