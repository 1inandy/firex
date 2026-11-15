"""Tunable science choices. Change here, not inline, and note the change in the PR description."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
RAW = DATA / "raw"
GRID = DATA / "grid"
HARMONIZED = DATA / "harmonized"
META = DATA / "meta.json"

# Grid cell size in degrees (~0.05 deg ≈ 5.5 km at the equator). Smaller = closer to VIIRS detail.
GRID_DEG = 0.05

# MODIS confidence is 0-100; VIIRS is l/n/h.
MODIS_MIN_CONFIDENCE = 30
VIIRS_KEEP_CONFIDENCE = {"n", "h", "nominal", "high"}

# FIRMS archive `type`: 0 vegetation fire, 1 active volcano, 2 other static land source, 3 offshore.
KEEP_TYPES = {0}

# Swath-edge ("bow-tie") duplicates: same satellite and overpass, closer than one pixel.
# Distances in km; overpass = same satellite, date and day/night within DEDUPE_MINUTES.
DEDUPE_KM = {"MODIS": 1.0, "VIIRS": 0.375}
DEDUPE_MINUTES = 6

# Years where both Aqua MODIS and SNPP VIIRS are available (used to learn the conversion).
OVERLAP_START_YEAR = 2012
# Conversion method: "frp_strata" (default) or "ratio" (baseline).
#  ratio:      VIIRS = r_month × MODIS
#  frp_strata: VIIRS = a_month × MODIS_weak + b_month × MODIS_strong, where a MODIS cell-day is "strong"
#              if its max FRP ≥ STRONG_FRP_MW. VIIRS sees far more extra pixels around weak/small fires
#              than around big ones, so a season dominated by big wildfires has a lower VIIRS÷MODIS ratio.
#              Splitting by intensity lets the conversion follow each year's fire mix.
HARMONIZE_METHOD = "frp_strata"
STRONG_FRP_MW = 30.0

# Out-of-sample validation split: fit ratios on [OVERLAP_START_YEAR, VALIDATION_TRAIN_END], test after.
VALIDATION_TRAIN_END = 2020

# Which VIIRS satellites make up the "VIIRS" family for the harmonized series. Using only SNPP keeps
# the record stationary: adding NOAA-20 (2018) and NOAA-21 (2024) would raise counts because more
# overpasses see more short-lived fires, not because more burned.
VIIRS_FAMILY_SENSORS = {"VIIRS_SNPP"}

# Fire events: grid cell-days are linked if their cells touch (8-neighbourhood) and they are at most
# EVENT_GAP_DAYS apart. Events smaller than EVENT_MIN_CELL_DAYS are dropped from event tables.
EVENT_GAP_DAYS = 2
EVENT_MIN_CELL_DAYS = 3

# Regions with their own VIIRS/MODIS ratios (fit on all cells inside the bbox). A user-drawn area uses
# the region containing its centre, falling back to ratios fit on all data ("global").
REGIONS = {
    "california": {"name": "California", "bbox": (-124.5, 32.5, -114.0, 42.0)},
    "chaco": {"name": "Gran Chaco & Paraná Delta (AR)", "bbox": (-66.0, -34.0, -57.0, -22.0)},
}
