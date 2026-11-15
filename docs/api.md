# firex API contract

Base URL: `http://localhost:8000` (frontend reads `?api=` override). All endpoints are GET, JSON unless noted.
`bbox` is always `W,S,E,N` in degrees. Month params are `YYYY-MM`; day params are `YYYY-MM-DD`.
Errors: `{"detail": "..."}` with 400 (bad params) or 503 (no data built yet).

## `/health`
```json
{"ok": true, "data_ready": true, "synthetic": true,
 "coverage": {"from": "2001-01", "to": "2026-11"}}
```
`synthetic: true` means the data came from `pipeline.demo` (not real FIRMS). The UI must show a banner.

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
CSV download: `year,month,value,lo,hi,source,anomaly,median_for_month`.

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
 "baseline": {"method": "ratio", "mape_monthly_pct": 25.9, "annual_abs_error_pct": 25.6, "r2": 0.83}}
```
