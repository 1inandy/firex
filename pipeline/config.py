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
# Conversion method: "frp_strata" (default), "quantile" or "ratio" (baseline).
#  ratio:      VIIRS = r_month × MODIS
#  frp_strata: VIIRS = a_month × MODIS_weak + b_month × MODIS_strong, where a MODIS cell-day is "strong"
#              if its max FRP ≥ STRONG_FRP_MW. VIIRS sees far more extra pixels around weak/small fires
#              than around big ones, so a season dominated by big wildfires has a lower VIIRS÷MODIS ratio.
#              Splitting by intensity lets the conversion follow each year's fire mix.
#  quantile:   per calendar month, map each cell's monthly MODIS cell-day count k onto the VIIRS distribution
#              (empirical quantile mapping of non-zero cell-months), times the ratio of active VIIRS to
#              MODIS cell-months. Works per cell, so it transfers from a region to any drawn area.
HARMONIZE_METHOD = "frp_strata"
STRONG_FRP_MW = 30.0
HARMONIZE_METHODS = ("ratio", "frp_strata", "quantile")

# Quantile mapping: a cell has at most 31 fire days a month. Months with fewer non-zero cell-months than
# QM_MIN_SAMPLES (either sensor) use the curve pooled over all months.
QM_MAX_K = 31
QM_MIN_SAMPLES = 30

# Out-of-sample validation split: fit ratios on [OVERLAP_START_YEAR, VALIDATION_TRAIN_END], test after.
VALIDATION_TRAIN_END = 2020

# MODIS orbit drift: Terra (since ~2020) and Aqua (since ~2022) have been drifting to earlier overpass
# times, so late-mission MODIS samples a cooler part of the daily fire cycle. Overlap years from here on
# are flagged in the validation (they are also the held-out test years).
MODIS_DRIFT_FROM = {"Terra": 2020, "Aqua": 2022}

# Alerts: notify when an area's latest month reaches this multiple of its historical median.
ALERTS_FILE = DATA / "alerts.json"
ALERT_DEFAULT_THRESHOLD = 2.0
ALERT_MIN_CELL_DAYS = 5  # ignore "3× normal" when that means 3 cell-days instead of 1

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
