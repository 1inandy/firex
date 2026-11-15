"""firex API.  Run: uvicorn backend.app.main:app --reload   (contract: docs/api.md)"""
from __future__ import annotations

import datetime as dt
import io

import pandas as pd
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from pipeline import config

from . import alerts as alerts_mod
from . import area as area_mod
from . import brief as brief_mod
from . import queries as q
from .area import Area

load_dotenv()
app = FastAPI(title="firex", description="Harmonized MODIS + VIIRS burning calendar")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["GET", "POST", "DELETE"], allow_headers=["*"])


@app.exception_handler(q.NoData)
def _no_data(_: Request, exc: q.NoData):
    return JSONResponse(status_code=503, content={"detail": str(exc)})


def _parse_area(bbox: str | None, poly: str | None, circle: str | None) -> Area:
    try:
        return area_mod.parse(bbox, poly, circle)
    except ValueError as e:
        raise HTTPException(400, str(e))


def area_param(bbox: str | None = Query(None, description="W,S,E,N"),
               poly: str | None = Query(None, description="Polygon: lon,lat;lon,lat;… (3–200 vertices)"),
               circle: str | None = Query(None, description="Circle: lon,lat,radius_km")) -> Area:
    """The selected area. Give one of bbox / poly / circle (poly wins, then circle)."""
    return _parse_area(bbox, poly, circle)


def optional_area(bbox: str | None = None, poly: str | None = None, circle: str | None = None) -> Area | None:
    return _parse_area(bbox, poly, circle) if (bbox or poly or circle) else None


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
            "max_date": m.get("max_date"), "method": config.HARMONIZE_METHOD,
            "overlap_start_year": config.OVERLAP_START_YEAR, "modis_drift_from": dict(config.MODIS_DRIFT_FROM)}


@app.get("/regions")
def regions():
    return [{"id": k, "name": v["name"], "bbox": list(v["bbox"])} for k, v in config.REGIONS.items()]


@app.get("/calendar")
def calendar(a: Area = Depends(area_param),
             start: str = Query("2001-01", alias="from"), end: str = Query("2100-12", alias="to")):
    h, med = q.calendar_frame(a, ym(start), ym(end))
    meta = {"area_kind": a.kind, "area_km2": round(area_mod.area_km2(a), 1)}
    if h.empty:
        return {"months": [], "median_by_month": {}, "seasonality": [], "summary": q.summary(h), "handoff": None, **meta}
    return {
        "months": q.months_payload(h),
        "median_by_month": {int(k): round(float(v), 2) for k, v in med.items()},
        "seasonality": q.seasonality(h),
        "summary": q.summary(h),
        "handoff": q.handoff(h),
        **meta,
    }


@app.get("/calendar.csv")
def calendar_csv(a: Area = Depends(area_param),
                 start: str = Query("2001-01", alias="from"), end: str = Query("2100-12", alias="to")):
    h, med = q.calendar_frame(a, ym(start), ym(end))
    out = h.rename(columns={"fire_cell_days": "value"}).assign(median_for_month=h.month.map(med))
    cols = ["year", "month", "value", "lo", "hi", "source", "anomaly", "median_for_month", "partial",
            "raw", "raw_source"]
    return _csv(out[cols].round(3), "firex_calendar.csv")


@app.get("/current")
def current(a: Area = Depends(area_param)):
    return q.current(a)


@app.get("/csv")
def csv(a: Area = Depends(area_param), start: str = Query(..., alias="from"), end: str = Query(..., alias="to")):
    """Cleaned hotspots for an area and date range (YYYY-MM-DD)."""
    df = q.sql(
        f"SELECT * FROM read_parquet(?) WHERE {q._bbox_sql('latitude', 'longitude')} "
        "AND acq_date BETWEEN CAST(? AS DATE) AND CAST(? AS DATE) ORDER BY acq_date, acq_time",
        [q._require("hotspots"), *q._bbox_args(a.bbox), day(start), day(end)])
    return _csv(a.filter(df, "latitude", "longitude"), "firex_hotspots.csv")


@app.get("/cells")
def cells(a: Area = Depends(area_param), start: str = Query("2001-01", alias="from"),
          end: str = Query("2100-12", alias="to"), limit: int = Query(5000, ge=1, le=50000)):
    return {"deg": config.GRID_DEG, "cells": q.cells(a, ym(start), ym(end), limit)}


@app.get("/events")
def events(a: Area = Depends(area_param), start: str = Query("2001-01-01", alias="from"),
           end: str = Query("2100-12-31", alias="to"), limit: int = Query(50, ge=1, le=500)):
    return {"events": q.events(a, day(start), day(end), limit)}


@app.get("/precedent")
def precedent(a: Area = Depends(area_param), month: int = Query(..., ge=1, le=12)):
    return q.precedent(a, month)


@app.get("/validation")
def validation(a: Area | None = Depends(optional_area)):
    return q.validation(a)


@app.get("/brief")
def brief(a: Area = Depends(area_param)):
    cur = q.current(a)
    h, _ = q.calendar_frame(a, 0, 10**6)
    prec = q.precedent(a, cur["month"])
    try:
        val = q.validation(a)
    except q.NoData:
        val = None
    facts = brief_mod.build_facts(cur, q.summary(h), prec, val, bool(q.meta().get("synthetic")))
    return brief_mod.brief(facts)


# ---------- alerts ----------

class AlertIn(BaseModel):
    bbox: str | None = None
    poly: str | None = None
    circle: str | None = None
    threshold: float = Field(config.ALERT_DEFAULT_THRESHOLD, gt=1, le=20)
    name: str | None = Field(None, max_length=80)
    email: str | None = Field(None, max_length=254)
    webhook: str | None = Field(None, max_length=500)


@app.get("/alerts")
def list_alerts():
    return {"alerts": alerts_mod.list_with_status(), "min_cell_days": config.ALERT_MIN_CELL_DAYS}


@app.post("/alerts", status_code=201)
def create_alert(body: AlertIn):
    params = {k: v for k, v in (("bbox", body.bbox), ("poly", body.poly), ("circle", body.circle)) if v}
    try:
        alert = alerts_mod.create(params, body.threshold, body.name, body.email, body.webhook)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {**alerts_mod.public(alert), "status": alerts_mod.status(alert)}


@app.delete("/alerts/{alert_id}", status_code=204)
def delete_alert(alert_id: str):
    if not alerts_mod.delete(alert_id):
        raise HTTPException(404, "no such alert")


@app.post("/alerts/check")
def check_alerts():
    """Evaluate all alerts and send notifications for newly triggered ones."""
    return {"results": alerts_mod.check_all(notify=True)}
