# Plume: a harmonized burning-activity calendar

*NASA Space Apps 2026: [Harmonization of MODIS and VIIRS Hot Spots](https://www.spaceappschallenge.org/2026/challenges/modis-and-viirs-hot-spots/)*

MODIS has recorded fire hotspots since 2000, but Terra and Aqua are drifting and being retired. VIIRS replaced it, and it sees about twice as many fire cell-days, so the two records can't be compared directly. The record is breaking a second time right now: Suomi NPP, the VIIRS satellite that carried it since 2012, [stopped delivering data on 2 November 2026](https://www.earthdata.nasa.gov/data/alerts-outages/reminder-new-suomi-npp-observations-cease-november-2-2026).

**Plume** merges MODIS (2000→), VIIRS S-NPP (2012→) and VIIRS NOAA-20 (2018→) into one consistent record and turns it into a burning calendar for any area you draw. It shows which months and years burned most, anomalies against the historical median, and how *this* month compares with the same days in every past year. You can download the hotspots and the calendar as CSV.

All data is real NASA FIRMS data. Nothing in the deployed app is simulated.

## What it does (mapped to the challenge)
| Challenge asks for | Plume |
|---|---|
| Harmonize MODIS + VIIRS so the sensor no longer matters | One series in S-NPP-equivalent *fire cell-days*. MODIS is converted with an FRP-stratified fit, Terra-only months are scaled to Terra + Aqua, and NOAA-20 is bridged across the S-NPP shutdown. Validated out of sample (below). |
| Select a region and date range | Draw a rectangle, polygon or circle, type coordinates, or use a quick pick. The URL keeps the state. |
| Burning calendar, early 2000s → now | Month × year heatmap (viridis), with counts, anomaly (diverging ramp) and raw-sensor views. |
| Current month vs historical median | "This month vs. history" compares the month so far (live NOAA-20 data when a FIRMS key is set) with the same days of that month in every year since 2000. |
| Peaks, anomalies, trends | Peak month and year, typical peak month, largest anomalies, trend per decade, seasonality band, and fire events. |
| Accessible color ramps | Viridis/cividis sequential and a colorblind-safe diverging ramp. Every cell is keyboard-focusable with a text label. |
| Download CSV + figures | Hotspot CSV (exact detections), calendar CSV (with uncertainty and sources), and PNG figures. |

## How the harmonization works
1. **Clean** (`pipeline/clean.py`): keep vegetation fires (`type == 0`), MODIS confidence ≥ 30 (the user guide's nominal + high), and VIIRS nominal/high. Drop exact duplicates and swath-edge "bow-tie" repeats: same satellite and overpass, closer than one pixel, with the brighter detection kept. Near-real-time data has no `type`, so detections within about 1 km of the archive's known static sources (flares, industry, volcanoes) are dropped.
2. **Grid** (`pipeline/grid.py`): snap to 0.05° cells and count **fire cell-days**, the number of cells with at least one detection on a day, per sensor family. This is our own metric. Counting cells instead of pixels removes most of the difference between 1 km and 375 m pixels.
3. **Convert** (`pipeline/harmonize.py`), per region and calendar month:
   - **MODIS → VIIRS**: `VIIRS ≈ a·MODIS_weak + b·MODIS_strong`, where strong means a cell-day with max FRP ≥ 30 MW. VIIRS adds far more detections around small, weak fires than around big ones, so splitting by intensity follows each year's fire mix. It is fit on **2012–2019 only**. Terra and Aqua began drifting in 2020 and 2022, and the conversion is applied to 2000–2011, before any drift.
   - **Terra-only months** (Nov 2000 – Jun 2002, before Aqua) are scaled by the Terra + Aqua ÷ Terra ratio learned from 2003–2019, with its year-to-year spread added to the band. Terra alone sees only 58% (California) and 39% (Chaco) of what both satellites see, so without this the record would start far too low.
   - **NOAA-20 → S-NPP**: a per-month factor fit on the years both flew (2018–2025). Months after S-NPP coverage ends use it.
4. **Uncertainty**: every converted month carries a 10–90% band, set from the leave-one-year-out errors.

### How well it works (real FIRMS data, out of sample)
| | California | Gran Chaco (AR) |
|---|---|---|
| Annual error, leave-one-year-out 2012–2019 | **6.6%** (ratio baseline 8.1%) | **3.6%** (baseline 3.9%) |
| Annual error, spatial holdout (fit on half the grid cells, test on the other half) | 7.4% | 2.6% |
| Drift stress test, 2020–2025: mean bias of MODIS-predicted VIIRS | −6.5% | −1.8% |
| Share of drift-year months inside the 10–90% band (nominal 80%) | 74% | 67% |
| NOAA-20 → S-NPP bridge, annual error (leave-one-year-out 2019–2025) | 2.3% | 2.8% |
| Same months 2012–2019: VIIRS ÷ MODIS fire cell-days | 1.95× | 2.08× |

These are live on the app's validation panel and in `data/harmonized/validation.json`. The drift test is why the conversion is fit on pre-drift years only: late-mission MODIS sees fewer fires because its overpass has moved away from the afternoon fire peak.

## Run it
```bash
git clone https://github.com/1inandy/firex.git && cd firex
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env                 # optional: FIRMS_MAP_KEY enables the live "this month" view

python -m pipeline.fetch --to 2025   # real FIRMS yearly country files, clipped to each region (~2 GB downloaded, ~80 MB kept; no key)
python -m pipeline.run data/raw      # clean -> grid -> harmonize -> events -> validate (~1 min)
python -m pipeline.figures           # pitch figures -> data/figures/*.svg
python -m pytest -q

uvicorn index:app --reload           # app + API on http://localhost:8000 (API under /api)
```
Regions live in `pipeline/config.py` (`REGIONS`: bbox + FIRMS country file). Adding one means adding an entry and re-running fetch and run. All science settings are in that file too.

**Deploy (Vercel):** `vercel deploy --prod` from the repo root. `index.py` is the entrypoint, `.vercelignore` uploads only the app and the built `data/` (~65 MB), and `requirements.txt` is the runtime set. Set `FIRMS_MAP_KEY` in the project's environment variables for the live month.

## API
Contract: [docs/api.md](docs/api.md). Areas can be a rectangle, polygon or circle (`bbox=`, `poly=lon,lat;…`, `circle=lon,lat,km`) on every endpoint. Calendar, map and current-month use the 0.05° grid (a cell counts if its centre is inside the shape). The hotspot CSV uses exact detection positions.

## Data and credits
NASA FIRMS: MODIS Collection 6.1 (MCD14ML), VIIRS 375 m S-NPP and NOAA-20 (Collection 2), from the [yearly country files](https://firms.modaps.eosdis.nasa.gov/country/) and the [area API](https://firms.modaps.eosdis.nasa.gov/api/area/). We acknowledge the use of data and imagery from LANCE FIRMS operated by NASA's Earth Science Data and Information System (ESDIS). Map tiles © OpenStreetMap contributors.

Background reading: Giglio et al. 2016 (MODIS C6 active fire, [10.1016/j.rse.2016.02.054](https://doi.org/10.1016/j.rse.2016.02.054)); Schroeder et al. 2014 (VIIRS 375 m, [10.1016/j.rse.2013.12.008](https://doi.org/10.1016/j.rse.2013.12.008)); Li et al. 2018 (VIIRS vs MODIS FRP, [10.1029/2017JD027823](https://doi.org/10.1029/2017JD027823)); Vadrevu & Lasko 2018 (VIIRS ≈ 4.8× MODIS fire counts in cropland, [10.3390/rs10070978](https://doi.org/10.3390/rs10070978)); Giglio et al. 2006 (Terra vs Aqua diurnal sampling, [10.1029/2005JG000142](https://doi.org/10.1029/2005JG000142)); Zheng et al. 2026 (MYD14/VNP14IMG FRP fusion, [10.3390/rs18101650](https://doi.org/10.3390/rs18101650)).
