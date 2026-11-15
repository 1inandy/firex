"""Baseline harmonization: learn VIIRS/MODIS ratio per calendar month from the overlap years.

This is the simple starting point (job 4). Upgrade path in docs/plan.md:
quantile mapping, then a model with land cover / FRP / season features.
"""
from __future__ import annotations

import pandas as pd

from . import config


def fit_monthly_ratio(monthly: pd.DataFrame) -> pd.DataFrame:
    """monthly: output of grid.monthly_cell_days for ONE region. Returns ratio per month + spread."""
    ov = monthly[monthly["year"] >= config.OVERLAP_START_YEAR]
    tot = ov.pivot_table(index=["year", "month"], columns="family",
                         values="fire_cell_days", aggfunc="sum", fill_value=0)
    if "MODIS" not in tot or "VIIRS" not in tot:
        raise ValueError("Need both MODIS and VIIRS data in the overlap years")
    tot = tot[tot["MODIS"] > 0]
    tot["ratio"] = tot["VIIRS"] / tot["MODIS"]
    r = tot.reset_index().groupby("month")["ratio"]
    return pd.DataFrame({"ratio": r.median(), "lo": r.quantile(0.1), "hi": r.quantile(0.9)}).reset_index()


def harmonize(monthly: pd.DataFrame, ratios: pd.DataFrame | None = None) -> pd.DataFrame:
    """Monthly series in VIIRS-equivalent fire cell-days for the rows in `monthly`.

    Pass `ratios` fitted on a whole region (data/harmonized/ratios.parquet) when `monthly`
    is a small user-drawn area; fitting on a tiny area is too noisy.
    """
    if ratios is None:
        ratios = fit_monthly_ratio(monthly)
    tot = monthly.groupby(["year", "month", "family"], as_index=False)["fire_cell_days"].sum()
    viirs = tot[tot["family"] == "VIIRS"].assign(lo=lambda d: d.fire_cell_days,
                                                hi=lambda d: d.fire_cell_days, source="viirs")
    modis = tot[(tot["family"] == "MODIS") & (tot["year"] < config.OVERLAP_START_YEAR)].merge(ratios, on="month")
    modis = modis.assign(lo=modis.fire_cell_days * modis.lo, hi=modis.fire_cell_days * modis.hi,
                         fire_cell_days=modis.fire_cell_days * modis.ratio, source="modis_converted")
    cols = ["year", "month", "fire_cell_days", "lo", "hi", "source"]
    return pd.concat([modis[cols], viirs[cols]]).sort_values(["year", "month"]).reset_index(drop=True)
