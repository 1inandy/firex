# Plume: Harmonized Burning Calendar
*NASA Space Apps 2026: "Harmonization of MODIS and VIIRS Hot Spots" (Advanced/Intermediate)*
*Inspired by ContainOS (TreeHacks 2026 winner, OpenAI AI track 1st place)*

## The idea in one line
ContainOS showed what responders need in the first hours of a fire. We give them the **memory**: 25 years of satellite fire history merged into one consistent record, so every area (and every new fire) comes with its past.

## What the challenge actually requires (score against this first!)
The brief splits into two parts:
1. **Harmonize MODIS + VIIRS hotspots** so "the sensor of origin no longer matters".
2. **Web app** built on the harmonized data:
   - User selects an area (draw on a map or type lat/lon) and a date range
   - **Burning calendar**: hotspot counts by month × year, early 2000s → now
   - Compare the **current month vs. the historical median**
   - Highlight peak months/years, anomalies, trends
   - **Accessible color ramps**
   - **Download a CSV** of hotspots plus analytics and figures

Audience: early-warning and emergency responders, scientists, land managers, firefighters.

> Rule: the ContainOS-inspired features are stretch goals. A working, honest harmonization + calendar beats a flashy AI layer on a weak core.

## Why now (pitch ammo)
- MODIS has run on Terra (since 2000) and Aqua (since 2002), but both satellites are being retired. NASA projects science end of about Feb 2027 for Terra and Sep 2027 for Aqua, and Terra has been drifting in orbit since 2020.
- VIIRS (on Suomi NPP since 2012, NOAA-20, NOAA-21) is the successor, but it's sharper and detects many more small fires.
- Without harmonization, a fire record plotted across both sensors shows a fake "explosion" in fires after 2012. Fire managers either lose 12 years of history or get misled. **Your tool fixes that.**

## The data
**Source: NASA FIRMS** (Fire Information for Resource Management System)
- Products: MODIS (1 km pixels, 2000→), VIIRS (375 m pixels, SNPP 2012→, NOAA-20 2018→, NOAA-21 newer).
- "SP" (standard processing) = cleaned archive, used for history. "NRT" (near real-time) = the last few days, used for the "this month" view.
- Each hotspot is a row with lat, lon, date and time, satellite, confidence, FRP (fire radiative power, i.e. how intense), day/night, and in archives a **type** field (vegetation fire vs. volcano vs. static industrial source vs. offshore).

**Getting it:**
- **Free MAP_KEY** from the FIRMS API page. Get it before the event.
- **The area API** (`/api/area/csv/{MAP_KEY}/{SOURCE}/{bbox}/{days}/{date}`) is good for recent data and live views. Only a few days per request, and a limit of roughly 5,000 transactions per 10 min, so it is **not** the way to pull 25 years.
- **Bulk history:** use the FIRMS **archive download** (request by region and date range; files are emailed) or the pre-made **yearly country CSVs**. Pull these **before the hackathon** for your demo region(s); they can take hours to arrive.
- Check the exact source names (e.g. `MODIS_SP`, `VIIRS_SNPP_SP`, `VIIRS_NOAA20_NRT`) on the FIRMS API page. Some of these I haven't confirmed.

## The core science: how to harmonize (this is what judges will dig into)

**Step 1: Clean**
- Keep only vegetation and land fires (archive `type == 0`). Drop gas flares, volcanoes, factories and offshore platforms, which otherwise show up as "fires" every single day.
- Apply confidence thresholds (MODIS ≥ ~30–50; VIIRS "nominal" + "high"). Report your choice.
- **Remove duplicate detections**: neighboring satellite scans overlap at the swath edges (the "bow-tie" effect), so the same fire can be counted twice.

**Step 2: Count the same thing**
- Don't compare raw pixel counts, because one MODIS 1 km pixel ≈ 7 VIIRS 375 m pixels.
- Snap every detection to a **common grid** (e.g. 1 km or 0.05°) and count **"fire cell-days"**: unique grid cells with ≥1 detection on a given day. This removes most of the pixel-size effect right away.

**Step 3: Learn the conversion from the overlap years**
- From 2012 to now, both sensors watched the same fires. **Aqua MODIS and SNPP VIIRS** pass overhead at about the same time of day (~1:30 pm/am), so they're the cleanest pair to compare.
- For each region × land-cover type × month, fit how VIIRS counts relate to MODIS counts:
  - *Baseline:* a simple ratio (VIIRS ÷ MODIS) per group
  - *Better:* quantile mapping (match the distributions)
  - *Best (if time allows):* a small model (e.g. gradient boosting) with land cover, season, FRP and fire-size features
- **Validate honestly**: train on 2012–2020, test on 2021–now, and show the error. A chart titled "our harmonized MODIS predicts real VIIRS within X%" is a slide judges will remember.

**Step 4: Build the series**
- 2000–2011: MODIS converted to "VIIRS-equivalent" units, with an uncertainty band.
- 2012→now: VIIRS (or a blend in the overlap).
- Flag known issues: Terra/Aqua orbit drift changes their overpass time, and fires burn hotter in the afternoon, so late-mission MODIS sees a different part of the daily fire cycle.

Further reading: Li et al. 2018 (JGR) compares MODIS vs. VIIRS fire intensity during the overlap; the FIRMS blog explains how sensor resolution changes daily fire extent; and a 2026 *Remote Sensing* paper describes a MODIS-VIIRS fusion method.

## The web app

**Core views (MVP)**
1. **Map + area picker:** draw a polygon or rectangle, or type lat/lon + radius. Quick-pick buttons for preset regions.
2. **Burning calendar heatmap:** rows = years (2001→2026), columns = months, color = harmonized hotspot count. Toggle between **"counts"** and **"anomaly vs. median"**. Use accessible ramps: a sequential colorblind-safe ramp (e.g. viridis/cividis) for counts, and a diverging ramp for anomalies.
3. **"This month vs. history" card:** "November 2026: 340 fire cell-days. Historical median: 120. **2.8× normal, highest since 2020.**"
4. **Seasonality chart:** typical year (median + 10–90% band) with the current year drawn on top.
5. **Downloads:** hotspot CSV for the area and date range, plus PNG figures.

**ContainOS-inspired layer (stretch, the differentiator)**
6. **Fire events, not just dots:** group hotspots that are close in space and time (DBSCAN, a clustering method) into individual fires, each with start date, duration, area and peak intensity.
7. **Precedent cards:** when a new fire appears in the NRT feed, show "In this area, fires starting in November historically burned a median of 4 days; the largest was X (2017)."
8. **Grounded AI briefing:** an LLM writes a plain-language summary for a fire manager, but **may only use numbers the app computed**. Like ContainOS's physics check, every number in the text is verified against the data, and anything unverifiable is removed or marked "unknown".
9. **Alerts:** a subscription that texts or emails when an area exceeds e.g. 2× its historical median.

## Architecture
```
Before the hackathon:
  FIRMS archive CSVs ──► Python cleaning + gridding ──► harmonization model
                                       │
                                       ▼
                     Parquet files: (grid_cell, date, sensor, counts, FRP)

App:
  Frontend (React + Leaflet/MapLibre + leaflet-draw + Plotly/ECharts)
        │  ?lat=..&lon=..&r=..&from=..&to=..      ← shareable URL
        ▼
  Backend (Python FastAPI)
        ├─ DuckDB queries the Parquet files directly (fast, no database server)
        ├─ /calendar   → month×year harmonized counts + anomalies
        ├─ /current    → FIRMS NRT API for the latest days → vs. median
        ├─ /csv        → hotspot export
        ├─ /events     → clustered fire events (stretch)
        └─ /brief      → grounded LLM summary (stretch)
```
Why DuckDB + Parquet: millions of hotspot rows queried by area in under a second, on a laptop, with no setup.

## Scope: pick 1–2 demo regions
Global is too much for 48 hours. Good choices:
- **California**: big recent fires (2018 Camp, 2020, 2025 LA), lots of public interest, and it connects to ContainOS/CalFire.
- **Argentina or Brazil**: lots of agricultural burning with strong seasonal patterns. The challenge's photo is from CONAE (Argentina's space agency).
- **Your own region**, if it burns. A personal connection makes the pitch.
The pipeline should still work anywhere. Precompute only the demo regions.

## Build order (48h)
| Hours | Goal |
|---|---|
| Pre-event | MAP_KEY, archive downloads for demo regions, prototype the cleaning script |
| 0–8 | Cleaning + gridding pipeline → Parquet. Skeleton map app |
| 8–20 | Harmonization (ratio baseline first) + validation chart. Calendar heatmap working end-to-end |
| 20–30 | Anomaly view, current month vs. median, CSV export, area drawing |
| 30–40 | Stretch: fire events + precedent cards, grounded AI briefing |
| 40–48 | Polish, accessibility check, demo video, slides. **Freeze features at hour 40** |

## Team split (4 people)
- **Data/science:** cleaning, gridding, harmonization model, validation (the core).
- **Backend:** FastAPI + DuckDB, FIRMS NRT integration, CSV export.
- **Frontend:** map, area drawing, calendar heatmap, charts, accessible colors.
- **Product/story:** user research (talk to a firefighter or land manager if at all possible, as ContainOS did with CalFire), pitch, demo video, AI briefing prompt + guardrails.

## Pitch outline (3 min)
1. **Hook:** "Every fire season, responders ask: is this normal? The satellites that answered that question for 25 years are being shut off."
2. **Problem:** The MODIS → VIIRS handoff breaks the record. Plot them together and fire seems to triple overnight in 2012.
3. **Our fix:** Harmonization, shown in one before/after chart plus our validation accuracy.
4. **Demo:** Draw a box over a region, see the calendar light up, and see "this month is 2.8× normal." Download the CSV. (Stretch: a new fire appears and its precedent card plus briefing pop up.)
5. **Impact:** Pre-position crews in the right months, spot abnormal seasons early, and keep 25 years of fire history usable after MODIS goes dark.

## Prep checklist (before the event)
- [ ] Get a FIRMS MAP_KEY
- [ ] Request archive downloads (MODIS SP + VIIRS SNPP SP, plus NOAA-20) for the demo regions
- [ ] Read the FIRMS FAQ on confidence, `type` and duplicates
- [ ] Skim Li et al. 2018 and the 2026 *Remote Sensing* fusion paper
- [ ] Try to get 15 minutes with a firefighter, land manager or emergency manager to learn what they'd actually check
- [ ] Look at ContainOS's GitHub repo for its UI and alert ideas (credit it, don't copy)

## Key links
- Challenge: https://www.spaceappschallenge.org/2026/challenges/modis-and-viirs-hot-spots/
- FIRMS API: https://firms.modaps.eosdis.nasa.gov/api
- FIRMS area API: https://firms.modaps.eosdis.nasa.gov/api/area/csv
- FIRMS blog on sensor differences: https://wiki.earthdata.nasa.gov/spaces/FIRMS/blog/2022/09/29/266967967/Characteristics+of+VIIRS+MODIS+and+OLI+Sensors+and+Their+Effects+on+the+Spatial+Extent+of+Daily+Active+Fire+Data
- MODIS → VIIRS transition (NASA): https://www.earthdata.nasa.gov/data/alerts-outages/transition-from-modis-viirs
- Terra end of an era: https://science.nasa.gov/science-research/earth-science/terra-the-end-of-an-era
- Li et al. 2018 (MODIS vs. VIIRS FRP): https://repository.library.noaa.gov/view/noaa/49896
- 2026 MODIS–VIIRS fusion paper: https://mdpi-res.com/d_attachment/remotesensing/remotesensing-18-01650/article_deploy/remotesensing-18-01650.pdf
- ContainOS: https://devpost.com/software/containos
