"""Harmonization: convert MODIS fire cell-days to VIIRS-equivalent units using the overlap years.

Two methods (config.HARMONIZE_METHOD):

* ``ratio`` (baseline): VIIRS = r_m × MODIS, r_m = median over overlap years of VIIRS÷MODIS in month m.
* ``frp_strata`` (default): VIIRS = a_m × MODIS_weak + b_m × MODIS_strong, least squares per month
  over the overlap years. "Strong" MODIS cell-days have max FRP ≥ STRONG_FRP_MW. VIIRS' 375 m pixels
  add many more detections around weak/small fires than around big intense ones, so the plain ratio
  overestimates big-wildfire years and underestimates quiet, agricultural-burn years.

Both produce a 10–90% band. For ``ratio`` it is the spread of monthly ratios; for ``frp_strata`` it is
the spread of observed÷fitted in the training months.

Next upgrade (docs/plan.md): quantile mapping or gradient boosting with land cover features.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from . import config

RATIO_COLS = ["month", "ratio", "lo", "hi", "weak", "strong", "band_lo", "band_hi"]


def _overlap_totals(monthly: pd.DataFrame, first: int, last: int | None) -> pd.DataFrame:
    """Per (year, month): MODIS, MODIS_strong and VIIRS cell-day totals in the given year range."""
    ov = monthly[(monthly["year"] >= first) & ((monthly["year"] <= last) if last else True)]
    if "strong_cell_days" not in ov:
        ov = ov.assign(strong_cell_days=0)
    tot = ov.pivot_table(index=["year", "month"], columns="family",
                         values=["fire_cell_days", "strong_cell_days"], aggfunc="sum", fill_value=0)
    fams = set(tot.columns.get_level_values(1))
    if "MODIS" not in fams or "VIIRS" not in fams:
        raise ValueError("Need both MODIS and VIIRS data in the overlap years")
    return pd.DataFrame({
        "MODIS": tot[("fire_cell_days", "MODIS")], "MODIS_strong": tot[("strong_cell_days", "MODIS")],
        "VIIRS": tot[("fire_cell_days", "VIIRS")],
    }).reset_index()


def _fit_strata(rows: pd.DataFrame, fallback: float) -> tuple[float, float]:
    """Non-negative least squares for V = a·weak + b·strong (falls back to the ratio when unstable)."""
    weak = (rows.MODIS - rows.MODIS_strong).to_numpy(float)
    strong = rows.MODIS_strong.to_numpy(float)
    v = rows.VIIRS.to_numpy(float)
    if len(rows) >= 3 and weak.sum() > 0 and strong.sum() > 0:
        coef, *_ = np.linalg.lstsq(np.c_[weak, strong], v, rcond=None)
        if (coef > 0).all():
            return float(coef[0]), float(coef[1])
        # One stratum alone: fit a single slope on the other.
    return fallback, fallback


def fit_monthly_ratio(monthly: pd.DataFrame, first_year: int = config.OVERLAP_START_YEAR,
                      last_year: int | None = None) -> pd.DataFrame:
    """Conversion table for ONE region: one row per month 1–12 (schema RATIO_COLS).

    `ratio/lo/hi` are the baseline ratio and its 10–90% spread; `weak/strong` are frp_strata
    coefficients with `band_lo/band_hi` multiplicative 10–90% residual bands. Months without overlap
    data get pooled values so every month is present.
    """
    tot = _overlap_totals(monthly, first_year, last_year)
    tot = tot[tot["MODIS"] > 0]
    if tot.empty:
        raise ValueError("No overlap months with MODIS detections")
    tot = tot.assign(ratio=tot["VIIRS"] / tot["MODIS"])
    pooled = float(tot["VIIRS"].sum() / tot["MODIS"].sum())
    rows = []
    for m in range(1, 13):
        sub = tot[tot.month == m]
        src = sub if len(sub) else tot
        ratio = float(src.ratio.median())
        weak, strong = _fit_strata(sub, ratio) if len(sub) else (pooled, pooled)
        fitted = weak * (src.MODIS - src.MODIS_strong) + strong * src.MODIS_strong
        resid = (src.VIIRS / fitted.replace(0, np.nan)).dropna()
        rows.append({
            "month": m, "ratio": ratio, "lo": float(src.ratio.quantile(0.1)), "hi": float(src.ratio.quantile(0.9)),
            "weak": weak, "strong": strong,
            "band_lo": float(resid.quantile(0.1)) if len(resid) else 1.0,
            "band_hi": float(resid.quantile(0.9)) if len(resid) else 1.0,
        })
    return pd.DataFrame(rows, columns=RATIO_COLS)


def fit_regional_ratios(monthly: pd.DataFrame, regions: dict = config.REGIONS) -> pd.DataFrame:
    """Conversion tables per configured region (cells inside its bbox) plus "global" (all data)."""
    parts = []
    for rid, reg in regions.items():
        w, s, e, n = reg["bbox"]
        sub = monthly[monthly.lon.between(w, e) & monthly.lat.between(s, n)]
        try:
            parts.append(fit_monthly_ratio(sub).assign(region=rid))
        except ValueError:
            continue
    parts.append(fit_monthly_ratio(monthly).assign(region="global"))
    return pd.concat(parts, ignore_index=True)


def region_for_bbox(bbox: tuple[float, float, float, float], regions: dict = config.REGIONS) -> str:
    w, s, e, n = bbox
    cx, cy = (w + e) / 2, (s + n) / 2
    for rid, reg in regions.items():
        rw, rs, re_, rn = reg["bbox"]
        if rw <= cx <= re_ and rs <= cy <= rn:
            return rid
    return "global"


def ratios_for_region(ratios: pd.DataFrame, region: str) -> pd.DataFrame:
    if "region" not in ratios:
        return ratios
    sub = ratios[ratios["region"] == region]
    if sub.empty:
        sub = ratios[ratios["region"] == "global"]
    return sub.drop(columns="region")


def convert_modis(modis: pd.DataFrame, ratios: pd.DataFrame, method: str | None = None) -> pd.DataFrame:
    """modis: rows with month, fire_cell_days[, strong_cell_days]. Adds value/lo/hi (VIIRS-equivalent)."""
    method = method or config.HARMONIZE_METHOD
    m = modis.merge(ratios, on="month", how="left")
    if method == "frp_strata" and "weak" in m and "strong_cell_days" in m:
        strong = m.strong_cell_days.astype(float)
        value = m.weak * (m.fire_cell_days - strong) + m.strong * strong
        return m.assign(value=value, lo=value * m.band_lo, hi=value * m.band_hi)
    return m.assign(value=m.fire_cell_days * m.ratio, lo=m.fire_cell_days * m.lo, hi=m.fire_cell_days * m.hi)


def harmonize(monthly: pd.DataFrame, ratios: pd.DataFrame | None = None,
              method: str | None = None) -> pd.DataFrame:
    """Monthly series in VIIRS-equivalent fire cell-days for the rows in `monthly`.

    Pass `ratios` fitted on a whole region (data/harmonized/ratios.parquet) when `monthly`
    is a small user-drawn area; fitting on a tiny area is too noisy.
    `monthly` needs year, month, family, fire_cell_days[, strong_cell_days] (per-cell rows are summed).
    """
    if ratios is None:
        ratios = fit_monthly_ratio(monthly)
    ratios = ratios[[c for c in RATIO_COLS if c in ratios]]
    vals = [c for c in ("fire_cell_days", "strong_cell_days") if c in monthly]
    tot = monthly.groupby(["year", "month", "family"], as_index=False)[vals].sum()
    viirs = tot[(tot["family"] == "VIIRS") & (tot["year"] >= config.OVERLAP_START_YEAR)]
    viirs = viirs.assign(fire_cell_days=viirs.fire_cell_days.astype(float), lo=lambda d: d.fire_cell_days,
                         hi=lambda d: d.fire_cell_days, source="viirs")
    modis = convert_modis(tot[(tot["family"] == "MODIS") & (tot["year"] < config.OVERLAP_START_YEAR)],
                          ratios, method)
    modis = modis.assign(fire_cell_days=modis.value, source="modis_converted")
    cols = ["year", "month", "fire_cell_days", "lo", "hi", "source"]
    return pd.concat([modis[cols], viirs[cols]]).sort_values(["year", "month"]).reset_index(drop=True)


def _scores(t: pd.DataFrame) -> dict:
    annual = t.groupby("year").agg(predicted=("predicted", "sum"), observed=("observed", "sum"),
                                   n=("month", "size")).reset_index()
    annual = annual[annual.n == 12]
    annual = annual.assign(error_pct=100 * (annual.predicted - annual.observed) / annual.observed.replace(0, np.nan))
    sig = t[t.observed >= 10]  # skip near-empty months where % error is meaningless
    mape = float((100 * (sig.predicted - sig.observed).abs() / sig.observed).mean()) if len(sig) else None
    ss_res = float(((t.predicted - t.observed) ** 2).sum())
    ss_tot = float(((t.observed - t.observed.mean()) ** 2).sum())
    return {
        "annual": annual,
        "mape_monthly_pct": None if mape is None else round(mape, 1),
        "annual_abs_error_pct": round(float(annual.error_pct.abs().mean()), 1) if len(annual) else None,
        "r2": round(1 - ss_res / ss_tot, 3) if ss_tot > 0 else None,
    }


def validate(monthly: pd.DataFrame, train_end: int = config.VALIDATION_TRAIN_END,
             last_full: tuple[int, int] | None = None, method: str | None = None) -> dict:
    """Out-of-sample check: fit on overlap years ≤ train_end, predict VIIRS from MODIS after.

    `last_full` = (year, month) of the last complete month; anything later is excluded.
    Reports the chosen method plus the plain-ratio baseline for comparison.
    """
    method = method or config.HARMONIZE_METHOD
    ratios = fit_monthly_ratio(monthly, last_year=train_end)
    tot = _overlap_totals(monthly, train_end + 1, None)
    if last_full:
        tot = tot[tot.year * 12 + tot.month <= last_full[0] * 12 + last_full[1]]
    tot = tot.rename(columns={"MODIS_strong": "strong_cell_days"}).assign(fire_cell_days=lambda d: d.MODIS)
    main = convert_modis(tot, ratios, method).assign(observed=lambda d: d.VIIRS.astype(float))
    main = main.assign(predicted=main.value, modis_raw=main.MODIS.astype(float)).sort_values(["year", "month"])
    base = convert_modis(tot, ratios, "ratio").assign(observed=lambda d: d.VIIRS.astype(float))
    base = base.assign(predicted=base.value)
    s, b = _scores(main), _scores(base)
    return {
        "method": method,
        "train_years": [config.OVERLAP_START_YEAR, train_end],
        "test_years": [train_end + 1, int(main.year.max()) if len(main) else train_end + 1],
        "monthly": [{"year": int(r.year), "month": int(r.month), "predicted": round(float(r.predicted), 2),
                     "observed": float(r.observed), "modis_raw": float(r.modis_raw)} for r in main.itertuples()],
        "annual": [{"year": int(r.year), "predicted": round(float(r.predicted), 1), "observed": float(r.observed),
                    "error_pct": None if pd.isna(r.error_pct) else round(float(r.error_pct), 1)}
                   for r in s["annual"].itertuples()],
        "mape_monthly_pct": s["mape_monthly_pct"], "annual_abs_error_pct": s["annual_abs_error_pct"], "r2": s["r2"],
        "baseline": {"method": "ratio", "mape_monthly_pct": b["mape_monthly_pct"],
                     "annual_abs_error_pct": b["annual_abs_error_pct"], "r2": b["r2"]},
    }
