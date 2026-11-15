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
# pipeline tests
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
