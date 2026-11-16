import datetime as dt

import numpy as np
import pandas as pd
import pytest

from pipeline import config
from pipeline.clean import clean, swath_duplicates
from pipeline.events import build_events
from pipeline.grid import monthly_cell_days, to_grid
from pipeline.harmonize import (drift_flag, fit_monthly_ratio, fit_regional_ratios, harmonize, raw_series,
                                region_for_bbox, validate)


def synthetic(seed=0):
    """MODIS 2005-2015, VIIRS 2012-2015 seeing ~3x as many fire cells."""
    rng = np.random.default_rng(seed)
    rows = []
    for year in range(2005, 2016):
        for month in (1, 7):
            day = dt.date(year, month, 10)
            for k in range(10):  # 10 MODIS fire cells
                rows.append(("MODIS", "Aqua", "80", 34.0 + k * 0.1, -119.0, day, 0))
            if year >= 2012:
                for k in range(30):  # 30 VIIRS fire cells
                    rows.append(("VIIRS_SNPP", "N", "n", 34.0 + k * 0.1, -118.0, day, 0))
    rows.append(("MODIS", "Aqua", "10", 30.0, -110.0, dt.date(2005, 1, 10), 0))   # low confidence
    rows.append(("VIIRS_SNPP", "N", "n", 29.0, -95.0, dt.date(2013, 1, 10), 2))   # gas flare
    df = pd.DataFrame(rows, columns=["sensor", "satellite", "confidence", "latitude", "longitude",
                                     "acq_date", "type"])
    return df.assign(acq_time=1330, frp=rng.uniform(1, 50, len(df)), daynight="D")


def test_clean_drops_low_confidence_and_static_sources():
    out = clean(synthetic())
    assert (out.latitude != 30.0).all()
    assert (out.latitude != 29.0).all()


def test_swath_duplicates_only_within_one_overpass_and_pixel():
    base = dict(sensor="VIIRS_SNPP", satellite="N", acq_date=dt.date(2020, 1, 1), daynight="D", frp=5.0,
                confidence="n", type=0)
    df = pd.DataFrame([
        {**base, "latitude": 10.0, "longitude": 20.0, "acq_time": 1330},
        {**base, "latitude": 10.002, "longitude": 20.0, "acq_time": 1330},   # ~220 m away: duplicate
        {**base, "latitude": 10.01, "longitude": 20.0, "acq_time": 1330},    # ~1.1 km: separate fire
        {**base, "latitude": 10.002, "longitude": 20.0, "acq_time": 1500},   # other overpass
        {**base, "latitude": 10.002, "longitude": 20.0, "acq_time": 1330, "satellite": "N20",
         "sensor": "VIIRS_NOAA20"},                                          # other satellite
    ])
    assert swath_duplicates(df).tolist() == [False, True, False, False, False]
    assert len(clean(df)) == 4


def test_swath_duplicates_found_far_from_the_prime_meridian():
    # 0.89 km apart on the same overpass at lon -120: one is a duplicate.
    base = dict(sensor="MODIS", satellite="Aqua", acq_date=dt.date(2020, 1, 1), daynight="D", frp=5.0,
                acq_time=1330, longitude=-120.0)
    df = pd.DataFrame([{**base, "latitude": 40.0}, {**base, "latitude": 40.008}])
    assert swath_duplicates(df).sum() == 1


def test_swath_duplicates_only_suppressed_by_kept_detections():
    # A chain 0.67 km apart: the middle repeats the first, but the last (1.34 km from the first) is a
    # separate pixel once the middle one is dropped. The brighter detection represents each duplicate pair.
    base = dict(sensor="MODIS", satellite="Aqua", acq_date=dt.date(2020, 1, 1), daynight="D",
                acq_time=1330, longitude=20.0)
    df = pd.DataFrame([{**base, "latitude": 10.0, "frp": 9.0}, {**base, "latitude": 10.006, "frp": 4.0},
                       {**base, "latitude": 10.012, "frp": 7.0}])
    assert swath_duplicates(df).tolist() == [False, True, False]
    assert swath_duplicates(df.iloc[::-1]).tolist() == [False, True, False]


def test_ratio_and_harmonized_series():
    monthly = monthly_cell_days(to_grid(clean(synthetic())))
    ratios = fit_monthly_ratio(monthly)
    assert len(ratios) == 12
    assert ratios.set_index("month").loc[[1, 7], "ratio"].tolist() == pytest.approx([3.0, 3.0])
    for method in ("ratio", "frp_strata"):
        h = harmonize(monthly, method=method)
        early = h[h.year == 2005]
        assert set(early.source) == {"modis_converted"}
        assert early.fire_cell_days.tolist() == pytest.approx([30.0, 30.0])
        assert set(h[h.year >= 2012].source) == {"viirs"}


def test_frp_strata_follows_fire_mix():
    """VIIRS adds 3 cells per weak MODIS cell but only 1 per strong one; the plain ratio can't capture that."""
    rows = []
    rng = np.random.default_rng(1)
    for year in range(2012, 2024):
        weak, strong = int(rng.integers(5, 40)), int(rng.integers(5, 40))
        rows += [dict(cell_id="a", lat=0, lon=0, year=year, month=8, family="MODIS",
                      fire_cell_days=weak + strong, strong_cell_days=strong),
                 dict(cell_id="a", lat=0, lon=0, year=year, month=8, family="VIIRS",
                      fire_cell_days=3 * weak + strong, strong_cell_days=0)]
    monthly = pd.DataFrame(rows)
    r = fit_monthly_ratio(monthly).set_index("month").loc[8]
    assert (r.weak, r.strong) == pytest.approx((3.0, 1.0))
    v = validate(monthly)
    assert v["annual_abs_error_pct"] is None  # only one month per year: no complete years
    assert v["mape_monthly_pct"] == pytest.approx(0.0, abs=1e-6)
    assert v["baseline"]["mape_monthly_pct"] > 5


def test_regional_ratios_and_lookup():
    monthly = monthly_cell_days(to_grid(clean(synthetic())))
    regions = {"socal": {"name": "x", "bbox": (-120, 33, -117, 38)}}
    r = fit_regional_ratios(monthly, regions)
    assert set(r.region) == {"socal", "global"}
    assert region_for_bbox((-119, 34, -118, 35), regions) == "socal"
    assert region_for_bbox((0, 0, 1, 1), regions) == "global"


def _qm_monthly(seed=2, years=range(2008, 2024), n=60, extra=30):
    """Per-cell months: each MODIS cell with k fire days is seen by VIIRS on 2k days, plus `extra`
    cells per month that only VIIRS sees (1 day each)."""
    rng = np.random.default_rng(seed)
    rows = []
    for year in years:
        ks = rng.integers(1, 9, n)
        for i, k in enumerate(ks):
            cell = dict(cell_id=f"c{year}_{i}", lat=0.0, lon=i * 0.05, year=year, month=8, strong_cell_days=0)
            rows.append({**cell, "family": "MODIS", "fire_cell_days": int(k)})
            if year >= config.OVERLAP_START_YEAR:
                rows.append({**cell, "family": "VIIRS", "fire_cell_days": int(2 * k)})
        if year >= config.OVERLAP_START_YEAR:
            rows += [dict(cell_id=f"x{year}_{j}", lat=1.0, lon=j * 0.05, year=year, month=8, family="VIIRS",
                          fire_cell_days=1, strong_cell_days=0) for j in range(extra)]
    return pd.DataFrame(rows)


def test_quantile_mapping_is_per_cell_and_transfers_to_small_areas():
    monthly = _qm_monthly()
    r = fit_monthly_ratio(monthly).set_index("month")
    curve = r.loc[8, [f"qm_{k}" for k in range(1, 9)]].to_numpy(float)
    assert (np.diff(curve) >= 0).all()  # monotone
    v = validate(monthly, method="quantile")
    assert v["method"] == "quantile" and {m["method"] for m in v["methods"]} == {"ratio", "frp_strata", "quantile"}
    assert v["mape_monthly_pct"] < 10
    # A curve fit on the whole region, applied to a 10-cell "drawn area" in a MODIS-only year:
    small = monthly[(monthly.year == 2009) & monthly.cell_id.isin([f"c2009_{i}" for i in range(10)])]
    h = harmonize(small, fit_monthly_ratio(monthly), method="quantile")
    truth = 2 * small.fire_cell_days.sum() * 1.5 * 0.85  # VIIRS 2k, ×1.5 more active cells, minus the 1-day extras
    assert h.fire_cell_days.iloc[0] == pytest.approx(truth, rel=0.35)
    assert h.fire_cell_days.iloc[0] < 0.5 * harmonize(monthly[monthly.year == 2009], fit_monthly_ratio(monthly),
                                                      method="quantile").fire_cell_days.iloc[0]


def test_quantile_falls_back_without_cells():
    monthly = monthly_cell_days(to_grid(clean(synthetic())))
    agg = monthly.drop(columns="cell_id")
    h = harmonize(agg, fit_monthly_ratio(monthly), method="quantile")  # no cell_id: FRP fit instead
    assert h[h.year == 2005].fire_cell_days.tolist() == pytest.approx([30.0, 30.0])


def test_raw_series_stitches_sensors_and_drift_flag():
    monthly = monthly_cell_days(to_grid(clean(synthetic())))
    raw = raw_series(monthly).set_index(["year", "month"])
    assert raw.loc[(2005, 1)].tolist() == [10.0, "modis"]
    assert raw.loc[(2013, 7)].tolist() == [30.0, "viirs"]
    assert drift_flag(2019) == [] and drift_flag(2021) == ["Terra"] and drift_flag(2023) == ["Terra", "Aqua"]


def test_events_link_adjacent_cells_across_days():
    d0 = dt.date(2020, 8, 1)
    pts = []
    # Fire A spreads east one cell per day for 5 days; fire B is far away; fire C reignites after a long gap.
    for k in range(5):
        pts.append((40.0, -120.0 + k * config.GRID_DEG, d0 + dt.timedelta(days=k)))
    pts += [(35.0, -118.0, d0), (35.0, -118.0, d0 + dt.timedelta(days=1)), (35.0, -118.0, d0 + dt.timedelta(days=2))]
    pts += [(35.0, -118.0, d0 + dt.timedelta(days=20 + k)) for k in range(3)]
    df = pd.DataFrame(pts, columns=["latitude", "longitude", "acq_date"]).assign(
        sensor="VIIRS_SNPP", frp=10.0)
    df["latitude"] += 0.001  # stay inside the cell
    df["longitude"] += 0.001
    ev = build_events(to_grid(df))
    assert sorted(ev.cell_days.tolist()) == [3, 3, 5]
    a = ev[ev.cell_days == 5].iloc[0]
    assert a.duration_days == 5 and a.start == d0


def test_fetch_clip_keeps_only_region_and_reads_archive_schema(tmp_path):
    from pipeline.fetch import COLUMNS, clip
    from pipeline.firms import load_archive
    df = pd.DataFrame({"latitude": [-30.0, -40.0, -23.0], "longitude": [-60.0, -60.0, -70.0],
                       "acq_date": ["2020-01-01"] * 3, "acq_time": ["0425", "0005", "1330"],
                       "satellite": ["N", "Terra", "Aqua"], "instrument": ["VIIRS", "MODIS", "MODIS"],
                       "confidence": ["n", "64", "80"], "frp": [1.0, 2.0, 3.0], "daynight": ["N", "N", "D"],
                       "type": [0, 0, 2]})[COLUMNS]
    kept = clip(df, config.REGIONS["chaco"]["bbox"])
    assert kept.latitude.tolist() == [-30.0]
    kept.to_csv(tmp_path / "viirs-snpp_2020.csv.gz", index=False)
    loaded = load_archive(tmp_path)
    assert loaded.sensor.tolist() == ["VIIRS_SNPP"] and loaded.confidence.tolist() == ["n"]


def test_terra_only_months_scaled_to_terra_plus_aqua():
    """Before Aqua (2002-07) MODIS is Terra alone; Terra+Aqua see 1.5x Terra's cell-days, so 2001 is scaled."""
    rows = []
    for year in range(2001, 2020):
        both = year >= 2003
        modis = 15 if both else 10
        rows.append(dict(cell_id="0_0", lat=0, lon=0, year=year, month=8, family="MODIS",
                         fire_cell_days=modis, strong_cell_days=0, terra_cell_days=10))
        if year >= 2012:
            rows.append(dict(cell_id="0_0", lat=0, lon=0, year=year, month=8, family="VIIRS",
                             fire_cell_days=30, strong_cell_days=0, terra_cell_days=0))
    monthly = pd.DataFrame(rows)
    r = fit_monthly_ratio(monthly).set_index("month").loc[8]
    assert r.terra_scale == pytest.approx(1.5) and r.ratio == pytest.approx(2.0)
    h = harmonize(monthly, method="ratio").set_index("year").fire_cell_days
    assert h[2001] == pytest.approx(30.0) and h[2005] == pytest.approx(30.0)  # 10 Terra-only -> 15 -> x2


def test_noaa20_bridges_the_snpp_shutdown():
    """S-NPP sees 1.2x NOAA-20's cell-days; after S-NPP coverage ends, NOAA-20 is scaled into S-NPP units."""
    rows = []
    for year in range(2012, 2027):
        for month in (2, 8):
            rows.append(dict(cell_id="0_0", lat=0, lon=0, year=year, month=month, family="MODIS",
                             fire_cell_days=10, strong_cell_days=0))
            if year <= 2025:
                rows.append(dict(cell_id="0_0", lat=0, lon=0, year=year, month=month, family="VIIRS",
                                 fire_cell_days=24, strong_cell_days=0))
            if year >= 2018:
                rows.append(dict(cell_id="0_0", lat=0, lon=0, year=year, month=month, family="NOAA20",
                                 fire_cell_days=20, strong_cell_days=0))
    monthly = pd.DataFrame(rows)
    r = fit_monthly_ratio(monthly).set_index("month")
    assert r.loc[8, "n20_scale"] == pytest.approx(1.2)
    obs = pd.DataFrame([{"family": f, "year": y, "month": m,
                         "frac": float(f != "VIIRS" or y <= 2025) * float(f != "NOAA20" or y >= 2018)}
                        for f in ("MODIS", "TERRA", "AQUA", "VIIRS", "NOAA20")
                        for y in range(2012, 2027) for m in (2, 8)])
    h = harmonize(monthly, obs=obs).set_index(["year", "month"])
    assert h.loc[(2026, 8), "source"] == "noaa20_converted"
    assert h.loc[(2026, 8), "fire_cell_days"] == pytest.approx(24.0)
    assert h.loc[(2025, 8), "source"] == "viirs" and len(h.loc[(2025, 8)].shape) == 1  # no double count
    assert set(raw_series(monthly, obs).query("year == 2026").raw_source) == {"noaa20"}


def test_outages_are_excluded_from_fits_and_filled_in_the_record():
    """A month where S-NPP missed days must not bias the NOAA-20 factor, and must not read as less fire."""
    rows, obs = [], []
    for year in range(2014, 2026):
        for month in (2, 11):
            truncated = (year, month) == (2024, 11)  # S-NPP delivered only a few days
            rows += [dict(cell_id="0_0", lat=0, lon=0, year=year, month=month, family="MODIS", fire_cell_days=50,
                          strong_cell_days=0),
                     dict(cell_id="0_0", lat=0, lon=0, year=year, month=month, family="VIIRS",
                          fire_cell_days=2 if truncated else 100, strong_cell_days=0)]
            if year >= 2018:
                rows.append(dict(cell_id="0_0", lat=0, lon=0, year=year, month=month, family="NOAA20",
                                 fire_cell_days=100, strong_cell_days=0))
            for f in ("MODIS", "TERRA", "AQUA", "VIIRS", "NOAA20"):
                frac = 0.1 if (f == "VIIRS" and truncated) else (0.0 if f == "NOAA20" and year < 2018 else 1.0)
                obs.append(dict(family=f, year=year, month=month, frac=frac))
    monthly, obs = pd.DataFrame(rows), pd.DataFrame(obs)
    r = fit_monthly_ratio(monthly, obs=obs).set_index("month")
    assert r.loc[11, "n20_scale"] == pytest.approx(1.0)  # without obs the truncated month would pull it down
    assert fit_monthly_ratio(monthly).set_index("month").loc[11, "n20_scale"] < 0.9
    h = harmonize(monthly, obs=obs).set_index(["year", "month"])
    assert h.loc[(2024, 11), "source"] == "noaa20_converted" and h.loc[(2024, 11), "fire_cell_days"] == pytest.approx(100)
    obs.loc[(obs.family == "NOAA20"), "frac"] = 0.0  # no NOAA-20 to swap in: scale S-NPP by the days observed
    h = harmonize(monthly[monthly.family != "NOAA20"], fit_monthly_ratio(monthly, obs=obs), obs=obs)
    assert h.set_index(["year", "month"]).loc[(2024, 11), "fire_cell_days"] == pytest.approx(20)
