import datetime as dt

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from backend.app import main
from backend.app import queries as q
from backend.app.area import parse
from pipeline import config, demo, run
from pipeline.clean import clean
from pipeline.grid import monthly_cell_days, to_grid
from pipeline.harmonize import fit_monthly_ratio
from tests.test_pipeline import synthetic

CA = "-124.5,32.5,-114,42"


def _paths(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "GRID", tmp_path / "grid")
    monkeypatch.setattr(config, "HARMONIZED", tmp_path / "h")
    monkeypatch.setattr(config, "META", tmp_path / "meta.json")
    monkeypatch.delenv("FIRMS_MAP_KEY", raising=False)


def test_calendar_and_csv(tmp_path, monkeypatch):
    _paths(monkeypatch, tmp_path)
    config.GRID.mkdir(); config.HARMONIZED.mkdir()
    hot = clean(synthetic())
    hot.to_parquet(config.GRID / "hotspots.parquet")
    monthly = monthly_cell_days(to_grid(hot))
    monthly.to_parquet(config.GRID / "monthly.parquet")
    fit_monthly_ratio(monthly).to_parquet(config.HARMONIZED / "ratios.parquet")

    c = TestClient(main.app)
    r = c.get("/calendar", params={"bbox": "-120,33,-117,38"})
    assert r.status_code == 200
    months = r.json()["months"]
    assert months[0]["year"] == 2005 and months[0]["source"] == "modis_converted"
    r = c.get("/csv", params={"bbox": "-120,33,-117,38", "from": "2013-01-01", "to": "2013-12-31"})
    assert r.status_code == 200 and "latitude" in r.text


def test_no_data_is_503(tmp_path, monkeypatch):
    _paths(monkeypatch, tmp_path)
    c = TestClient(main.app)
    assert c.get("/health").json()["data_ready"] is False
    assert c.get("/calendar", params={"bbox": CA}).status_code == 503
    assert c.get("/calendar", params={"bbox": "1,2,3"}).status_code == 400


@pytest.fixture(scope="module")
def demo_data(tmp_path_factory):
    """Full pipeline on one synthetic region (a few seconds)."""
    root = tmp_path_factory.mktemp("demo")
    mp = pytest.MonkeyPatch()
    mp.setattr(config, "GRID", root / "grid")
    mp.setattr(config, "HARMONIZED", root / "h")
    mp.setattr(config, "META", root / "meta.json")
    mp.delenv("FIRMS_MAP_KEY", raising=False)
    demo.generate(root / "raw", end=dt.date(2026, 11, 15), regions=[demo.REGIONS[0]])
    run.main(str(root / "raw"))
    yield TestClient(main.app)
    mp.undo()


def test_end_to_end_endpoints(demo_data):
    c = demo_data
    h = c.get("/health").json()
    assert h["data_ready"] and h["synthetic"] and h["coverage"] == {"from": "2001-01", "to": "2026-11"}

    cal = c.get("/calendar", params={"bbox": CA}).json()
    m = cal["months"]
    assert (m[0]["year"], m[0]["month"]) == (2001, 1) and (m[-1]["year"], m[-1]["month"]) == (2026, 11)
    assert m[-1]["partial"] and not m[-2]["partial"]
    assert {x["source"] for x in m if x["year"] < 2012} == {"modis_converted"}
    assert len(cal["seasonality"]) == 12 and cal["summary"]["typical_peak_month"] in range(6, 11)

    cur = q.current(parse(CA, None, None), today=dt.date(2026, 11, 16))
    assert (cur["year"], cur["month"], cur["days_covered"], cur["partial"]) == (2026, 11, 15, True)
    assert cur["n_years"] == 26 and 1 <= cur["rank"] <= 26 and cur["source"] == "archive"
    assert c.get("/current", params={"bbox": CA}).status_code == 200

    ev = c.get("/events", params={"bbox": CA, "from": "2020-01-01", "to": "2020-12-31", "limit": 5}).json()["events"]
    assert len(ev) == 5 and ev[0]["cell_days"] >= ev[-1]["cell_days"] and len(ev[0]["start"]) == 10

    prec = c.get("/precedent", params={"bbox": CA, "month": 8}).json()
    assert prec["n_events"] > 0 and prec["largest"]["start"][5:7] == "08"

    val = c.get("/validation", params={"bbox": CA}).json()
    assert val["region"] == "california" and val["scheme"] == "leave-one-year-out"
    assert val["test_years"] == [2012, 2019] and val["drift_test"]["years"][0] == 2020
    assert val["annual_abs_error_pct"] < val["baseline"]["annual_abs_error_pct"]

    cells = c.get("/cells", params={"bbox": CA, "limit": 10}).json()["cells"]
    assert len(cells) == 10

    csv = c.get("/calendar.csv", params={"bbox": CA}).text.splitlines()
    assert csv[0].startswith("year,month,value") and len(csv) == 1 + len(m)

    b = c.get("/brief", params={"bbox": CA}).json()
    assert "synthetic" in b["text"] and "burned" not in b["text"] and b["facts"]["year"] == 2026


def test_current_month_is_anchored_to_today(demo_data):
    """Past the archive end without NRT: say so and fall back to the last full archived month."""
    a = parse(CA, None, None)
    cur = q.current(a, today=dt.date(2026, 12, 20))
    assert not cur["live"] and "FIRMS_MAP_KEY" in cur["note"]
    assert (cur["year"], cur["month"], cur["partial"]) == (2026, 10, False)


def test_current_month_uses_live_nrt(demo_data, monkeypatch):
    """With NRT, the month so far comes from NOAA-20 detections, compared with the same days of past years."""
    a = parse(CA, None, None)
    nrt = pd.DataFrame({"latitude": [37.01, 37.21], "longitude": [-120.01, -120.21],
                        "acq_date": [dt.date(2026, 12, 2)] * 2, "acq_time": ["1030"] * 2, "sensor": "VIIRS_NOAA20",
                        "satellite": "N20", "confidence": "n", "frp": [5.0, 50.0], "daynight": "D", "type": pd.NA})
    monkeypatch.setenv("FIRMS_MAP_KEY", "test")
    monkeypatch.setattr(q, "_nrt_fetch", lambda bbox, since, until, key: (nrt, dt.date(2026, 12, 4)))
    cur = q.current(a, today=dt.date(2026, 12, 6))
    assert cur["live"] and (cur["year"], cur["month"], cur["days_covered"]) == (2026, 12, 4)
    assert cur["value"] > 0 and cur["source"] == "nrt"


def test_anomaly_baseline_does_not_depend_on_the_displayed_range(demo_data):
    full = {(m["year"], m["month"]): m["anomaly"] for m in
            demo_data.get("/calendar", params={"bbox": CA}).json()["months"]}
    one = demo_data.get("/calendar", params={"bbox": CA, "from": "2015-01", "to": "2015-12"}).json()["months"]
    assert [m["anomaly"] for m in one] == [full[(2015, k)] for k in range(1, 13)]


def test_csv_streams_and_caps(demo_data, monkeypatch):
    r = demo_data.get("/csv", params={"bbox": CA, "from": "2020-08-01", "to": "2020-08-31"})
    lines = r.text.splitlines()
    assert r.status_code == 200 and lines[0].startswith("latitude,longitude") and len(lines) > 100
    monkeypatch.setattr(main, "CSV_MAX_ROWS", 10)
    assert demo_data.get("/csv", params={"bbox": CA, "from": "2020-08-01", "to": "2020-08-31"}).status_code == 413


def test_parallel_requests_do_not_collide(demo_data):
    """Routes run in a thread pool; a shared duckdb connection used to fail with 'connection is busy'."""
    from concurrent.futures import ThreadPoolExecutor
    paths = [("/calendar", {"bbox": CA}), ("/current", {"bbox": CA}), ("/cells", {"bbox": CA}),
             ("/events", {"bbox": CA}), ("/precedent", {"bbox": CA, "month": 8})] * 6
    with ThreadPoolExecutor(12) as ex:
        codes = list(ex.map(lambda p: demo_data.get(p[0], params=p[1]).status_code, paths))
    assert set(codes) == {200}


def test_raw_series_and_handoff(demo_data):
    cal = demo_data.get("/calendar", params={"bbox": CA}).json()
    m = cal["months"]
    assert {x["raw_source"] for x in m if x["year"] < 2012} == {"modis"}
    assert {x["raw_source"] for x in m if x["year"] >= 2012} == {"viirs"}  # S-NPP observed every month here
    # Quiet synthetic days are not mistaken for outages; only Jan 2012 is partial (S-NPP starts on the 20th).
    assert [(x["year"], x["month"]) for x in m if x["observed"] < 1] == [(2012, 1)]
    ho = cal["handoff"]
    assert ho["before_years"] == [2009, 2011] and ho["after_years"] == [2012, 2014]
    assert abs(ho["harmonized_jump"] - 1) < abs(ho["raw_jump"] - 1)
    assert ho["overlap_years"] == [2012, 2019] and ho["viirs_per_modis"] > 1.1
    assert "raw" in demo_data.get("/calendar.csv", params={"bbox": CA}).text.splitlines()[0]


def test_polygon_and_circle_areas(demo_data):
    c = demo_data
    total = lambda **p: c.get("/calendar", params=p).json()["summary"]["total"]  # noqa: E731
    box = total(bbox=CA)
    # Triangle = half of the California bbox (west/south half).
    tri = total(poly="-124.5,32.5;-114,32.5;-124.5,42")
    assert 0 < tri < box
    circ = c.get("/calendar", params={"circle": "-120,37,150"}).json()
    assert circ["area_kind"] == "circle" and circ["area_km2"] == pytest.approx(3.14159 * 150 ** 2, rel=1e-3)
    assert 0 < circ["summary"]["total"] < box
    # Every endpoint takes the shape params.
    for path, extra in [("/current", {}), ("/cells", {}), ("/events", {}), ("/precedent", {"month": 8}),
                        ("/csv", {"from": "2020-08-01", "to": "2020-08-31"}), ("/brief", {})]:
        assert c.get(path, params={"circle": "-120,37,150", **extra}).status_code == 200, path
    cells = c.get("/cells", params={"circle": "-120,37,150", "limit": 50000}).json()["cells"]
    from backend.app.area import haversine_km
    assert all(haversine_km(x["lat"], x["lon"], 37, -120) <= 150 for x in cells)
    assert c.get("/calendar", params={"poly": "-120,37;-119,37"}).status_code == 400
    assert c.get("/calendar", params={"circle": "-120,37,0"}).status_code == 400
    assert c.get("/calendar").status_code == 400


def test_validation_compares_methods_and_flags_drift(demo_data):
    v = demo_data.get("/validation", params={"bbox": CA}).json()
    assert [m["method"] for m in v["methods"]] == ["ratio", "frp_strata", "quantile"]
    assert v["drift_from"] == {"Terra": 2020, "Aqua": 2022}
    assert not any(r["modis_drift"] for r in v["annual"])  # held-out years predate the drift
    assert all(r["modis_drift"] for r in v["drift_test"]["annual"])  # stress test: drift years only
