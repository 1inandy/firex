"""Download real FIRMS archive hotspots for the configured regions.

    python -m pipeline.fetch [region ...] [--to 2025]

Reads NASA FIRMS yearly country files (MODIS C6.1 and VIIRS S-NPP 375 m, standard processing),
keeps only detections inside the region's bbox, and writes data/raw/<region>/<product>_<year>.csv.gz.
Files already on disk are skipped, so the command can be re-run after an interruption.
"""
from __future__ import annotations

import argparse
import datetime as dt
import sys

import pandas as pd

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
            df.to_csv(path, index=False)
            print(f"{rid} {product} {year}: {len(df):,} hotspots", flush=True)


def main(argv: list[str]) -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("regions", nargs="*", default=list(config.REGIONS))
    ap.add_argument("--to", type=int, default=dt.date.today().year, help="last year to fetch")
    args = ap.parse_args(argv)
    for rid in args.regions:
        fetch_region(rid, args.to)


if __name__ == "__main__":
    main(sys.argv[1:])
