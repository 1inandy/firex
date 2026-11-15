import datetime as dt

import pytest
from fastapi.testclient import TestClient

from backend.app import brief, main
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
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)


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
    mp.delenv("ANTHROPIC_API_KEY", raising=False)
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

    cur = c.get("/current", params={"bbox": CA}).json()
    assert (cur["year"], cur["month"], cur["days_covered"], cur["partial"]) == (2026, 11, 15, True)
    assert cur["n_years"] == 26 and 1 <= cur["rank"] <= 26

    ev = c.get("/events", params={"bbox": CA, "from": "2020-01-01", "to": "2020-12-31", "limit": 5}).json()["events"]
    assert len(ev) == 5 and ev[0]["cell_days"] >= ev[-1]["cell_days"] and len(ev[0]["start"]) == 10

    prec = c.get("/precedent", params={"bbox": CA, "month": 8}).json()
    assert prec["n_events"] > 0 and prec["largest"]["start"][5:7] == "08"

    val = c.get("/validation", params={"bbox": CA}).json()
    assert val["region"] == "california" and val["test_years"][0] == 2021
    assert val["annual_abs_error_pct"] < val["baseline"]["annual_abs_error_pct"]

    cells = c.get("/cells", params={"bbox": CA, "limit": 10}).json()["cells"]
    assert len(cells) == 10

    csv = c.get("/calendar.csv", params={"bbox": CA}).text.splitlines()
    assert csv[0].startswith("year,month,value") and len(csv) == 1 + len(m)

    b = c.get("/brief", params={"bbox": CA}).json()
    assert b["model"] == "template" and b["verified"] and "synthetic" in b["text"]


def test_brief_verifier_flags_invented_numbers():
    facts = {"current_value": 340.0, "historical_median": 120.0, "ratio_to_median": 2.83, "year": 2026,
             "largest_event_start": "2017-10-08"}
    ok, bad = brief.verify("340 fire cell-days vs 120; 2.8× normal; largest began 2017, in 2026.", facts)
    assert bad == []
    marked, bad = brief.verify("About 55% more than 2019.", facts)
    assert bad == ["55", "2019"] and "[unverified]" in marked


def test_parallel_requests_do_not_collide(demo_data):
    """Routes run in a thread pool; a shared duckdb connection used to fail with 'connection is busy'."""
    from concurrent.futures import ThreadPoolExecutor
    paths = [("/calendar", {"bbox": CA}), ("/current", {"bbox": CA}), ("/cells", {"bbox": CA}),
             ("/events", {"bbox": CA}), ("/precedent", {"bbox": CA, "month": 8})] * 6
    with ThreadPoolExecutor(12) as ex:
        codes = list(ex.map(lambda p: demo_data.get(p[0], params=p[1]).status_code, paths))
    assert set(codes) == {200}
