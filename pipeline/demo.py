"""Synthetic FIRMS-format hotspot archives for demos and tests (no MAP_KEY needed).

    python -m pipeline.demo            # writes data/raw/demo/*.csv.gz
    python -m pipeline.run data/raw/demo

The output follows the FIRMS archive CSV schema (MODIS C6.1 and VIIRS 375 m) closely enough that the
real pipeline runs on it unchanged. The physics are deliberately simple but keep the properties the
harmonization has to deal with:

* VIIRS (375 m) detects many small, low-FRP fires MODIS (1 km) misses, so VIIRS counts are higher;
  the gap depends on the season's fire mix (small agricultural burns vs. big wildfires).
* Sensors come and go on their real schedule: Terra 2000-11, Aqua 2002-07, SNPP 2012-01,
  NOAA-20 2018-01, NOAA-21 2024-01. The last month is partial (stops "today").
* Noise the cleaning step must remove: static industrial sources (type 2), low-confidence pixels,
  and duplicated swath-edge ("bow-tie") detections.

A marker file `SYNTHETIC` is written next to the CSVs; the pipeline records it in data/meta.json and
the UI shows a banner. Never present these numbers as real.
"""
from __future__ import annotations

import datetime as dt
import gzip
import sys
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

from . import config

START = dt.date(2001, 1, 1)
SENSOR_START = {
    ("MODIS", "Terra"): dt.date(2000, 11, 1),
    ("MODIS", "Aqua"): dt.date(2002, 7, 4),
    ("VIIRS", "N"): dt.date(2012, 1, 20),
    ("VIIRS", "N20"): dt.date(2018, 1, 5),
    ("VIIRS", "N21"): dt.date(2024, 1, 17),
}
# Local solar overpass hour (day pass; night pass is +12h).
OVERPASS_HOUR = {"Terra": 10.5, "Aqua": 13.5, "N": 13.5, "N20": 12.7, "N21": 13.1}


@dataclass
class Zone:
    lat: float
    lon: float
    sd: float          # degrees, spatial spread of ignition sites
    weight: float      # share of the region's ignitions


@dataclass
class Region:
    id: str
    small_per_year: float               # small/agricultural burns (single spot-days)
    wild_per_year: float                # wildfire events
    small_season: list[float]           # 12 monthly weights
    wild_season: list[float]
    zones: list[Zone]
    small_zones: list[Zone]
    trend_per_decade: float = 0.0       # multiplicative growth of wildfire activity
    static_sources: list[tuple[float, float]] = field(default_factory=list)
    # Scripted big events: (start date, centre lat, centre lon, spot-days, duration days)
    scripted: list[tuple[str, float, float, int, int]] = field(default_factory=list)


REGIONS = [
    Region(
        id="california",
        small_per_year=2500, wild_per_year=110,
        small_season=[2, 2, 3, 4, 5, 6, 6, 6, 6, 8, 7, 3],
        wild_season=[0.2, 0.2, 0.3, 0.5, 1.2, 3, 6, 8, 7, 4, 1.2, 0.4],
        zones=[Zone(40.4, -122.6, 0.7, 3), Zone(38.7, -120.6, 0.6, 3), Zone(36.6, -121.2, 0.5, 1.5),
               Zone(34.4, -118.3, 0.5, 2.5), Zone(41.3, -120.8, 0.6, 1.5), Zone(33.2, -116.8, 0.4, 1)],
        small_zones=[Zone(37.2, -120.3, 0.8, 3), Zone(39.4, -121.9, 0.4, 1.5), Zone(35.5, -119.3, 0.4, 1.5)],
        trend_per_decade=0.25,
        static_sources=[(38.03, -122.13), (33.80, -118.23), (35.38, -119.02)],
        scripted=[
            ("2008-06-21", 39.8, -121.6, 4000, 40),
            ("2017-10-08", 38.5, -122.6, 2500, 18),
            ("2018-07-27", 39.2, -122.8, 9000, 45),
            ("2018-11-08", 39.8, -121.5, 4500, 17),
            ("2020-08-16", 39.0, -122.3, 14000, 60),
            ("2020-08-18", 37.2, -119.3, 7000, 70),
            ("2021-07-14", 40.1, -121.1, 11000, 90),
            ("2025-01-07", 34.1, -118.5, 2500, 24),
            ("2026-11-02", 34.6, -118.6, 900, 25),
        ],
    ),
    Region(
        id="chaco",
        small_per_year=9000, wild_per_year=60,
        small_season=[2, 2, 2, 3, 5, 8, 12, 16, 14, 8, 4, 2],
        wild_season=[0.5, 0.5, 0.5, 0.6, 1, 2, 4, 7, 7, 4, 1.5, 0.8],
        zones=[Zone(-24.5, -62.5, 1.2, 3), Zone(-27.0, -60.5, 1.0, 2), Zone(-33.0, -59.8, 0.4, 1.5)],
        small_zones=[Zone(-25.0, -62.0, 1.5, 3), Zone(-29.0, -61.0, 1.2, 2), Zone(-32.8, -60.0, 0.5, 1.5)],
        trend_per_decade=0.1,
        static_sources=[(-32.95, -60.65)],
        scripted=[
            ("2008-04-10", -33.0, -59.9, 5000, 30),
            ("2020-07-25", -33.1, -59.7, 9000, 75),
            ("2022-02-05", -27.6, -57.8, 6000, 35),
        ],
    ),
]


def _days(a: dt.date, b: dt.date) -> int:
    return (b - a).days


def _sample_zone(rng: np.random.Generator, zones: list[Zone], n: int) -> tuple[np.ndarray, np.ndarray]:
    w = np.array([z.weight for z in zones], dtype=float)
    k = rng.choice(len(zones), size=n, p=w / w.sum())
    lat = np.array([z.lat for z in zones])[k] + rng.normal(0, 1, n) * np.array([z.sd for z in zones])[k]
    lon = np.array([z.lon for z in zones])[k] + rng.normal(0, 1, n) * np.array([z.sd for z in zones])[k]
    return lat, lon


def _seasonal_dates(rng: np.random.Generator, n: int, year: int, season: list[float],
                    end: dt.date) -> np.ndarray:
    """n day-offsets (from START) in `year`, distributed by monthly weights, truncated at `end`."""
    month = rng.choice(12, size=n, p=np.array(season) / np.sum(season)) + 1
    first = np.array([_days(START, dt.date(year, m, 1)) for m in range(1, 13)])
    length = np.array([(dt.date(year + (m == 12), m % 12 + 1, 1) - dt.date(year, m, 1)).days
                       for m in range(1, 13)])
    off = first[month - 1] + (rng.random(n) * length[month - 1]).astype(int)
    return off[off <= _days(START, end)]


def spots(region: Region, end: dt.date, rng: np.random.Generator) -> pd.DataFrame:
    """True fire 'spot-days': one burning ~375 m patch on one day, with an FRP."""
    parts = []
    for year in range(START.year, end.year + 1):
        mood = rng.lognormal(0, 0.35)  # wet/dry year
        growth = (1 + region.trend_per_decade) ** ((year - START.year) / 10)
        # Small burns: single spot-days, low FRP.
        n_small = rng.poisson(region.small_per_year * rng.lognormal(0, 0.15))
        off = _seasonal_dates(rng, n_small, year, region.small_season, end)
        lat, lon = _sample_zone(rng, region.small_zones, len(off))
        parts.append(pd.DataFrame({"day": off, "lat": lat, "lon": lon,
                                   "frp": rng.lognormal(1.2, 0.7, len(off))}))
        # Wildfires: heavy-tailed size, spread outward over days.
        n_wild = rng.poisson(region.wild_per_year * mood * growth)
        starts = _seasonal_dates(rng, n_wild, year, region.wild_season, end)
        clat, clon = _sample_zone(rng, region.zones, len(starts))
        size = np.minimum((rng.pareto(1.3, len(starts)) + 1) * 6 * mood, 6000).astype(int)
        for s, la, lo, sz in zip(starts, clat, clon, size):
            parts.append(_event(rng, s, la, lo, sz, max(1, int(np.sqrt(sz) * 1.5)), end))
    for date, la, lo, sz, dur in region.scripted:
        s = _days(START, dt.date.fromisoformat(date))
        if s <= _days(START, end):
            parts.append(_event(rng, s, la, lo, sz, dur, end))
    return pd.concat(parts, ignore_index=True)


def _event(rng, start: int, lat: float, lon: float, size: int, duration: int, end: dt.date) -> pd.DataFrame:
    t = rng.beta(1.3, 2.5, size)                      # most burning early in the event
    day = start + (t * duration).astype(int)
    r = np.sqrt(size) * 0.004 * (0.3 + t)             # perimeter grows with time
    ang = rng.random(size) * 2 * np.pi
    rad = np.abs(rng.normal(0, 1, size)) * r
    lat_s = lat + rad * np.sin(ang)
    lon_s = lon + rad * np.cos(ang) / np.cos(np.radians(lat))
    frp = rng.lognormal(2.6, 1.0, size) * (1 + 2 * (size > 1000))
    keep = day <= _days(START, end)
    return pd.DataFrame({"day": day, "lat": lat_s, "lon": lon_s, "frp": frp})[keep]


def detect(sp: pd.DataFrame, end: dt.date, rng: np.random.Generator) -> pd.DataFrame:
    """Simulate what each satellite overpass reports for the true spots."""
    out = []
    day_date = np.datetime64(START) + sp["day"].to_numpy().astype("timedelta64[D]")
    for (inst, sat), since in SENSOR_START.items():
        live = (sp["day"].to_numpy() >= _days(START, max(since, START)))
        if sat == "Terra":  # Terra's orbit drifts after 2020: slightly earlier overpass, fewer detections
            live &= ~((day_date > np.datetime64("2022-10-01")) & (rng.random(len(sp)) < 0.15))
        s = sp[live]
        if s.empty:
            continue
        for daynight, active_p in (("D", 0.75), ("N", 0.35)):
            # Detection probability rises with FRP; VIIRS' threshold is ~7x lower than MODIS'.
            half = 4.0 if inst == "VIIRS" else 28.0
            p = active_p / (1 + (half / s["frp"].to_numpy()) ** 1.6)
            hit = rng.random(len(s)) < p
            d = s[hit]
            if d.empty:
                continue
            jitter = 0.003 if inst == "VIIRS" else 0.008
            lat = d["lat"].to_numpy() + rng.normal(0, jitter, len(d))
            lon = d["lon"].to_numpy() + rng.normal(0, jitter, len(d))
            hour = OVERPASS_HOUR[sat] + (12 if daynight == "N" else 0) - lon / 15 + rng.normal(0, 0.3, len(d))
            day = d["day"].to_numpy() + np.floor(hour / 24).astype(int)
            hour = np.mod(hour, 24)
            acq_time = (np.floor(hour) * 100 + np.floor((hour % 1) * 60)).astype(int)
            frp = d["frp"].to_numpy() * rng.lognormal(0, 0.3, len(d)) * (2.5 if inst == "MODIS" else 1)
            out.append(pd.DataFrame({"inst": inst, "satellite": sat, "latitude": lat, "longitude": lon,
                                     "day": day, "acq_time": acq_time, "frp": frp, "daynight": daynight}))
    det = pd.concat(out, ignore_index=True)
    return det[det["day"] <= _days(START, end)]


def add_noise(det: pd.DataFrame, region: Region, end: dt.date, rng: np.random.Generator) -> pd.DataFrame:
    det = det.assign(type=0)
    # Swath-edge "bow-tie" duplicates: same overpass, a few hundred metres apart.
    dup = det.sample(frac=0.04, random_state=int(rng.integers(1 << 31)))
    step = np.where(dup["inst"] == "VIIRS", 0.0025, 0.007)
    dup = dup.assign(latitude=dup["latitude"] + step * rng.choice([-1, 1], len(dup)))
    # Static industrial sources: detected on many days of the year.
    statics = []
    n_days = _days(START, end) + 1
    for la, lo in region.static_sources:
        for (inst, sat), since in SENSOR_START.items():
            days = np.arange(max(0, _days(START, since)), n_days)
            days = days[rng.random(len(days)) < (0.5 if inst == "VIIRS" else 0.2)]
            statics.append(pd.DataFrame({"inst": inst, "satellite": sat,
                                         "latitude": la + rng.normal(0, 0.002, len(days)),
                                         "longitude": lo + rng.normal(0, 0.002, len(days)),
                                         "day": days, "acq_time": 2130, "frp": rng.lognormal(2, 0.5, len(days)),
                                         "daynight": "N", "type": 2}))
    return pd.concat([det, dup, *statics], ignore_index=True)


def to_firms(det: pd.DataFrame, rng: np.random.Generator) -> dict[str, pd.DataFrame]:
    """Format as FIRMS archive CSV columns, split by instrument."""
    date = (np.datetime64(START) + det["day"].to_numpy().astype("timedelta64[D]")).astype(str)
    n = len(det)
    is_v = (det["inst"] == "VIIRS").to_numpy()
    frp = det["frp"].to_numpy()
    # Confidence: mostly good; weak fires are more often low confidence.
    weak = rng.random(n) < 0.25 / (1 + frp / 5)
    modis_conf = np.where(weak, rng.integers(0, 30, n), rng.integers(30, 101, n))
    viirs_conf = np.where(weak, "l", np.where(frp > 15, "h", "n"))
    base = pd.DataFrame({
        "latitude": det["latitude"].round(5).to_numpy(), "longitude": det["longitude"].round(5).to_numpy(),
        "acq_date": date, "acq_time": det["acq_time"].to_numpy(), "satellite": det["satellite"].to_numpy(),
        "instrument": det["inst"].to_numpy(), "frp": frp.round(2), "daynight": det["daynight"].to_numpy(),
        "type": det["type"].to_numpy(),
    })
    bright = 300 + 25 * np.log1p(frp) + rng.normal(0, 3, n)
    modis = base[~is_v].assign(brightness=bright[~is_v].round(2), scan=1.0, track=1.0,
                               confidence=modis_conf[~is_v], version="6.1",
                               bright_t31=(bright[~is_v] - 20).round(2))
    viirs = base[is_v].assign(bright_ti4=bright[is_v].round(2), scan=0.4, track=0.4,
                              confidence=viirs_conf[is_v], version=2,
                              bright_ti5=(bright[is_v] - 30).round(2))
    mcols = ["latitude", "longitude", "brightness", "scan", "track", "acq_date", "acq_time", "satellite",
             "instrument", "confidence", "version", "bright_t31", "frp", "daynight", "type"]
    vcols = ["latitude", "longitude", "bright_ti4", "scan", "track", "acq_date", "acq_time", "satellite",
             "instrument", "confidence", "version", "bright_ti5", "frp", "daynight", "type"]
    return {"modis": modis[mcols].sort_values("acq_date"), "viirs": viirs[vcols].sort_values("acq_date")}


def generate(out_dir: Path, end: dt.date | None = None, seed: int = 7,
             regions: list[Region] | None = None) -> list[Path]:
    end = end or dt.date.today()
    out_dir.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(seed)
    written = []
    for region in regions or REGIONS:
        det = add_noise(detect(spots(region, end, rng), end, rng), region, end, rng)
        for name, df in to_firms(det, rng).items():
            path = out_dir / f"{region.id}_{name}.csv.gz"
            with gzip.open(path, "wt") as f:
                df.to_csv(f, index=False)
            written.append(path)
            print(f"  {path.name}: {len(df):,} rows")
    (out_dir / "SYNTHETIC").write_text(f"Generated by pipeline.demo seed={seed} end={end}\n")
    return written


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else config.RAW / "demo"
    print(f"Writing synthetic FIRMS archives to {target}")
    generate(target)
