"""firex API.  Run: uvicorn backend.app.main:app --reload   (contract: docs/api.md)"""
from __future__ import annotations

import datetime as dt
import io

import duckdb
import pandas as pd
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse

from pipeline import config

from . import brief as brief_mod
from . import queries as q

load_dotenv()
app = FastAPI(title="firex", description="Harmonized MODIS + VIIRS burning calendar")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["GET"], allow_headers=["*"])


@app.exception_handler(q.NoData)
def _no_data(_: Request, exc: q.NoData):
    return JSONResponse(status_code=503, content={"detail": str(exc)})


def parse_bbox(bbox: str) -> q.Bbox:
    try:
        w, s, e, n = (float(x) for x in bbox.split(","))
    except ValueError:
        raise HTTPException(400, "bbox must be W,S,E,N")
    if not (-180 <= w < e <= 180 and -90 <= s < n <= 90):
        raise HTTPException(400, "bbox must satisfy W<E, S<N within lon/lat bounds")
    return w, s, e, n


def ym(v: str) -> int:
    try:
        y, m = v.split("-")[:2]
        y, m = int(y), int(m)
        assert 1 <= m <= 12
    except (ValueError, AssertionError):
        raise HTTPException(400, f"bad month {v!r}: use YYYY-MM")
    return y * 12 + m


def day(v: str) -> str:
    try:
        return dt.date.fromisoformat(v).isoformat()
    except ValueError:
        raise HTTPException(400, f"bad date {v!r}: use YYYY-MM-DD")


def _csv(df: pd.DataFrame, name: str) -> StreamingResponse:
    buf = io.StringIO()
    df.to_csv(buf, index=False)
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition": f"attachment; filename={name}"})


@app.get("/health")
def health():
    m = q.meta()
    ready = (config.GRID / "monthly.parquet").exists()
    cov = None
    if m.get("min_date"):
        cov = {"from": m["min_date"][:7], "to": m["max_date"][:7]}
    return {"ok": True, "data_ready": ready, "synthetic": bool(m.get("synthetic")), "coverage": cov,
            "max_date": m.get("max_date"), "method": config.HARMONIZE_METHOD}


@app.get("/regions")
def regions():
    return [{"id": k, "name": v["name"], "bbox": list(v["bbox"])} for k, v in config.REGIONS.items()]


@app.get("/calendar")
def calendar(bbox: str = Query(..., description="W,S,E,N"),
             start: str = Query("2001-01", alias="from"), end: str = Query("2100-12", alias="to")):
    b = parse_bbox(bbox)
    h, med = q.calendar_frame(b, ym(start), ym(end))
    if h.empty:
        return {"months": [], "median_by_month": {}, "seasonality": [], "summary": q.summary(h)}
    return {
        "months": q.months_payload(h),
        "median_by_month": {int(k): round(float(v), 2) for k, v in med.items()},
        "seasonality": q.seasonality(h),
        "summary": q.summary(h),
    }


@app.get("/calendar.csv")
def calendar_csv(bbox: str, start: str = Query("2001-01", alias="from"), end: str = Query("2100-12", alias="to")):
    h, med = q.calendar_frame(parse_bbox(bbox), ym(start), ym(end))
    out = h.rename(columns={"fire_cell_days": "value"}).assign(median_for_month=h.month.map(med))
    cols = ["year", "month", "value", "lo", "hi", "source", "anomaly", "median_for_month", "partial"]
    return _csv(out[cols].round(3), "firex_calendar.csv")


@app.get("/current")
def current(bbox: str):
    return q.current(parse_bbox(bbox))


@app.get("/csv")
def csv(bbox: str, start: str = Query(..., alias="from"), end: str = Query(..., alias="to")):
    """Cleaned hotspots for an area and date range (YYYY-MM-DD)."""
    b = parse_bbox(bbox)
    df = duckdb.execute(
        f"SELECT * FROM read_parquet(?) WHERE {q._bbox_sql('latitude', 'longitude')} "
        "AND acq_date BETWEEN CAST(? AS DATE) AND CAST(? AS DATE) ORDER BY acq_date, acq_time",
        [q._require("hotspots"), *q._bbox_args(b), day(start), day(end)]).df()
    return _csv(df, "firex_hotspots.csv")


@app.get("/cells")
def cells(bbox: str, start: str = Query("2001-01", alias="from"), end: str = Query("2100-12", alias="to"),
          limit: int = Query(5000, ge=1, le=50000)):
    return {"deg": config.GRID_DEG, "cells": q.cells(parse_bbox(bbox), ym(start), ym(end), limit)}


@app.get("/events")
def events(bbox: str, start: str = Query("2001-01-01", alias="from"), end: str = Query("2100-12-31", alias="to"),
           limit: int = Query(50, ge=1, le=500)):
    return {"events": q.events(parse_bbox(bbox), day(start), day(end), limit)}


@app.get("/precedent")
def precedent(bbox: str, month: int = Query(..., ge=1, le=12)):
    return q.precedent(parse_bbox(bbox), month)


@app.get("/validation")
def validation(bbox: str | None = None):
    return q.validation(parse_bbox(bbox) if bbox else None)


@app.get("/brief")
def brief(bbox: str):
    b = parse_bbox(bbox)
    cur = q.current(b)
    h, _ = q.calendar_frame(b, 0, 10**6)
    prec = q.precedent(b, cur["month"])
    try:
        val = q.validation(b)
    except q.NoData:
        val = None
    facts = brief_mod.build_facts(cur, q.summary(h), prec, val, bool(q.meta().get("synthetic")))
    return brief_mod.brief(facts)
