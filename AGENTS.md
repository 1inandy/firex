# Agent instructions (Claude Code, Cursor, Codex, Copilot, Gemini…)

Read [docs/plan.md](docs/plan.md) first. Several people work on this repo at once, so keep changes small and on a branch.

## Git
- Never commit straight to `main`. Use a branch per change, then open a PR.
- Small PRs. Pull/rebase on `main` before opening one.
- Never commit `data/` contents or `.env` (both gitignored).

## Project map
| Path | What lives there | Owner area |
|---|---|---|
| `docs/plan.md` | Full project plan: science, architecture, build order. **Read first.** | everyone |
| `pipeline/` | FIRMS download → clean → grid → harmonize → Parquet | data/science |
| `backend/` | FastAPI + DuckDB API over the Parquet files | backend |
| `frontend/` | Map, area picker, burning calendar, charts | frontend |
| `data/` | Local data (gitignored) | — |

## Shared contracts (change only via a PR that says so)
**Gridded Parquet** `data/grid/*.parquet`: one row per (cell, day, sensor):
`cell_id: str, lat: float, lon: float, date: date, sensor: str ("MODIS"|"VIIRS_SNPP"|"VIIRS_NOAA20"|"VIIRS_NOAA21"), n_detections: int, frp_sum: float, frp_max: float`

**Monthly cell-days** `data/grid/monthly.parquet`: `cell_id, lat, lon, year, month, family ("MODIS"|"VIIRS"), fire_cell_days`

**Conversion ratios** `data/harmonized/ratios.parquet`: `month, ratio, lo, hi` (VIIRS ÷ MODIS, fitted on the overlap years)

**Harmonized series** (computed per request by `pipeline.harmonize.harmonize`): `year, month, fire_cell_days, lo, hi, source ("viirs"|"modis_converted")`

**API** (see `backend/app/main.py`): `GET /calendar?bbox=W,S,E,N&from=YYYY-MM&to=YYYY-MM` →
`{"months":[{"year":2001,"month":1,"value":12.0,"lo":9.1,"hi":15.2,"anomaly":0.4}], "median_by_month":{...}}`

## Checks before opening a PR
```bash
python -m pytest -q        # from the repo root: pipeline + API tests
```

## Conventions
- Python 3.11+, type hints, `ruff` formatting. Keep functions small and pure where possible; I/O at the edges.
- Fire science choices (confidence thresholds, grid size, which hotspot types are kept) live in `pipeline/config.py`, not hard-coded.
- Colors: colorblind-safe ramps only (viridis/cividis sequential, a diverging ramp for anomalies).
