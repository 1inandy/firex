from fastapi.testclient import TestClient

from backend.app import main
from pipeline import config
from pipeline.clean import clean
from pipeline.grid import monthly_cell_days, to_grid
from pipeline.harmonize import fit_monthly_ratio
from tests.test_pipeline import synthetic


def test_calendar_and_csv(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "GRID", tmp_path / "grid")
    monkeypatch.setattr(config, "HARMONIZED", tmp_path / "h")
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
