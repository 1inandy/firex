"""Harmonization: convert MODIS fire cell-days to VIIRS-equivalent units using the overlap years.

Two methods (config.HARMONIZE_METHOD):

* ``ratio`` (baseline): VIIRS = r_m × MODIS, r_m = median over overlap years of VIIRS÷MODIS in month m.
* ``frp_strata`` (default): VIIRS = a_m × MODIS_weak + b_m × MODIS_strong, least squares per month
  over the overlap years. "Strong" MODIS cell-days have max FRP ≥ STRONG_FRP_MW. VIIRS' 375 m pixels
  add many more detections around weak/small fires than around big intense ones, so the plain ratio
  overestimates big-wildfire years and underestimates quiet, agricultural-burn years.

* ``quantile``: empirical quantile mapping per calendar month, done per grid cell. For each month,
  the non-zero monthly cell-day counts k of MODIS cells and of VIIRS cells in the overlap years form two
  distributions; a MODIS count k maps to the VIIRS count at the same quantile, g(k) = Q_V(F_M(k)). VIIRS
  also lights up more cells than MODIS, so the mapped value is scaled by c = n_VIIRS / n_MODIS active
  cell-months. An area's converted total is Σ_cells c·g(k_cell). Because the curve is per cell, a curve
  fit on a whole region applies to any drawn area. The table is stored in ratios.parquet as columns
  qm_1…qm_31 (= c·g(k)) plus qm_band_lo/qm_band_hi.

All produce a 10–90% band. For ``ratio`` it is the spread of monthly ratios; for ``frp_strata`` and
``quantile`` it is the spread of observed÷fitted in the training months.

Next upgrade (docs/plan.md): gradient boosting with land cover features.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from . import config

RATIO_COLS = ["month", "ratio", "lo", "hi", "weak", "strong", "band_lo", "band_hi"]
QM_COLS = [f"qm_{k}" for k in range(1, config.QM_MAX_K + 1)] + ["qm_band_lo", "qm_band_hi"]


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


def _cell_months(monthly: pd.DataFrame) -> pd.DataFrame:
    """One row per (cell, year, month, family) with that cell's fire cell-days."""
    return monthly.groupby(["cell_id", "year", "month", "family"], as_index=False)["fire_cell_days"].sum()


def _qm_curve(modis_k: np.ndarray, viirs_k: np.ndarray) -> np.ndarray:
    """c·Q_V(F_M(k)) for k = 1…QM_MAX_K (mid-rank empirical CDF; k beyond the MODIS max maps to the VIIRS max)."""
    m, v = np.sort(modis_k.astype(float)), np.sort(viirs_k.astype(float))
    ks = np.arange(1, config.QM_MAX_K + 1)
    p = (np.searchsorted(m, ks, "left") + np.searchsorted(m, ks, "right")) / (2 * len(m))
    return np.quantile(v, np.clip(p, 0, 1)) * len(v) / len(m)


def _qm_table(ratios: pd.DataFrame) -> np.ndarray | None:
    """12 × QM_MAX_K lookup (row = month-1, column = k-1), or None when the ratios carry no QM fit."""
    cols = QM_COLS[:-2]
    if not set(cols) <= set(ratios.columns):
        return None
    t = ratios.set_index("month").reindex(range(1, 13))[cols].to_numpy(float)
    return None if np.isnan(t).any() else t


def _quantile_totals(cell_months: pd.DataFrame, table: np.ndarray) -> pd.DataFrame:
    """Per (year, month) VIIRS-equivalent totals from per-cell MODIS counts."""
    k = np.clip(cell_months.fire_cell_days.to_numpy(int), 1, config.QM_MAX_K)
    mapped = table[cell_months.month.to_numpy(int) - 1, k - 1]
    return (cell_months.assign(value=mapped).groupby(["year", "month"], as_index=False)["value"].sum())


def _fit_quantile(monthly: pd.DataFrame, first_year: int, last_year: int | None) -> pd.DataFrame | None:
    """QM columns (month + QM_COLS) fit on the overlap years, or None without per-cell data."""
    if "cell_id" not in monthly:
        return None
    ov = monthly[(monthly.year >= first_year) & ((monthly.year <= last_year) if last_year else True)]
    cm = _cell_months(ov)
    cm = cm[cm.fire_cell_days > 0]
    mod, vii = cm[cm.family == "MODIS"], cm[cm.family == "VIIRS"]
    if len(mod) < config.QM_MIN_SAMPLES or len(vii) < config.QM_MIN_SAMPLES:
        return None
    pooled = _qm_curve(mod.fire_cell_days.to_numpy(), vii.fire_cell_days.to_numpy())
    curves = []
    for m in range(1, 13):
        mm, vv = mod[mod.month == m].fire_cell_days.to_numpy(), vii[vii.month == m].fire_cell_days.to_numpy()
        ok = len(mm) >= config.QM_MIN_SAMPLES and len(vv) >= config.QM_MIN_SAMPLES
        curves.append(_qm_curve(mm, vv) if ok else pooled)
    table = np.vstack(curves)
    # Residual band: observed VIIRS ÷ QM-predicted totals in the training months.
    pred = _quantile_totals(mod, table)
    obs = vii.groupby(["year", "month"], as_index=False)["fire_cell_days"].sum()
    j = pred.merge(obs, on=["year", "month"], how="left").fillna({"fire_cell_days": 0})
    j = j[j.value > 0].assign(r=lambda d: d.fire_cell_days / d.value)
    rows = []
    for m in range(1, 13):
        r = j[j.month == m].r
        r = r if len(r) >= 3 else j.r
        rows.append({"month": m, **{f"qm_{k + 1}": float(table[m - 1, k]) for k in range(config.QM_MAX_K)},
                     "qm_band_lo": float(r.quantile(0.1)) if len(r) else 1.0,
                     "qm_band_hi": float(r.quantile(0.9)) if len(r) else 1.0})
    return pd.DataFrame(rows)


def fit_monthly_ratio(monthly: pd.DataFrame, first_year: int = config.OVERLAP_START_YEAR,
                      last_year: int | None = None) -> pd.DataFrame:
    """Conversion table for ONE region: one row per month 1–12 (schema RATIO_COLS).

    `ratio/lo/hi` are the baseline ratio and its 10–90% spread; `weak/strong` are frp_strata
    coefficients with `band_lo/band_hi` multiplicative 10–90% residual bands. Months without overlap
    data get pooled values so every month is present. With per-cell input (a `cell_id` column) the
    quantile-mapping columns QM_COLS are added too.
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
    out = pd.DataFrame(rows, columns=RATIO_COLS)
    qm = _fit_quantile(monthly, first_year, last_year)
    return out.merge(qm, on="month", how="left") if qm is not None else out


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
    method = method or config.HARMONIZE_METHOD
    if ratios is None:
        ratios = fit_monthly_ratio(monthly)
    ratios = ratios[[c for c in RATIO_COLS + QM_COLS if c in ratios]]
    vals = [c for c in ("fire_cell_days", "strong_cell_days") if c in monthly]
    tot = monthly.groupby(["year", "month", "family"], as_index=False)[vals].sum()
    viirs = tot[(tot["family"] == "VIIRS") & (tot["year"] >= config.OVERLAP_START_YEAR)]
    viirs = viirs.assign(fire_cell_days=viirs.fire_cell_days.astype(float), lo=lambda d: d.fire_cell_days,
                         hi=lambda d: d.fire_cell_days, source="viirs")
    table = _qm_table(ratios) if method == "quantile" else None
    if table is not None and "cell_id" in monthly:
        early = monthly[(monthly.family == "MODIS") & (monthly.year < config.OVERLAP_START_YEAR)]
        cm = _cell_months(early)
        modis = _quantile_totals(cm[cm.fire_cell_days > 0], table).merge(ratios, on="month", how="left")
        modis = modis.assign(lo=modis.value * modis.qm_band_lo, hi=modis.value * modis.qm_band_hi)
    else:  # quantile needs per-cell rows and a QM fit; otherwise use the FRP-stratified fit
        modis = convert_modis(tot[(tot["family"] == "MODIS") & (tot["year"] < config.OVERLAP_START_YEAR)],
                              ratios, "frp_strata" if method == "quantile" else method)
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


def raw_series(monthly: pd.DataFrame) -> pd.DataFrame:
    """The naive, un-harmonized record: MODIS cell-days before OVERLAP_START_YEAR, VIIRS after.

    This is what you get by stitching the two sensors together; the jump at the handoff is the sensor
    change, not more fire. Columns: year, month, raw, raw_source ("modis"|"viirs").
    """
    if monthly.empty:
        return pd.DataFrame(columns=["year", "month", "raw", "raw_source"])
    tot = monthly.groupby(["year", "month", "family"], as_index=False)["fire_cell_days"].sum()
    early = (tot.family == "MODIS") & (tot.year < config.OVERLAP_START_YEAR)
    late = (tot.family == "VIIRS") & (tot.year >= config.OVERLAP_START_YEAR)
    out = tot[early | late].assign(raw=lambda d: d.fire_cell_days.astype(float),
                                   raw_source=lambda d: np.where(d.family == "MODIS", "modis", "viirs"))
    return out[["year", "month", "raw", "raw_source"]].reset_index(drop=True)


def drift_flag(year: int) -> list[str]:
    """MODIS satellites whose overpass time had drifted by `year`."""
    return [sat for sat, y in config.MODIS_DRIFT_FROM.items() if year >= y]


def validate(monthly: pd.DataFrame, train_end: int = config.VALIDATION_TRAIN_END,
             last_full: tuple[int, int] | None = None, method: str | None = None) -> dict:
    """Out-of-sample check: fit on overlap years ≤ train_end, predict VIIRS from MODIS after.

    `last_full` = (year, month) of the last complete month; anything later is excluded.
    Reports the chosen method, the plain-ratio baseline, and every method that could be fit (`methods`).
    """
    method = method or config.HARMONIZE_METHOD
    ratios = fit_monthly_ratio(monthly, last_year=train_end)
    tot = _overlap_totals(monthly, train_end + 1, None)
    if last_full:
        tot = tot[tot.year * 12 + tot.month <= last_full[0] * 12 + last_full[1]]
    tot = tot.rename(columns={"MODIS_strong": "strong_cell_days"}).assign(fire_cell_days=lambda d: d.MODIS)
    base = tot[["year", "month", "MODIS", "VIIRS"]].assign(observed=lambda d: d.VIIRS.astype(float),
                                                         modis_raw=lambda d: d.MODIS.astype(float))
    preds = {m: base.assign(predicted=convert_modis(tot, ratios, m).value.to_numpy())
             for m in ("ratio", "frp_strata")}
    table = _qm_table(ratios)
    if table is not None and "cell_id" in monthly:
        cm = _cell_months(monthly[(monthly.family == "MODIS") & (monthly.year > train_end)])
        qt = _quantile_totals(cm[cm.fire_cell_days > 0], table).rename(columns={"value": "predicted"})
        preds["quantile"] = base.merge(qt, on=["year", "month"], how="left").fillna({"predicted": 0.0})
    if method not in preds:
        method = "frp_strata"
    scores = {m: _scores(p) for m, p in preds.items()}
    main = preds[method].sort_values(["year", "month"])
    s, b = scores[method], scores["ratio"]

    def brief(m: str) -> dict:
        return {"method": m, "mape_monthly_pct": scores[m]["mape_monthly_pct"],
                "annual_abs_error_pct": scores[m]["annual_abs_error_pct"], "r2": scores[m]["r2"]}

    return {
        "method": method,
        "train_years": [config.OVERLAP_START_YEAR, train_end],
        "test_years": [train_end + 1, int(main.year.max()) if len(main) else train_end + 1],
        "monthly": [{"year": int(r.year), "month": int(r.month), "predicted": round(float(r.predicted), 2),
                     "observed": float(r.observed), "modis_raw": float(r.modis_raw)} for r in main.itertuples()],
        "annual": [{"year": int(r.year), "predicted": round(float(r.predicted), 1), "observed": float(r.observed),
                    "error_pct": None if pd.isna(r.error_pct) else round(float(r.error_pct), 1),
                    "modis_drift": drift_flag(int(r.year))}
                   for r in s["annual"].itertuples()],
        "mape_monthly_pct": s["mape_monthly_pct"], "annual_abs_error_pct": s["annual_abs_error_pct"], "r2": s["r2"],
        "baseline": brief("ratio"),
        "methods": [brief(m) for m in config.HARMONIZE_METHODS if m in scores],
        "drift_from": dict(config.MODIS_DRIFT_FROM),
    }
