"""firex API.  Run: uvicorn backend.app.main:app --reload"""
from __future__ import annotations

import duckdb
import pandas as pd
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

from pipeline import config
from pipeline.harmonize import harmonize

app = FastAPI(title="firex")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


def parse_bbox(bbox: str) -> tuple[float, float, float, float]:
    try:
        w, s, e, n = (float(x) for x in bbox.split(","))
    except ValueError:
        raise HTTPException(400, "bbox must be W,S,E,N")
    return w, s, e, n


def ym(v: str) -> int:
    y, m = v.split("-")
    return int(y) * 12 + int(m)


@app.get("/health")
def health():
    return {"ok": True, "data_ready": (config.GRID / "monthly.parquet").exists()}


@app.get("/calendar")
def calendar(bbox: str = Query(..., description="W,S,E,N"),
             start: str = Query("2001-01", alias="from"), end: str = Query("2100-12", alias="to")):
    w, s, e, n = parse_bbox(bbox)
    monthly_path, ratios_path = config.GRID / "monthly.parquet", config.HARMONIZED / "ratios.parquet"
    if not monthly_path.exists():
        raise HTTPException(503, "No data yet: run `python -m pipeline.run data/raw/<region>`")
    monthly = duckdb.execute(
        "SELECT * FROM read_parquet(?) WHERE lon BETWEEN ? AND ? AND lat BETWEEN ? AND ?",
        [str(monthly_path), w, e, s, n]).df()
    if monthly.empty:
        return {"months": [], "median_by_month": {}}
    ratios = pd.read_parquet(ratios_path) if ratios_path.exists() else None
    h = harmonize(monthly, ratios)
    h = h[(h.year * 12 + h.month >= ym(start)) & (h.year * 12 + h.month <= ym(end))]
    med = h.groupby("month")["fire_cell_days"].median()
    h = h.assign(anomaly=h.fire_cell_days / h.month.map(med).replace(0, float("nan")))
    return {
        "months": [{"year": int(r.year), "month": int(r.month), "value": float(r.fire_cell_days),
                    "lo": float(r.lo), "hi": float(r.hi), "source": r.source,
                    "anomaly": None if pd.isna(r.anomaly) else round(float(r.anomaly), 3)}
                   for r in h.itertuples()],
        "median_by_month": {int(k): float(v) for k, v in med.items()},
    }


@app.get("/csv")
def csv(bbox: str, start: str = Query(..., alias="from"), end: str = Query(..., alias="to")):
    """Raw cleaned hotspots for an area and date range (YYYY-MM-DD)."""
    w, s, e, n = parse_bbox(bbox)
    path = config.GRID / "hotspots.parquet"
    if not path.exists():
        raise HTTPException(503, "No data yet")
    df = duckdb.execute(
        "SELECT * FROM read_parquet(?) WHERE longitude BETWEEN ? AND ? AND latitude BETWEEN ? AND ? "
        "AND acq_date BETWEEN CAST(? AS DATE) AND CAST(? AS DATE) ORDER BY acq_date",
        [str(path), w, e, s, n, start, end]).df()
    return StreamingResponse(iter([df.to_csv(index=False)]), media_type="text/csv",
                             headers={"Content-Disposition": "attachment; filename=firex_hotspots.csv"})
