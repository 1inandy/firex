import datetime as dt

import numpy as np
import pandas as pd
import pytest

from pipeline import config
from pipeline.clean import clean, swath_duplicates
from pipeline.events import build_events
from pipeline.grid import monthly_cell_days, to_grid
from pipeline.harmonize import fit_monthly_ratio, fit_regional_ratios, harmonize, region_for_bbox, validate


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
    v = validate(monthly, train_end=2019)
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
