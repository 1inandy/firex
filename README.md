# 🔥 firex: a harmonized burning-activity calendar

*NASA Space Apps 2026: [Harmonization of MODIS and VIIRS Hot Spots](https://www.spaceappschallenge.org/2026/challenges/modis-and-viirs-hot-spots/)*

MODIS has recorded fire hotspots since 2000, but its satellites are being retired. VIIRS replaces it, but it sees many more small fires, so the two records can't be compared directly.
**firex** merges them into one consistent 25-year record and turns it into a burning calendar for any area you pick: which months and years burned most, and how *this* month compares with history.

Full plan: [docs/plan.md](docs/plan.md)

## Team setup (5 minutes)

### 1. Clone + env
```bash
git clone https://github.com/1inandy/firex.git
cd firex
cp .env.example .env        # paste your FIRMS key: https://firms.modaps.eosdis.nasa.gov/api/map_key
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Connect your agent to Axis (shared job board + file locks)
We use [Axis](https://github.com/VirSanghavi/axis) so everyone's agents (Claude Code, Cursor, Codex…) don't overwrite each other.

1. Make an account at **https://useaxis.dev**. One person creates an **org** (dashboard → org switcher → *+ new org*) and invites the others from **/team**.
2. The org admin puts the org id in `.axis/axis.json` and commits it:
   ```json
   { "project": "firex", "org": "<org id from useaxis.dev/team>" }
   ```
   Until then, each person's agents land on their own personal board.
3. Connect your agent. The repo already includes the config:
   - **Claude Code:** `.mcp.json` is checked in. Run `claude`, approve the `axis` server, then `/mcp` → axis → *Authenticate*.
   - **Cursor:** `.cursor/mcp.json` is checked in. Click *Needs login* on the axis server in Settings → MCP.
   - **VS Code Copilot:** `.vscode/mcp.json` is checked in.
   - **Codex / others:** add the HTTP MCP server `https://useaxis.dev/api/mcp` (see the [Axis setup docs](https://github.com/VirSanghavi/axis/blob/main/agent-instructions/mcp-setup.md)).
4. First person in: tell your agent *"Post each job in docs/jobs.md to the Axis board."*
5. Everyone: tell your agent *"Claim the next job."* Agents follow [AGENTS.md](AGENTS.md).

Live board: https://useaxis.dev/team/board

### 3. Run things
```bash
# no FIRMS data yet? build a clearly-labelled synthetic FIRMS-format dataset (California + Gran Chaco)
python -m pipeline.demo                  # -> data/raw/demo/*.csv.gz (~5 s)
python -m pipeline.run data/raw/demo     # clean -> grid -> harmonize -> events -> validate (~5 s)
# real data: drop FIRMS archive CSVs (MODIS SP + VIIRS SNPP SP) in data/raw/<region>/ and
python -m pipeline.run data/raw/<region>
# pitch figures (before/after harmonization, held-out validation) -> data/figures/*.svg
python -m pipeline.figures
# tests
python -m pytest -q
# API (http://localhost:8000/docs)
uvicorn backend.app.main:app --reload
# frontend (http://localhost:5500)
python -m http.server 5500 -d frontend
```

## Repo layout
```
pipeline/   FIRMS download → clean → grid → harmonize (Python)
backend/    FastAPI + DuckDB over Parquet
frontend/   Map + burning calendar (starts as a static Leaflet page)
docs/       plan.md (the full plan), jobs.md (starter Axis jobs)
data/       local data, gitignored
```

## Credits
Data: NASA FIRMS (MODIS C6.1, VIIRS 375 m). Response-copilot ideas inspired by ContainOS (TreeHacks 2026).

## How the harmonization works
1. **Clean** (`pipeline/clean.py`): keep vegetation fires (`type == 0`), MODIS confidence ≥ 30, VIIRS nominal/high; drop exact duplicates and swath-edge "bow-tie" repeats (same satellite + overpass, closer than one pixel).
2. **Grid** (`pipeline/grid.py`): snap to 0.05° cells and count **fire cell-days** (cells with ≥ 1 detection per day), per sensor family. The VIIRS family is Suomi NPP only, so adding NOAA-20/21 later doesn't inflate the record.
3. **Convert** (`pipeline/harmonize.py`): on the overlap years (2012→) fit, per region and calendar month,
   `VIIRS ≈ a·MODIS_weak + b·MODIS_strong` (strong = cell-day max FRP ≥ 30 MW). VIIRS adds far more detections around small/weak fires than around big ones, so this follows each year's fire mix; the plain `VIIRS ÷ MODIS` ratio is kept as the baseline.
   An alternative, `quantile`, does per-cell quantile mapping: a cell with *k* MODIS fire days in a month maps to the VIIRS count at the same percentile, scaled by how many more cells VIIRS lights up. Set `HARMONIZE_METHOD` in `pipeline/config.py`.
4. **Validate**: fit on 2012–2020, predict 2021→ from MODIS alone, compare with real VIIRS (`/validation`, `data/figures/*_validation.svg`). Every method is scored on the same held-out years. Those years are also MODIS orbit-drift years (Terra ~2020→, Aqua ~2022→), which the UI flags.
5. **Series**: 2001–2011 = converted MODIS with a 10–90% band; 2012→ = VIIRS.

The calendar's **Raw sensors** view shows the un-harmonized record (MODIS before 2012, VIIRS after) so the fake jump at the handoff is visible next to the fix.

**Areas** can be a rectangle, polygon or circle (`bbox=`, `poly=lon,lat;…`, `circle=lon,lat,km` on every endpoint); only cells, hotspots and events inside the shape are counted.

**Alerts**: subscribe an area at N× its historical median from the Alerts card. Run `python -m backend.app.alerts` from cron after each data refresh (email via `SMTP_*` in `.env`, or an https webhook per alert; otherwise logged). Subscriptions are stored in `data/alerts.json`.

Settings live in `pipeline/config.py`. API contract: [docs/api.md](docs/api.md). `/brief` uses Claude when `ANTHROPIC_API_KEY` is set (template otherwise); every number in it is checked against the computed facts.

> **Demo data is synthetic.** `pipeline.demo` simulates FIRMS-format detections so the app runs end to end; the UI shows a banner whenever it's in use. Validation numbers on synthetic data say the code works, not that the method does: rerun on real FIRMS archives before quoting them.
