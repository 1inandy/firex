# Agent instructions (Claude Code, Cursor, Codex, Copilot, Gemini…)

This repo is worked on by **several developers running different coding agents at the same time**.
We coordinate through **Axis** (https://github.com/VirSanghavi/axis): a shared job board plus per-file locks, served over MCP at `https://useaxis.dev/api/mcp`.
The board for this repo is pinned by `.axis/axis.json` (project `firex`).

## The protocol: follow it every session

1. **Start:** read the shared context/notepad (`update_shared_context` / `mcp://context/current`) to see what others are doing.
2. **Get work:** call `claim_next_job`. Don't invent work that isn't on the board; if something is missing, `post_job` it first (see `docs/jobs.md` for the starter list).
3. **Before editing ANY file:** call `propose_file_access` for that file.
   - `GRANTED` → edit it.
   - `REQUIRES_ORCHESTRATION` → someone else holds it. **Do not edit.** Pick another file or job, or leave a note in the shared context.
   - Never use `force_unlock` unless the holder has clearly crashed (locks auto-expire after 30 min).
4. **While working:** post short progress notes to the shared notepad, especially interface decisions (API shapes, column names, file formats) that other agents depend on.
5. **Finish:** run the checks below, commit on a branch, then `complete_job` with a one-line outcome. Release locks.

If the Axis MCP server isn't connected, **stop and tell your human** instead of editing blind.

## Git
- Never commit straight to `main`. Use a branch per job: `job/<short-name>`, then open a PR.
- Small PRs. Pull/rebase on `main` before opening one.
- Never commit `data/` contents or `.env` (both gitignored).

## Project map
| Path | What lives there | Owner area |
|---|---|---|
| `docs/plan.md` | Full project plan: science, architecture, build order. **Read first.** | everyone |
| `docs/jobs.md` | Starter job list to seed the Axis board | everyone |
| `pipeline/` | FIRMS download → clean → grid → harmonize → Parquet | data/science |
| `backend/` | FastAPI + DuckDB API over the Parquet files | backend |
| `frontend/` | Map, area picker, burning calendar, charts | frontend |
| `data/` | Local data (gitignored) | — |

## Shared contracts (change only via a job + a notepad note)
**Gridded Parquet** `data/grid/*.parquet`: one row per (cell, day, sensor):
`cell_id: str, lat: float, lon: float, date: date, sensor: str ("MODIS"|"VIIRS_SNPP"|"VIIRS_NOAA20"|"VIIRS_NOAA21"), n_detections: int, frp_sum: float, frp_max: float`

**Monthly cell-days** `data/grid/monthly.parquet`: `cell_id, lat, lon, year, month, family ("MODIS"|"VIIRS"), fire_cell_days`

**Conversion ratios** `data/harmonized/ratios.parquet`: `month, ratio, lo, hi` (VIIRS ÷ MODIS, fitted on the overlap years)

**Harmonized series** (computed per request by `pipeline.harmonize.harmonize`): `year, month, fire_cell_days, lo, hi, source ("viirs"|"modis_converted")`

**API** (see `backend/app/main.py`): `GET /calendar?bbox=W,S,E,N&from=YYYY-MM&to=YYYY-MM` →
`{"months":[{"year":2001,"month":1,"value":12.0,"lo":9.1,"hi":15.2,"anomaly":0.4}], "median_by_month":{...}}`

## Checks before completing a job
```bash
python -m pytest -q        # from the repo root: pipeline + API tests
```

## Conventions
- Python 3.11+, type hints, `ruff` formatting. Keep functions small and pure where possible; I/O at the edges.
- Fire science choices (confidence thresholds, grid size, which hotspot types are kept) live in `pipeline/config.py`, not hard-coded.
- Colors: colorblind-safe ramps only (viridis/cividis sequential, a diverging ramp for anomalies).
