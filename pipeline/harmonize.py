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

RATIO_COLS = ["month", "ratio", "lo", "hi", "weak", "strong", "band_lo", "band_hi", "terra_scale", "terra_lo", "terra_hi", "n20_scale", "n20_band_lo", "n20_band_hi"]
QM_COLS = [f"qm_{k}" for k in range(1, config.QM_MAX_K + 1)] + ["qm_band_lo", "qm_band_hi"]


def _overlap_totals(monthly: pd.DataFrame, first: int, last: int | None, obs: pd.DataFrame | None = None) -> pd.DataFrame:
    """Per (year, month): MODIS, MODIS_strong and VIIRS cell-day totals in the given year range, months where
    every satellite observed all days only (an outage would read as missing fire)."""
    ov = monthly[(monthly["year"] >= first) & ((monthly["year"] <= last) if last else True)]
    if "strong_cell_days" not in ov:
        ov = ov.assign(strong_cell_days=0)
    tot = ov.pivot_table(index=["year", "month"], columns="family",
                         values=["fire_cell_days", "strong_cell_days"], aggfunc="sum", fill_value=0)
    fams = set(tot.columns.get_level_values(1))
    if "MODIS" not in fams or "VIIRS" not in fams:
        raise ValueError("Need both MODIS and VIIRS data in the overlap years")
    out = pd.DataFrame({
        "MODIS": tot[("fire_cell_days", "MODIS")], "MODIS_strong": tot[("strong_cell_days", "MODIS")],
        "VIIRS": tot[("fire_cell_days", "VIIRS")],
    }).reset_index()
    # One satellite missing ≤ 3 days barely changes the Terra + Aqua union; the union and S-NPP must be complete.
    return _drop_incomplete(out, obs, {"MODIS": 1.0, "VIIRS": 1.0, "TERRA": 0.9, "AQUA": 0.9})


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


def _fit_quantile(monthly: pd.DataFrame, first_year: int, last_year: int | None,
                  obs: pd.DataFrame | None = None) -> pd.DataFrame | None:
    """QM columns (month + QM_COLS) fit on the overlap years, or None without per-cell data."""
    if "cell_id" not in monthly:
        return None
    ov = monthly[(monthly.year >= first_year) & ((monthly.year <= last_year) if last_year else True)]
    cm = _cell_months(ov)
    cm = _drop_incomplete(cm[cm.fire_cell_days > 0], obs, {"MODIS": 1.0, "VIIRS": 1.0, "TERRA": 0.9, "AQUA": 0.9})
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


def _terra_scale(monthly: pd.DataFrame, obs: pd.DataFrame | None = None) -> dict[int, tuple[float, float, float]]:
    """Per calendar month: (scale, lo, hi). scale = MODIS (Terra + Aqua) cell-days ÷ those Terra saw over
    TERRA_SCALE_YEARS; lo/hi = 10–90% of the yearly values ÷ scale (how much the scale varies by year)."""
    if "terra_cell_days" not in monthly:
        return {}
    y0, y1 = config.TERRA_SCALE_YEARS
    m = monthly[(monthly.family == "MODIS") & monthly.year.between(y0, y1)]
    t = m.groupby(["year", "month"])[["fire_cell_days", "terra_cell_days"]].sum().reset_index()
    t = _drop_incomplete(t[t.terra_cell_days > 0], obs, ("TERRA", "AQUA"))
    if t.empty:
        return {}
    pooled = float(t.fire_cell_days.sum() / t.terra_cell_days.sum())
    out = {}
    for mo in range(1, 13):
        sub = t[t.month == mo]
        sub = sub if len(sub) >= 3 else t
        scale = float(sub.fire_cell_days.sum() / sub.terra_cell_days.sum()) if len(sub) else pooled
        r = sub.fire_cell_days / sub.terra_cell_days / scale
        out[mo] = (scale, float(r.quantile(0.1)), float(r.quantile(0.9)))
    return out


def _noaa20_scale(monthly: pd.DataFrame, obs: pd.DataFrame | None = None) -> pd.DataFrame | None:
    """Per calendar month: S-NPP ÷ NOAA-20 cell-days over the months both covered, with a 10–90% band."""
    t = monthly[monthly.year >= config.NOAA20_BRIDGE_FIRST_YEAR].pivot_table(
        index=["year", "month"], columns="family", values="fire_cell_days", aggfunc="sum", fill_value=0)
    if "NOAA20" not in t or "VIIRS" not in t:
        return None
    t = _drop_incomplete(t[(t.NOAA20 > 0) & (t.VIIRS > 0)].reset_index(), obs, ("VIIRS", "NOAA20"))
    if len(t) < 3:
        return None
    pooled = float(t.VIIRS.sum() / t.NOAA20.sum())
    rows = []
    for m in range(1, 13):
        sub = t[t.month == m]
        scale = float(sub.VIIRS.sum() / sub.NOAA20.sum()) if len(sub) else pooled
        r = (t.VIIRS / (t.NOAA20 * scale))[t.month == m] if len(sub) >= 3 else t.VIIRS / (t.NOAA20 * pooled)
        rows.append({"month": m, "n20_scale": scale, "n20_band_lo": float(r.quantile(0.1)),
                     "n20_band_hi": float(r.quantile(0.9))})
    return pd.DataFrame(rows)


def _frac(obs: pd.DataFrame | None, fam: str, year, month) -> np.ndarray:
    """Share of the month's days on which sensor group `fam` was observing (1.0 when unknown)."""
    key = pd.DataFrame({"year": np.asarray(year, int), "month": np.asarray(month, int)})
    if obs is None or key.empty:
        return np.ones(len(key))
    o = obs.loc[obs.family == fam, ["year", "month", "frac"]]
    # Inside an observation table, a missing entry means the sensor was not delivering (e.g. before launch).
    return key.merge(o, on=["year", "month"], how="left").frac.fillna(0.0).to_numpy(float)


def _drop_incomplete(df: pd.DataFrame, obs: pd.DataFrame | None, fams) -> pd.DataFrame:
    """Rows of (year, month) where the sensor groups observed enough of the month (for fitting and scoring).

    `fams` is a list of groups that must be complete, or a {group: minimum share of days} mapping.
    """
    if obs is None or df.empty:
        return df
    need = fams if isinstance(fams, dict) else {f: 1.0 for f in fams}
    ok = np.ones(len(df), dtype=bool)
    for f, minimum in need.items():
        ok &= _frac(obs, f, df.year, df.month) >= minimum
    return df[ok]


def record_source(year, month, obs: pd.DataFrame | None) -> tuple[np.ndarray, np.ndarray]:
    """Which sensor each month of the record comes from, and the share of days it observed.

    Before OVERLAP_START_YEAR: converted MODIS. After: S-NPP, unless S-NPP missed days (an outage, or after
    it stopped on 2026-11-02) and NOAA-20 observed more of the month, in which case NOAA-20 converted to
    S-NPP units. The caller scales by 1/frac to fill outage days it could not replace.
    """
    year, month = np.asarray(year, int), np.asarray(month, int)
    fv, fn, fm = (_frac(obs, f, year, month) for f in ("VIIRS", "NOAA20", "MODIS"))
    early = year < config.OVERLAP_START_YEAR
    n20 = ~early & (fv < 1) & (fn > fv)
    return (np.select([early, n20], ["modis_converted", "noaa20_converted"], "viirs"),
            np.select([early, n20], [fm, fn], fv))


def _fill(value, frac):
    """Scale a count observed on `frac` of the month's days to the whole month (no-op at 0 or 1)."""
    frac = np.asarray(frac, float)
    return np.where((frac > 0) & (frac < 1), value / np.where(frac > 0, frac, 1.0), value)


def _terra_only(year: pd.Series, month: pd.Series) -> np.ndarray:
    ay, am = config.AQUA_FIRST_MONTH
    return (np.asarray(year) * 12 + np.asarray(month)) < ay * 12 + am


def fit_monthly_ratio(monthly: pd.DataFrame, first_year: int = config.CONVERSION_FIT_YEARS[0],
                      last_year: int | None = config.CONVERSION_FIT_YEARS[1],
                      obs: pd.DataFrame | None = None) -> pd.DataFrame:
    """Conversion table for ONE region: one row per month 1–12 (schema RATIO_COLS).

    `ratio/lo/hi` are the baseline ratio and its 10–90% spread; `weak/strong` are frp_strata
    coefficients with `band_lo/band_hi` multiplicative 10–90% residual bands. Months without overlap
    data get pooled values so every month is present. With per-cell input (a `cell_id` column) the
    quantile-mapping columns QM_COLS are added too.
    """
    tot = _overlap_totals(monthly, first_year, last_year, obs)
    tot = tot[tot["MODIS"] > 0]
    if tot.empty:
        raise ValueError("No overlap months with MODIS detections")
    tot = tot.assign(ratio=tot["VIIRS"] / tot["MODIS"])
    pooled = float(tot["VIIRS"].sum() / tot["MODIS"].sum())
    terra = _terra_scale(monthly, obs)
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
            "terra_scale": terra.get(m, (1.0, 1.0, 1.0))[0],
            "terra_lo": terra.get(m, (1.0, 1.0, 1.0))[1], "terra_hi": terra.get(m, (1.0, 1.0, 1.0))[2],
        })
    out = pd.DataFrame(rows, columns=RATIO_COLS[:11])
    n20 = _noaa20_scale(monthly, obs)
    out = out.merge(n20, on="month", how="left") if n20 is not None else out
    qm = _fit_quantile(monthly, first_year, last_year, obs)
    return out.merge(qm, on="month", how="left") if qm is not None else out


def fit_regional_ratios(monthly: pd.DataFrame, regions: dict = config.REGIONS,
                        obs: pd.DataFrame | None = None) -> pd.DataFrame:
    """Conversion tables per configured region (cells inside its bbox) plus "global" (all data)."""
    parts = []
    for rid, reg in regions.items():
        w, s, e, n = reg["bbox"]
        sub = monthly[monthly.lon.between(w, e) & monthly.lat.between(s, n)]
        try:
            parts.append(fit_monthly_ratio(sub, obs=obs).assign(region=rid))
        except ValueError:
            continue
    parts.append(fit_monthly_ratio(monthly, obs=obs).assign(region="global"))
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
              method: str | None = None, obs: pd.DataFrame | None = None) -> pd.DataFrame:
    """Monthly series in VIIRS-equivalent fire cell-days for the rows in `monthly`.

    Pass `ratios` fitted on a whole region (data/harmonized/ratios.parquet) when `monthly`
    is a small user-drawn area; fitting on a tiny area is too noisy.
    `monthly` needs year, month, family, fire_cell_days[, strong_cell_days] (per-cell rows are summed).
    `obs` (family, year, month, frac): share of days each sensor observed; it picks the source of each month
    (see `record_source`) and fills outage days. Output column `observed` is that share (1 = complete).
    """
    method = method or config.HARMONIZE_METHOD
    if ratios is None:
        ratios = fit_monthly_ratio(monthly, obs=obs)
    ratios = ratios[[c for c in RATIO_COLS + QM_COLS if c in ratios]]
    vals = [c for c in ("fire_cell_days", "strong_cell_days") if c in monthly]
    tot = monthly.groupby(["year", "month", "family"], as_index=False)[vals].sum()
    late = tot[tot["year"] >= config.OVERLAP_START_YEAR]
    src, frac = record_source(late.year, late.month, obs)
    late = late.assign(src=src, observed=frac)
    viirs = late[(late.family == "VIIRS") & (late.src == "viirs")]
    v = _fill(viirs.fire_cell_days.astype(float).to_numpy(), viirs.observed)
    viirs = viirs.assign(fire_cell_days=v, lo=v, hi=v, source="viirs")
    n20 = late[(late.family == "NOAA20") & (late.src == "noaa20_converted")]
    if len(n20) and "n20_scale" in ratios:
        n20 = n20.merge(ratios[["month", "n20_scale", "n20_band_lo", "n20_band_hi"]], on="month", how="left")
        v = _fill((n20.fire_cell_days * n20.n20_scale).to_numpy(), n20.observed)
        viirs = pd.concat([viirs, n20.assign(fire_cell_days=v, lo=v * n20.n20_band_lo, hi=v * n20.n20_band_hi,
                                             source="noaa20_converted")])
    table = _qm_table(ratios) if method == "quantile" else None
    if table is not None and "cell_id" in monthly:
        early = monthly[(monthly.family == "MODIS") & (monthly.year < config.OVERLAP_START_YEAR)]
        cm = _cell_months(early)
        modis = _quantile_totals(cm[cm.fire_cell_days > 0], table).merge(ratios, on="month", how="left")
        modis = modis.assign(lo=modis.value * modis.qm_band_lo, hi=modis.value * modis.qm_band_hi)
    else:  # quantile needs per-cell rows and a QM fit; otherwise use the FRP-stratified fit
        modis = convert_modis(tot[(tot["family"] == "MODIS") & (tot["year"] < config.OVERLAP_START_YEAR)],
                              ratios, "frp_strata" if method == "quantile" else method)
    # Terra-only months (before Aqua): scale up to what Terra + Aqua would have seen.
    if "terra_scale" in modis:
        t = _terra_only(modis.year, modis.month)
        k = np.where(t, modis.terra_scale.fillna(1.0), 1.0)
        klo = k * np.where(t, modis.get("terra_lo", 1.0), 1.0)
        khi = k * np.where(t, modis.get("terra_hi", 1.0), 1.0)
        modis = modis.assign(value=modis.value * k, lo=modis.lo * klo, hi=modis.hi * khi)
    fm = _frac(obs, "MODIS", modis.year, modis.month)
    modis = modis.assign(value=_fill(modis.value.to_numpy(), fm), lo=_fill(modis.lo.to_numpy(), fm),
                         hi=_fill(modis.hi.to_numpy(), fm), observed=fm)
    modis = modis.assign(fire_cell_days=modis.value, source="modis_converted")
    cols = ["year", "month", "fire_cell_days", "lo", "hi", "source", "observed"]
    return pd.concat([modis[cols], viirs[cols]]).sort_values(["year", "month"]).reset_index(drop=True)


def _scores(t: pd.DataFrame) -> dict:
    annual = t.groupby("year").agg(predicted=("predicted", "sum"), observed=("observed", "sum"),
                                   n=("month", "size")).reset_index()
    annual = annual[annual.n >= 10]  # years with ≥ 10 fully observed months, scored over those months
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


def raw_series(monthly: pd.DataFrame, obs: pd.DataFrame | None = None) -> pd.DataFrame:
    """The naive, un-harmonized record: MODIS cell-days before OVERLAP_START_YEAR, VIIRS after.

    This is what you get by stitching the two sensors together; the jump at the handoff is the sensor
    change, not more fire. Columns: year, month, raw, raw_source ("modis"|"viirs").
    """
    if monthly.empty:
        return pd.DataFrame(columns=["year", "month", "raw", "raw_source"])
    tot = monthly.groupby(["year", "month", "family"], as_index=False)["fire_cell_days"].sum()
    early = (tot.family == "MODIS") & (tot.year < config.OVERLAP_START_YEAR)
    src, _ = record_source(tot.year, tot.month, obs)
    late = (tot.family == "VIIRS") & (src == "viirs")
    n20 = (tot.family == "NOAA20") & (src == "noaa20_converted")
    out = tot[early | late | n20].assign(raw=lambda d: d.fire_cell_days.astype(float),
                                         raw_source=lambda d: d.family.map({"MODIS": "modis", "VIIRS": "viirs",
                                                                            "NOAA20": "noaa20"}))
    return out[["year", "month", "raw", "raw_source"]].reset_index(drop=True)


def drift_flag(year: int) -> list[str]:
    """MODIS satellites whose overpass time had drifted by `year`."""
    return [sat for sat, y in config.MODIS_DRIFT_FROM.items() if year >= y]


def _predictions(ratios: pd.DataFrame, test: pd.DataFrame, years: list[int],
                 last_full: tuple[int, int] | None, obs: pd.DataFrame | None = None) -> dict[str, pd.DataFrame]:
    """Predicted vs observed VIIRS per (year, month) in `years`, from the MODIS in `test`, per method."""
    tot = _overlap_totals(test[test.year.isin(years)], min(years), max(years), obs)
    if last_full:
        tot = tot[tot.year * 12 + tot.month <= last_full[0] * 12 + last_full[1]]
    tot = tot.rename(columns={"MODIS_strong": "strong_cell_days"}).assign(fire_cell_days=lambda d: d.MODIS)
    base = tot[["year", "month"]].assign(observed=tot.VIIRS.astype(float).to_numpy(),
                                         modis_raw=tot.MODIS.astype(float).to_numpy())
    out = {}
    for m in ("ratio", "frp_strata"):
        c = convert_modis(tot, ratios, m)
        out[m] = base.assign(predicted=c.value.to_numpy(), lo=c.lo.to_numpy(), hi=c.hi.to_numpy())
    table = _qm_table(ratios)
    if table is not None and "cell_id" in test:
        cm = _cell_months(test[(test.family == "MODIS") & test.year.isin(years)])
        qt = _quantile_totals(cm[cm.fire_cell_days > 0], table).merge(
            ratios[["month", "qm_band_lo", "qm_band_hi"]], on="month")
        qt = qt.assign(predicted=qt.value, lo=qt.value * qt.qm_band_lo, hi=qt.value * qt.qm_band_hi)
        out["quantile"] = base.merge(qt[["year", "month", "predicted", "lo", "hi"]], on=["year", "month"],
                                     how="left").fillna({"predicted": 0.0, "lo": 0.0, "hi": 0.0})
    return out


def _brief(scores: dict, m: str) -> dict:
    return {"method": m, "mape_monthly_pct": scores[m]["mape_monthly_pct"],
            "annual_abs_error_pct": scores[m]["annual_abs_error_pct"], "r2": scores[m]["r2"]}


def _annual_rows(s: dict) -> list[dict]:
    return [{"year": int(r.year), "predicted": round(float(r.predicted), 1), "observed": float(r.observed),
             "error_pct": None if pd.isna(r.error_pct) else round(float(r.error_pct), 1),
             "modis_drift": drift_flag(int(r.year))} for r in s["annual"].itertuples()]


def _checkerboard(cell_id: pd.Series) -> np.ndarray | None:
    """Parity of the grid cell (i + j) % 2, or None when cell ids aren't grid indices."""
    ij = cell_id.astype(str).str.split("_", expand=True)
    if ij.shape[1] != 2:
        return None
    i, j = pd.to_numeric(ij[0], errors="coerce"), pd.to_numeric(ij[1], errors="coerce")
    return None if i.isna().any() or j.isna().any() else ((i + j) % 2).to_numpy(int)


def _validate_noaa20(monthly: pd.DataFrame, obs: pd.DataFrame | None = None) -> dict | None:
    """Leave-one-year-out check of the NOAA-20 → S-NPP factor: each year's S-NPP total (over the months both
    observed completely) predicted from NOAA-20 with the factor fit on the other years."""
    m = monthly[(monthly.year >= config.NOAA20_BRIDGE_FIRST_YEAR) & monthly.family.isin(["VIIRS", "NOAA20"])]
    m = _drop_incomplete(m, obs, ("VIIRS", "NOAA20"))
    t = m.groupby(["year", "family"]).agg(n=("month", "nunique")).reset_index()
    full = sorted(set(t[(t.family == "NOAA20") & (t.n >= 10)].year) & set(t[(t.family == "VIIRS") & (t.n >= 10)].year))
    if len(full) < 3:
        return None
    rows = []
    for y in full:
        sc = _noaa20_scale(monthly[monthly.year != y], obs)
        if sc is None:
            return None
        cur = m[m.year == y]
        tot = cur.groupby(["month", "family"]).fire_cell_days.sum().unstack(fill_value=0)
        if "NOAA20" not in tot or "VIIRS" not in tot:
            continue
        pred = float((tot.NOAA20 * sc.set_index("month").n20_scale.reindex(tot.index)).sum())
        seen = float(tot.VIIRS.sum())
        rows.append({"year": int(y), "predicted": round(pred, 1), "observed": seen,
                     "error_pct": round(100 * (pred - seen) / seen, 1) if seen else None})
    errs = [abs(r["error_pct"]) for r in rows if r["error_pct"] is not None]
    return {"years": [full[0], full[-1]], "annual": rows,
            "annual_abs_error_pct": round(sum(errs) / len(errs), 1) if errs else None}


def loyo_bands(preds: dict[str, pd.DataFrame]) -> dict[str, tuple[float, float]]:
    """Per method: 10–90% of observed ÷ predicted over held-out months (an honest predictive band)."""
    out = {}
    for m, p in preds.items():
        p = p[(p.observed >= 10) & (p.predicted > 0)]
        if len(p) >= 10:
            r = p.observed / p.predicted
            out[m] = (float(r.quantile(0.1)), float(r.quantile(0.9)))
    return out


def calibrate_bands(ratios: pd.DataFrame, bands: dict[str, tuple[float, float]]) -> pd.DataFrame:
    """Replace the in-sample bands in a conversion table with leave-one-year-out ones."""
    r = ratios.copy()
    if "frp_strata" in bands:
        r["band_lo"], r["band_hi"] = bands["frp_strata"]
    if "ratio" in bands:
        r["lo"], r["hi"] = r.ratio * bands["ratio"][0], r.ratio * bands["ratio"][1]
    if "quantile" in bands and "qm_band_lo" in r:
        r["qm_band_lo"], r["qm_band_hi"] = bands["quantile"]
    return r


SMALL_AREA_TILES = (1.0, 0.25)  # degrees; ~10,000 and ~600 km² at mid-latitudes


def _tile_errors(ratios: pd.DataFrame, monthly: pd.DataFrame, year: int, deg: float,
                 obs: pd.DataFrame | None) -> pd.DataFrame:
    """Per (tile, month) of `year`: MODIS-predicted vs observed VIIRS, for tiles of `deg` degrees."""
    m = monthly[monthly.year == year]
    tile = (np.floor(m.lat / deg).astype(int).astype(str) + "_" + np.floor(m.lon / deg).astype(int).astype(str))
    t = m.assign(tile=tile).pivot_table(index=["tile", "year", "month"], columns="family",
                                        values=["fire_cell_days", "strong_cell_days"], aggfunc="sum", fill_value=0)
    if ("fire_cell_days", "MODIS") not in t or ("fire_cell_days", "VIIRS") not in t:
        return pd.DataFrame()
    t = pd.DataFrame({"fire_cell_days": t[("fire_cell_days", "MODIS")],
                      "strong_cell_days": t[("strong_cell_days", "MODIS")],
                      "observed": t[("fire_cell_days", "VIIRS")]}).reset_index()
    t = _drop_incomplete(t, obs, {"MODIS": 1.0, "VIIRS": 1.0, "TERRA": 0.9, "AQUA": 0.9})
    c = convert_modis(t, ratios, "frp_strata")
    return t.assign(predicted=c.value.to_numpy())


def _small_area(tiles: pd.DataFrame) -> dict | None:
    """Out-of-sample error on single tiles, and a band that covers them: multiplicative 10–90% of observed ÷
    predicted, plus an additive allowance (90th percentile of what VIIRS saw in tile-months where MODIS saw
    nothing, which no multiple of zero can cover). `band_coverage_pct` is measured on all active tile-months."""
    if tiles.empty:
        return None
    yr = tiles.groupby(["tile", "year"]).agg(p=("predicted", "sum"), o=("observed", "sum"))
    yr = yr[yr.o >= 50]  # tiles with real fire activity that year
    mo = tiles[(tiles.observed >= 10) & (tiles.predicted > 0)]
    if yr.empty or len(mo) < 20:
        return None
    err = (100 * (yr.p - yr.o) / yr.o).abs()
    r = mo.observed / mo.predicted
    lo, hi = float(r.quantile(0.1)), float(r.quantile(0.9))
    zero = tiles[(tiles.predicted == 0) & (tiles.observed > 0)].observed
    add = float(zero.quantile(0.9)) if len(zero) else 0.0
    act = tiles[(tiles.observed > 0) | (tiles.predicted > 0)]
    inside = (act.observed >= act.predicted * lo) & (act.observed <= act.predicted * hi + add)
    return {"median_annual_abs_error_pct": round(float(err.median()), 1),
            "p90_annual_abs_error_pct": round(float(err.quantile(0.9)), 1),
            "band": [round(lo, 3), round(hi, 3)], "band_add_per_tile": round(add, 1),
            "band_coverage_pct": round(float(inside.mean() * 100), 1),
            "zero_prediction_share_pct": round(float(len(zero) / max(len(act), 1) * 100), 1),
            "n_tile_years": int(len(yr))}


def validate(monthly: pd.DataFrame, last_full: tuple[int, int] | None = None, method: str | None = None,
             bands: dict | None = None, obs: pd.DataFrame | None = None) -> dict:
    """Out-of-sample checks of the conversion that is actually used (fit on CONVERSION_FIT_YEARS).

    * Headline, leave-one-year-out: each fit-window year is predicted from coefficients fit on the others.
      Those years predate the MODIS orbit drift, like the 2000-2011 record the conversion is applied to.
    * ``drift_test``: the deployed coefficients on the overlap years after the window, where Terra and Aqua
      had drifted; shows how much drift biases MODIS (reported, not hidden).
    * ``spatial``: fit on half the grid cells (checkerboard), predict the other half's totals: does the
      conversion transfer to areas it never saw? (Users draw their own areas.)
    * ``band_coverage_pct``: share of held-out months whose observed VIIRS lies inside the 10-90% band.
    """
    method = method or config.HARMONIZE_METHOD
    y0, y1 = config.CONVERSION_FIT_YEARS
    if bands is None:
        bands = {}
    avail = sorted(set(monthly[(monthly.family == "VIIRS") & monthly.year.between(y0, y1)].year))
    if len(avail) < 3:
        raise ValueError("Need at least 3 overlap years in the fit window to validate")
    folds: dict[str, list[pd.DataFrame]] = {}
    tiles: dict[float, list[pd.DataFrame]] = {d: [] for d in SMALL_AREA_TILES}
    for y in avail:
        ratios = fit_monthly_ratio(monthly[monthly.year != y], y0, y1, obs)
        for m, p in _predictions(ratios, monthly, [y], last_full, obs).items():
            folds.setdefault(m, []).append(p)
        if "lat" in monthly:
            for d in SMALL_AREA_TILES:
                tiles[d].append(_tile_errors(ratios, monthly, y, d, obs))
    preds = {m: pd.concat(ps, ignore_index=True) for m, ps in folds.items()}
    if method not in preds:
        method = "frp_strata"
    scores = {m: _scores(p) for m, p in preds.items()}
    main = preds[method].sort_values(["year", "month"])
    bands.update(loyo_bands(preds))

    full = calibrate_bands(fit_monthly_ratio(monthly, y0, y1, obs), bands)
    coverage = None
    later = sorted(set(monthly[(monthly.family == "VIIRS") & (monthly.year > y1)].year))
    drift_test = None
    if later:
        dp = _predictions(full, monthly, later, last_full, obs).get(method)
        if dp is not None and len(dp):
            sig = dp[dp.observed >= 10]
            if len(sig):
                coverage = float(((sig.observed >= sig.lo) & (sig.observed <= sig.hi)).mean() * 100)
            ds = _scores(dp)
            a = ds["annual"]
            drift_test = {"years": [int(min(later)), int(dp.year.max())], "annual": _annual_rows(ds),
                          "annual_abs_error_pct": ds["annual_abs_error_pct"],
                          "mean_bias_pct": round(float(a.error_pct.mean()), 1) if len(a) else None}

    spatial = None
    parity = _checkerboard(monthly["cell_id"]) if "cell_id" in monthly else None
    if parity is not None:
        try:
            sr = fit_monthly_ratio(monthly[parity == 0], y0, y1, obs)
            sp = _predictions(sr, monthly[parity == 1], avail, last_full, obs)
            ss = {m: _scores(p) for m, p in sp.items()}
            spatial = {"description": "fit on half the grid cells (checkerboard), tested on the other half",
                       "methods": [_brief(ss, m) for m in config.HARMONIZE_METHODS if m in ss],
                       **_brief(ss, method if method in ss else "frp_strata")}
        except ValueError:
            spatial = None

    s = scores[method]
    return {
        "method": method,
        "scheme": "leave-one-year-out",
        "noaa20_bridge": _validate_noaa20(monthly, obs),
        "train_years": [y0, y1],
        "test_years": [int(avail[0]), int(avail[-1])],
        "monthly": [{"year": int(r.year), "month": int(r.month), "predicted": round(float(r.predicted), 2),
                     "observed": float(r.observed), "modis_raw": float(r.modis_raw)} for r in main.itertuples()],
        "annual": _annual_rows(s),
        "mape_monthly_pct": s["mape_monthly_pct"], "annual_abs_error_pct": s["annual_abs_error_pct"], "r2": s["r2"],
        "band": [round(x, 3) for x in bands.get(method, (1.0, 1.0))],
        "band_coverage_pct": None if coverage is None else round(coverage, 1),
        "band_coverage_on": "drift-test years (not used to set the band)",
        "baseline": _brief(scores, "ratio"),
        "methods": [_brief(scores, m) for m in config.HARMONIZE_METHODS if m in scores],
        "drift_test": drift_test,
        "small_area": [{"tile_deg": d, **sa} for d in SMALL_AREA_TILES
                       if (sa := _small_area(pd.concat(tiles[d], ignore_index=True) if tiles[d] else pd.DataFrame()))],
        "spatial": spatial,
        "terra_scale": {int(r.month): round(float(r.terra_scale), 3) for r in full.itertuples()},
        "drift_from": dict(config.MODIS_DRIFT_FROM),
    }


def harmonize_cells(monthly: pd.DataFrame, ratios: pd.DataFrame, obs: pd.DataFrame | None = None) -> pd.DataFrame:
    """Per (cell, year, month) VIIRS-equivalent values, using the same sensor choice and conversion as
    `harmonize` (the FRP-stratified fit; quantile mapping only makes sense for area totals). For map layers."""
    cols = ["cell_id", "lat", "lon", "year", "month", "value"]
    src, frac = record_source(monthly.year, monthly.month, obs)
    monthly = monthly.assign(src=src, observed=frac)
    early = monthly[(monthly.family == "MODIS") & (monthly.year < config.OVERLAP_START_YEAR)]
    method = "ratio" if config.HARMONIZE_METHOD == "ratio" else "frp_strata"
    modis = convert_modis(early, ratios[[c for c in RATIO_COLS if c in ratios]], method)
    if "terra_scale" in modis:
        modis = modis.assign(value=modis.value * np.where(_terra_only(modis.year, modis.month),
                                                          modis.terra_scale.fillna(1.0), 1.0))
    modis = modis.assign(value=_fill(modis.value.to_numpy(), modis.observed))
    viirs = monthly[(monthly.family == "VIIRS") & (monthly.src == "viirs")]
    viirs = viirs.assign(value=_fill(viirs.fire_cell_days.astype(float).to_numpy(), viirs.observed))
    n20 = monthly[(monthly.family == "NOAA20") & (monthly.src == "noaa20_converted")]
    if len(n20) and "n20_scale" in ratios:
        n20 = n20.merge(ratios[["month", "n20_scale"]], on="month", how="left")
        n20 = n20.assign(value=_fill((n20.fire_cell_days * n20.n20_scale).to_numpy(), n20.observed))
    else:
        n20 = n20.assign(value=pd.Series(dtype=float))
    return pd.concat([modis[cols], viirs[cols], n20[cols]], ignore_index=True)
