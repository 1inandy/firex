# Plume API contract

Base URL: `http://localhost:8000` (frontend reads `?api=` override). Endpoints are GET, JSON unless noted (alerts also use POST/DELETE).

**Area params.** Every endpoint that takes an area accepts exactly one of:
- `bbox=W,S,E,N` (degrees)
- `poly=lon,lat;lon,lat;…` (3–200 vertices, not closed)
- `circle=lon,lat,radius_km` (radius 0–2000 km)

If several are given, `poly` wins, then `circle`. Shapes filter by grid-cell centre (calendar, cells, current), hotspot position (`/csv`) and event centroid (events, precedent). Regional conversion ratios and validation use the region containing the shape's bounding-box centre. Below, `bbox` stands for any of the three. Month params are `YYYY-MM`; day params are `YYYY-MM-DD`.
Errors: `{"detail": "..."}` with 400 (bad params) or 503 (no data built yet).

## `/health`
```json
{"ok": true, "data_ready": true, "synthetic": true,
 "coverage": {"from": "2001-01", "to": "2026-11"}}
```
`synthetic: true` means the data came from `pipeline.demo` (not real FIRMS). The UI must show a banner.
Also returns `method` (harmonization in use), `overlap_start_year` (2012) and `modis_drift_from` (`{"Terra": 2020, "Aqua": 2022}`).

## `/regions`
Preset quick-pick areas.
```json
[{"id": "california", "name": "California", "bbox": [-124.5, 32.5, -114.0, 42.0]}]
```

## `/calendar?bbox&from&to`
Harmonized month × year series (VIIRS-equivalent fire cell-days).
```json
{"months": [{"year": 2001, "month": 1, "value": 12.0, "lo": 9.1, "hi": 15.2,
             "source": "modis_converted", "anomaly": 0.4, "partial": false}],
 "median_by_month": {"1": 10.0, "...": 0},
 "seasonality": [{"month": 1, "median": 10.0, "p10": 2.0, "p90": 30.0}],
 "summary": {
   "total": 12345.0,
   "peak_month": {"year": 2020, "month": 9, "value": 900.0},
   "peak_year": {"year": 2020, "value": 3000.0},
   "typical_peak_month": 8,
   "trend_per_decade_pct": 12.3,
   "top_anomalies": [{"year": 2020, "month": 9, "value": 900.0, "anomaly": 5.1}]
 }}
```
- `source`: `"viirs"` (2012→, measured) or `"modis_converted"` (2001–2011, MODIS × monthly ratio). `lo/hi` = 10–90% ratio band (equal to value for VIIRS).
- `anomaly` = value ÷ median for that calendar month (null if median is 0). Medians use complete months only.
- `partial: true` marks the month still in progress (its value covers only the days observed so far).
- `trend_per_decade_pct` may be null (too few years).
- `raw` / `raw_source` (`"modis"`|`"viirs"`): the un-harmonized stitched record (raw MODIS before 2012, raw VIIRS after), for the "raw sensors" view.
- `handoff`: `{"before_years": [2009, 2011], "after_years": [2012, 2014], "raw_jump": 2.3, "harmonized_jump": 1.1}`: mean annual total after ÷ before the sensor switch, raw vs. harmonized. Null when there aren't three complete years on both sides.
- `area_kind` (`"bbox"|"polygon"|"circle"`) and `area_km2`.

## `/current?bbox`
Latest month vs. history for the same calendar month.
```json
{"year": 2026, "month": 11, "value": 340.0, "median": 120.0, "ratio": 2.83,
 "highest_since": 2020, "rank": 3, "n_years": 26, "partial": true, "days_covered": 15,
 "source": "archive"}
```
- `highest_since`: most recent earlier year whose same month was higher; null if this is the record. Only meaningful when `ratio > 1`.
- When the month is partial, `value` and `median` both cover days 1–`days_covered` only (same days of past years), so the comparison is fair.
- `rank`: 1 = highest of `n_years` for that calendar month. `partial` = month not finished.
- `source`: `"nrt"` (live FIRMS) or `"archive"`.

## `/calendar.csv?bbox&from&to`
CSV download: `year,month,value,lo,hi,source,anomaly,median_for_month,partial,raw,raw_source`.

## `/csv?bbox&from=YYYY-MM-DD&to=YYYY-MM-DD`
CSV download of cleaned hotspots: `latitude,longitude,acq_date,acq_time,sensor,satellite,confidence,frp,daynight,type`.

## `/cells?bbox&from=YYYY-MM&to=YYYY-MM&limit=5000`
Grid cells with fire activity in the range (for a map heat layer), largest first.
```json
{"deg": 0.05, "cells": [{"lat": 37.025, "lon": -119.975, "cell_days": 14}]}
```

## `/events?bbox&from=YYYY-MM-DD&to=YYYY-MM-DD&limit=50`
Hotspots clustered into fire events (adjacent grid cells within a few days of each other), biggest first.
```json
{"events": [{"id": "e123", "start": "2020-08-16", "end": "2020-10-02", "duration_days": 48,
             "cell_days": 900, "area_km2": 1500.0, "peak_frp": 812.3,
             "lat": 39.1, "lon": -122.3, "bbox": [-122.6, 38.8, -121.9, 39.5]}]}
```

## `/precedent?bbox&month=11`
How fires starting in that calendar month behaved historically in the area.
```json
{"month": 11, "n_events": 42, "median_duration_days": 4, "p90_duration_days": 15,
 "median_area_km2": 12.0, "largest": {"...event...": 0}}
```
`largest` may be null when `n_events` is 0.

## `/brief?bbox`
Grounded plain-language briefing. Every number in `text` is checked against `facts`.
```json
{"text": "…", "facts": {"current_value": 340.0, "...": 0}, "verified": true,
 "unverified_numbers": [], "model": "template"}
```
`model` is `"template"` unless `ANTHROPIC_API_KEY` is set.

## `/validation?bbox` (bbox optional: picks the region containing its centre)
Region-wide out-of-sample check of the harmonization: ratios fit on 2012–2020, applied to MODIS 2021→, compared to real VIIRS.
```json
{"region": "california", "region_name": "California", "method": "frp_strata",
 "train_years": [2012, 2020], "test_years": [2021, 2026],
 "monthly": [{"year": 2021, "month": 1, "predicted": 100.0, "observed": 110.0, "modis_raw": 30.0}],
 "annual": [{"year": 2021, "predicted": 1200.0, "observed": 1300.0, "error_pct": -7.7}],
 "mape_monthly_pct": 18.2, "annual_abs_error_pct": 6.1, "r2": 0.91,
 "baseline": {"method": "ratio", "mape_monthly_pct": 25.9, "annual_abs_error_pct": 25.6, "r2": 0.83},
 "methods": [{"method": "ratio", "...": 0}, {"method": "frp_strata", "...": 0}, {"method": "quantile", "...": 0}],
 "drift_from": {"Terra": 2020, "Aqua": 2022}}
```
- `methods`: every harmonization method fit on the same training years and scored on the same test years (`quantile` only when per-cell data exists).
- `annual[].modis_drift`: MODIS satellites whose overpass had drifted by that year (e.g. `["Terra", "Aqua"]`).

## Alerts
Subscriptions stored in `data/alerts.json`. An alert triggers when the latest month (same days-so-far comparison as `/current`) is ≥ `threshold` × the historical median and has at least 5 fire cell-days (or has fire where the median is 0). It notifies at most once per calendar month.

- `GET /alerts` → `{"alerts": [{"id", "name", "area": {"circle": "-120.0,37.0,150.0"}, "threshold": 2.0, "email": "o…@example.com", "webhook": false, "created", "last_notified": "2026-11", "status": {"triggered": true, "year": 2026, "month": 11, "value": 48.0, "median": 30.8, "ratio": 1.56, "partial": true, "source": "archive"}}], "min_cell_days": 5}` (contact details masked)
- `POST /alerts` with JSON `{"bbox"|"poly"|"circle": "...", "threshold": 2.0, "name": "optional", "email": "optional", "webhook": "https://optional"}` → 201 + the alert with `status`. 400 on a bad area, email, non-https webhook or threshold outside (1, 20].
- `DELETE /alerts/{id}` → 204, or 404.
- `POST /alerts/check` → `{"results": [{"alert": {...}, "status": {...}, "notified": ["emailed" | "webhook sent" | "logged (SMTP_HOST not set)" | "email failed: …"]}]}`. Same as `python -m backend.app.alerts` (for cron).
Email uses `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `ALERT_FROM` from `.env`; webhooks get a JSON POST `{"text": "...", "status": {...}}`.
