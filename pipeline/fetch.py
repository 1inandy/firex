"""Download real FIRMS archive hotspots for the configured regions.

    python -m pipeline.fetch [region ...] [--to 2025]     # yearly archive files (no key)
    python -m pipeline.fetch --api [region ...]           # the current year to yesterday, via the area API
                                                          # (needs FIRMS_MAP_KEY)

Reads NASA FIRMS yearly country files (MODIS C6.1 and VIIRS S-NPP 375 m, standard processing),
keeps only detections inside the region's bbox, and writes data/raw/<region>/<product>_<year>.csv.gz.
Files already on disk are skipped, so the command can be re-run after an interruption.
"""
from __future__ import annotations

import argparse
import datetime as dt
import io
import os
import sys

import pandas as pd
import requests

from . import config

COLUMNS = ["latitude", "longitude", "acq_date", "acq_time", "satellite", "instrument",
           "confidence", "frp", "daynight", "type"]


def clip(df: pd.DataFrame, bbox: tuple[float, float, float, float]) -> pd.DataFrame:
    w, s, e, n = bbox
    return df[df.longitude.between(w, e) & df.latitude.between(s, n)]


def fetch_year(product: str, year: int, country: str, bbox) -> pd.DataFrame | None:
    """One product-year clipped to bbox, or None when FIRMS has no file for it yet."""
    url = config.FIRMS_COUNTRY_URL.format(product=product, year=year, country=country)
    try:
        chunks = pd.read_csv(url, usecols=COLUMNS, dtype={"acq_time": str, "confidence": str},
                             chunksize=500_000)
        return pd.concat(clip(c, bbox) for c in chunks)
    except Exception as e:  # urllib raises HTTPError 404 for years not published yet
        if "404" in str(e):
            return None
        raise


def fetch_region(rid: str, last_year: int) -> None:
    reg = config.REGIONS[rid]
    out = config.RAW / rid
    out.mkdir(parents=True, exist_ok=True)
    for product, first in config.FIRMS_COUNTRY_PRODUCTS.items():
        for year in range(first, last_year + 1):
            path = out / f"{product}_{year}.csv.gz"
            if path.exists():
                continue
            df = fetch_year(product, year, reg["country"], reg["bbox"])
            if df is None:
                print(f"{rid} {product} {year}: not published")
                continue
            tmp = path.with_name(path.name + ".part")
            df.to_csv(tmp, index=False, compression="gzip")
            tmp.replace(path)  # atomic: an interrupted download never leaves a truncated file behind
            print(f"{rid} {product} {year}: {len(df):,} hotspots", flush=True)


API = "https://firms.modaps.eosdis.nasa.gov/api"
API_FAMILIES = {"MODIS": ("MODIS_SP", "MODIS_NRT"), "VIIRS_SNPP": ("VIIRS_SNPP_SP", "VIIRS_SNPP_NRT"),
                "VIIRS_NOAA20": ("VIIRS_NOAA20_SP", "VIIRS_NOAA20_NRT")}
API_CHUNK_DAYS = 5  # the area API serves 1–5 days per call


def availability(key: str) -> dict[str, tuple[dt.date, dt.date]]:
    """Date range each FIRMS source currently serves (/api/data_availability)."""
    r = requests.get(f"{API}/data_availability/csv/{key}/ALL", timeout=60)
    r.raise_for_status()
    df = pd.read_csv(io.StringIO(r.text))
    return {row.data_id: (dt.date.fromisoformat(row.min_date), dt.date.fromisoformat(row.max_date))
            for row in df.itertuples()}


def fetch_api_range(source: str, bbox, start: dt.date, end: dt.date, key: str) -> pd.DataFrame:
    """Raw FIRMS rows for [start, end] from the area API, in 5-day calls. Raises if any call fails, so a
    file on disk always covers exactly the dates in its name."""
    w, s, e, n = bbox
    parts, d = [], start
    while d <= end:
        days = min(API_CHUNK_DAYS, (end - d).days + 1)
        r = requests.get(f"{API}/area/csv/{key}/{source}/{w},{s},{e},{n}/{days}/{d.isoformat()}", timeout=120)
        r.raise_for_status()
        if r.text.startswith("Invalid") or "Error" in r.text[:40]:
            raise RuntimeError(f"{source} {d}: {r.text[:200]}")
        parts.append(pd.read_csv(io.StringIO(r.text), dtype={"acq_time": str, "confidence": str}))
        d += dt.timedelta(days=days)
    return pd.concat(parts, ignore_index=True)


def fetch_region_api(rid: str, key: str, until: dt.date) -> None:
    """The current year (from Jan 1) to `until` for each sensor family: standard-processing (SP) data up to
    the date FIRMS has it, near-real-time (NRT) after. Saved as <SOURCE>_<from>_<to>.csv.gz."""
    reg, avail = config.REGIONS[rid], availability(key)
    out = config.RAW / rid
    out.mkdir(parents=True, exist_ok=True)
    start = dt.date(until.year, 1, 1)
    for fam, (sp, nrt) in API_FAMILIES.items():
        sp_end = min(avail[sp][1], until) if sp in avail else start - dt.timedelta(days=1)
        spans = [(sp, start, sp_end)]
        if nrt in avail:
            spans.append((nrt, max(sp_end + dt.timedelta(days=1), avail[nrt][0]), min(avail[nrt][1], until)))
        for source, a, b in spans:
            if b < a:
                continue
            path = out / f"{source}_{a}_{b}.csv.gz"
            if path.exists():
                continue
            for old in out.glob(f"{source}_{a}_*.csv.gz"):  # an earlier, shorter run of the same span
                old.unlink()
            df = fetch_api_range(source, reg["bbox"], a, b, key)
            tmp = path.with_name(path.name + ".part")
            df.to_csv(tmp, index=False, compression="gzip")
            tmp.replace(path)
            print(f"{rid} {source} {a}..{b}: {len(df):,} hotspots", flush=True)


def main(argv: list[str]) -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("regions", nargs="*", default=list(config.REGIONS))
    ap.add_argument("--to", type=int, default=dt.date.today().year, help="last year to fetch")
    ap.add_argument("--api", action="store_true", help="fetch this year to yesterday via the area API")
    args = ap.parse_args(argv)
    if args.api:
        from dotenv import load_dotenv
        load_dotenv()
        key = os.environ.get("FIRMS_MAP_KEY")
        if not key or key == "your_map_key_here":
            raise SystemExit("Set FIRMS_MAP_KEY in .env (free: https://firms.modaps.eosdis.nasa.gov/api/map_key/)")
        until = dt.date.today() - dt.timedelta(days=1)
        for rid in args.regions:
            fetch_region_api(rid, key, until)
        return
    for rid in args.regions:
        fetch_region(rid, args.to)


if __name__ == "__main__":
    main(sys.argv[1:])
