"""Tunable science choices. Change here, not inline, and note changes on the Axis notepad."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
RAW = DATA / "raw"
GRID = DATA / "grid"
HARMONIZED = DATA / "harmonized"

# Grid cell size in degrees (~0.05 deg ≈ 5.5 km at the equator). Smaller = closer to VIIRS detail.
GRID_DEG = 0.05

# MODIS confidence is 0-100; VIIRS is l/n/h.
MODIS_MIN_CONFIDENCE = 30
VIIRS_KEEP_CONFIDENCE = {"n", "h", "nominal", "high"}

# FIRMS archive `type`: 0 vegetation fire, 1 active volcano, 2 other static land source, 3 offshore.
KEEP_TYPES = {0}

# Years where both Aqua MODIS and SNPP VIIRS are available (used to learn the conversion).
OVERLAP_START_YEAR = 2012
