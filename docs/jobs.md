# Starter jobs for the Axis board

The first person to connect Axis should ask their agent:
> "Post each job in docs/jobs.md to the Axis board with its priority and dependencies."

| # | Job | Area | Depends on | Priority |
|---|---|---|---|---|
| 1 | Download FIRMS archive (MODIS SP + VIIRS SNPP SP + NOAA-20 SP) for demo region(s) into `data/raw/` | data | — | high |
| 2 | Finish `pipeline/clean.py`: confidence thresholds, keep `type==0`, drop duplicate swath-edge detections | data | — | high |
| 3 | Finish `pipeline/grid.py`: snap to grid, aggregate fire cell-days → `data/grid/` Parquet | data | 2 | high |
| 4 | `pipeline/harmonize.py` baseline: per region×month VIIRS/MODIS ratio from Aqua MODIS vs SNPP VIIRS overlap (2012→) | science | 3 | high |
| 5 | Harmonization validation: train 2012–2020, test 2021→, produce error chart for the pitch | science | 4 | high |
| 6 | Backend `/calendar` reading harmonized Parquet with DuckDB (bbox + date range) | backend | 3 | high |
| 7 | Backend `/current`: FIRMS NRT for last N days vs historical median | backend | 6 | medium |
| 8 | Backend `/csv`: hotspot export for bbox + date range | backend | 3 | medium |
| 9 | Frontend: map + draw rectangle/polygon + lat/lon input, URL state | frontend | — | high |
| 10 | Frontend: burning calendar heatmap (year × month), counts/anomaly toggle, accessible ramps | frontend | 6 | high |
| 11 | Frontend: "this month vs history" card + seasonality chart | frontend | 7 | medium |
| 12 | Stretch: cluster hotspots into fire events (DBSCAN) + precedent cards | science | 3 | low |
| 13 | Stretch: grounded AI briefing (numbers verified against computed stats) | backend | 6 | low |
| 14 | Pitch deck + demo video | story | 10 | medium |
