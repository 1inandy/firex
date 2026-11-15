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


def test_raw_series_and_handoff(demo_data):
    cal = demo_data.get("/calendar", params={"bbox": CA}).json()
    m = cal["months"]
    assert {x["raw_source"] for x in m if x["year"] < 2012} == {"modis"}
    assert {x["raw_source"] for x in m if x["year"] >= 2012} == {"viirs"}
    ho = cal["handoff"]
    assert ho["before_years"] == [2009, 2011] and ho["after_years"] == [2012, 2014]
    assert abs(ho["harmonized_jump"] - 1) < abs(ho["raw_jump"] - 1)
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
    assert all(r["modis_drift"] for r in v["annual"])  # test years 2021+ are all drift years


def test_alerts_lifecycle(demo_data, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ALERTS_FILE", tmp_path / "alerts.json")
    monkeypatch.delenv("SMTP_HOST", raising=False)
    c = demo_data
    assert c.get("/alerts").json()["alerts"] == []
    assert c.post("/alerts", json={"bbox": CA, "email": "not-an-email"}).status_code == 400
    assert c.post("/alerts", json={"bbox": CA, "webhook": "http://x"}).status_code == 400
    assert c.post("/alerts", json={"threshold": 2}).status_code == 400
    r = c.post("/alerts", json={"circle": "-120,37,150", "threshold": 1.01, "name": "Sierra", "email": "ops@example.com"})
    assert r.status_code == 201
    a = r.json()
    assert a["email"] == "o…@example.com" and a["area"] == {"circle": "-120.0,37.0,150.0"}
    assert set(a["status"]) >= {"triggered", "ratio", "value", "median"}
    hi = c.post("/alerts", json={"bbox": CA, "threshold": 20}).json()
    lst = c.get("/alerts").json()["alerts"]
    assert [x["id"] for x in lst] == [a["id"], hi["id"]]
    res = {x["alert"]["id"]: x for x in c.post("/alerts/check").json()["results"]}
    assert res[hi["id"]]["notified"] == []  # 20× is never reached
    assert res[a["id"]]["status"]["triggered"]  # demo Nov 2026 is well above 1.01× median
    assert res[a["id"]]["notified"] == ["logged (SMTP_HOST not set)"]
    again = {x["alert"]["id"]: x for x in c.post("/alerts/check").json()["results"]}
    assert again[a["id"]]["notified"] == []  # once per month
    assert c.delete(f"/alerts/{a['id']}").status_code == 204
    assert c.delete(f"/alerts/{a['id']}").status_code == 404
    assert [x["id"] for x in c.get("/alerts").json()["alerts"]] == [hi["id"]]
