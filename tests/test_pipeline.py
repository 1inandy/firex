import datetime as dt

import numpy as np
import pandas as pd
import pytest

from pipeline.clean import clean
from pipeline.grid import monthly_cell_days, to_grid
from pipeline.harmonize import fit_monthly_ratio, harmonize


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


def test_ratio_and_harmonized_series():
    monthly = monthly_cell_days(to_grid(clean(synthetic())))
    ratios = fit_monthly_ratio(monthly)
    assert ratios.ratio.tolist() == pytest.approx([3.0, 3.0])
    h = harmonize(monthly)
    early = h[h.year == 2005]
    assert set(early.source) == {"modis_converted"}
    assert early.fire_cell_days.tolist() == pytest.approx([30.0, 30.0])
    assert set(h[h.year >= 2012].source) == {"viirs"}
