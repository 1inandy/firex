# Plume API contract

Base URL: `/api` on the deployed site (`uvicorn index:app` locally), or `http://localhost:8000` when running `backend.app.main:app` alone; the frontend reads an `?api=` override. All endpoints are GET, JSON unless noted.

**Area params.** Every endpoint that takes an area accepts exactly one of:
- `bbox=W,S,E,N` (degrees)
- `poly=lon,lat;lon,lat;…` (3–200 vertices, not closed)
- `circle=lon,lat,radius_km` (radius 0–2000 km)

If several are given, `poly` wins, then `circle`. Shapes filter by grid-cell centre (calendar, cells, current), hotspot position (`/csv`) and event centroid (events, precedent). Regional conversion ratios and validation use the region containing the shape's bounding-box centre. Below, `bbox` stands for any of the three. Month params are `YYYY-MM`; day params are `YYYY-MM-DD`.
Errors: `{"detail": "..."}` with 400 (bad params), 413 (CSV request too large) or 503 (no data built yet).

## `/health`
```json
{"ok": true, "data_ready": true, "synthetic": true,
 "coverage": {"from": "2001-01", "to": "2026-11"}}
```
`synthetic: true` means the data came from `pipeline.demo` (not real FIRMS). The UI must show a banner.
Also returns `method` (harmonization in use), `overlap_start_year` (2012), `modis_drift_from` (`{"Terra": 2020, "Aqua": 2022}`),
`aqua_first_month` (`"2002-07"`; earlier MODIS is Terra only), `snpp_end` (last full month of S-NPP coverage), `sensors` (per family `[first, last]` day covered by the input files), `outages` (per sensor group,
detected outage date ranges) and `live_nrt` (the server has a FIRMS key).

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
- `source`: `"modis_converted"` (2000–2011, MODIS converted; Terra-only months before 2002-07 also scaled to Terra + Aqua), `"viirs"` (S-NPP, measured) or `"noaa20_converted"` (NOAA-20 in S-NPP units). The choice is made per month from coverage: NOAA-20 replaces S-NPP in any month S-NPP missed days (an outage such as Jul–Aug 2022, or after S-NPP ended) when NOAA-20 observed more of it. `lo/hi` = 10–90% band from leave-one-year-out errors (equal to value for measured S-NPP).
- `anomaly` = value ÷ median for that calendar month (null if median is 0, and for the partial month). The median uses every complete month of the whole record, whatever `from`/`to` is displayed.
- `observed`: share of the month's days the chosen sensor was delivering data (1 = complete). Below 1, the missing
  days were an outage (or the start of a sensor's coverage) and the value is scaled up to the whole month; an S-NPP month
  with an outage uses NOAA-20 instead when NOAA-20 observed more of it.
- For areas under ~100,000 km² (~3,000 km² for the finest class), converted months use the wider band measured on 1° /
  0.25° tiles (`/validation` → `small_area`).
- `partial: true` marks the month still in progress (its value covers only the days observed so far).
- `trend_per_decade_pct` may be null (too few years). `trend_t` = slope ÷ standard error; `trend_significant` = |t| ≥ 2.
- `raw` / `raw_source` (`"modis"`|`"viirs"`|`"noaa20"`): the un-harmonized stitched record, for the "raw sensors" view.
- `handoff`: `{"overlap_years": [2012, 2019], "viirs_per_modis": 1.95, "before_years": [2009, 2011], "after_years": [2012, 2014], "raw_jump": 2.51, "harmonized_jump": 1.17}`. `viirs_per_modis` = VIIRS ÷ MODIS fire cell-days in this area over the same months (the sensor difference, measured simultaneously). The jumps compare three complete years either side of 2012 (context only: they include real change). Null without overlap data.
- `area_kind` (`"bbox"|"polygon"|"circle"`) and `area_km2`.

## `/current?bbox`
This month so far vs. the same days of the same month in every past year. Anchored to today: the month containing
yesterday. Days after the archive ends come from FIRMS `VIIRS_NOAA20_NRT` (5-day calls from the 1st; static heat
sources masked); `days_covered` is the last day actually fetched.
```json
{"year": 2026, "month": 11, "value": 340.0, "median": 120.0, "ratio": 2.83,
 "highest_since": 2020, "rank": 3, "n_years": 26, "partial": true, "days_covered": 15,
 "live": true, "source": "nrt", "note": null}
```
- Without a server `FIRMS_MAP_KEY` (or if FIRMS is unreachable): `live: false`, `source: "archive"`, the latest complete archived month, and `note` saying so.
- `highest_since`: most recent earlier year whose same month was higher; null if this is the record. Only meaningful when `ratio > 1`.
- When the month is partial, `value` and `median` both cover days 1–`days_covered` only (same days of past years), so the comparison is fair.
- `rank`: 1 = highest of `n_years` for that calendar month. `partial` = month not finished.

## `/calendar.csv?bbox&from&to`
CSV download: `year,month,value,lo,hi,source,anomaly,median_for_month,partial,raw,raw_source`.

## `/csv?bbox&from=YYYY-MM-DD&to=YYYY-MM-DD`
CSV download of cleaned hotspots, as detected (all sensors, not harmonized), streamed: `latitude,longitude,acq_date,acq_time,sensor,satellite,confidence,frp,daynight,type`. Filters on exact detection position. More than 1,000,000 rows → 413.

## `/cells?bbox&from=YYYY-MM&to=YYYY-MM&limit=5000`
Grid cells with fire activity in the range (for a map heat layer), largest first, in the calendar's harmonized units.
```json
{"deg": 0.05, "cells": [{"lat": 37.025, "lon": -119.975, "cell_days": 14.6}]}
```

## `/events?bbox&from=YYYY-MM-DD&to=YYYY-MM-DD&limit=50`
Detections clustered into fire events (adjacent grid cells within a few days of each other), most fire cell-days first.
`area_km2` is the detection footprint (area of grid cells with detections), not burned area; `duration_days` is the detection span.
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
Plain-language briefing written by a deterministic template from the computed `facts` (no model; every number is a fact).
```json
{"text": "…", "facts": {"current_value": 340.0, "...": 0}}
```

## `/validation?bbox` (bbox optional: picks the region containing its centre)
Out-of-sample checks of the conversion actually used (fit on 2012–2019, before the MODIS orbit drift).
```json
{"region": "california", "region_name": "California", "method": "frp_strata", "scheme": "leave-one-year-out",
 "train_years": [2012, 2019], "test_years": [2012, 2019],
 "monthly": [{"year": 2012, "month": 1, "predicted": 100.0, "observed": 110.0, "modis_raw": 30.0}],
 "annual": [{"year": 2012, "predicted": 5923.9, "observed": 5451.0, "error_pct": 8.7, "modis_drift": []}],
 "mape_monthly_pct": 17.1, "annual_abs_error_pct": 6.6, "r2": 0.934,
 "baseline": {"method": "ratio", "mape_monthly_pct": 17.6, "annual_abs_error_pct": 8.1, "r2": 0.875},
 "methods": [{"method": "ratio", "...": 0}, {"method": "frp_strata", "...": 0}, {"method": "quantile", "...": 0}],
 "spatial": {"description": "…", "method": "frp_strata", "annual_abs_error_pct": 7.4, "...": 0, "methods": []},
 "drift_test": {"years": [2020, 2025], "annual": [], "annual_abs_error_pct": 7.4, "mean_bias_pct": -6.5},
 "band": [0.827, 1.282], "band_coverage_pct": 73.6, "band_coverage_on": "drift-test years (not used to set the band)",
 "noaa20_bridge": {"years": [2019, 2025], "annual": [], "annual_abs_error_pct": 2.3},
 "terra_scale": {"1": 2.36, "...": 0}, "drift_from": {"Terra": 2020, "Aqua": 2022}}
```
- `annual`/`monthly`/`methods`: each year 2012–2019 predicted by a conversion fit on the other years.
- `spatial`: fit on half the grid cells (checkerboard), scored on the other half.
- `drift_test`: the deployed conversion applied to 2020→ (drifted MODIS); `mean_bias_pct` < 0 = MODIS sees fewer fires.
- `band`: the 10–90% multiplicative band shipped with converted months; `band_coverage_pct` = share of drift-test months inside it.
- `noaa20_bridge`: leave-one-year-out error of the NOAA-20 → S-NPP factor. `terra_scale`: Terra + Aqua ÷ Terra per month.
- `small_area`: `[{"tile_deg": 1.0, "median_annual_abs_error_pct": 17.3, "p90_annual_abs_error_pct": 56.1, "band": [0.63, 2.73], "band_add_per_tile": 6.0, "band_coverage_pct": 76.2, "zero_prediction_share_pct": 32.3, "n_tile_years": 261}, …]`, the same leave-one-year-out test on 1° and 0.25° tiles; coverage is over all active tile-months.
- All fits and scores use only months that every relevant satellite observed completely (see `/health` → `outages`).
