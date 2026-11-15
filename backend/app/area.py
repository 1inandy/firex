"""User-selected areas: a bbox, a polygon, or a circle (centre + radius).

SQL prefilters on the bounding box (fast, uses Parquet row-group stats); `Area.filter` then keeps only rows
whose point (grid-cell centre, hotspot, or event centroid) falls inside the actual shape.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

Bbox = tuple[float, float, float, float]
EARTH_KM = 6371.0088
MAX_VERTICES = 200


@dataclass(frozen=True)
class Area:
    bbox: Bbox
    polygon: tuple[tuple[float, float], ...] | None = None  # (lon, lat) vertices, not closed
    circle: tuple[float, float, float] | None = None  # (lon, lat, radius_km)

    @property
    def kind(self) -> str:
        return "polygon" if self.polygon else "circle" if self.circle else "bbox"

    def contains(self, lat, lon) -> np.ndarray:
        lat, lon = np.asarray(lat, float), np.asarray(lon, float)
        w, s, e, n = self.bbox
        inside = (lon >= w) & (lon <= e) & (lat >= s) & (lat <= n)
        if self.polygon:
            inside &= _in_polygon(lon, lat, np.asarray(self.polygon, float))
        elif self.circle:
            clon, clat, km = self.circle
            inside &= haversine_km(lat, lon, clat, clon) <= km
        return inside

    def filter(self, df: pd.DataFrame, lat: str = "lat", lon: str = "lon") -> pd.DataFrame:
        if self.kind == "bbox" or df.empty:
            return df
        return df[self.contains(df[lat], df[lon])]


def haversine_km(lat1, lon1, lat2, lon2) -> np.ndarray:
    p1, p2 = np.radians(lat1), np.radians(lat2)
    dphi, dl = p2 - p1, np.radians(np.asarray(lon2) - np.asarray(lon1))
    a = np.sin(dphi / 2) ** 2 + np.cos(p1) * np.cos(p2) * np.sin(dl / 2) ** 2
    return 2 * EARTH_KM * np.arcsin(np.sqrt(np.clip(a, 0, 1)))


def _in_polygon(x: np.ndarray, y: np.ndarray, poly: np.ndarray) -> np.ndarray:
    """Even-odd ray casting, vectorized over points."""
    inside = np.zeros(x.shape, bool)
    xj, yj = poly[-1]
    for xi, yi in poly:
        crosses = (yi > y) != (yj > y)
        with np.errstate(divide="ignore", invalid="ignore"):
            xint = (xj - xi) * (y - yi) / (yj - yi) + xi
        inside ^= crosses & (x < xint)
        xj, yj = xi, yi
    return inside


def from_bbox(b: Bbox) -> Area:
    return Area(b)


def from_polygon(pts: list[tuple[float, float]]) -> Area:
    if not 3 <= len(pts) <= MAX_VERTICES:
        raise ValueError(f"polygon needs 3–{MAX_VERTICES} vertices")
    for lon, lat in pts:
        if not (-180 <= lon <= 180 and -90 <= lat <= 90):
            raise ValueError("polygon vertices must be valid lon,lat")
    lons, lats = [p[0] for p in pts], [p[1] for p in pts]
    if max(lons) - min(lons) <= 0 or max(lats) - min(lats) <= 0:
        raise ValueError("polygon has no area")
    return Area((min(lons), min(lats), max(lons), max(lats)), polygon=tuple(pts))


def from_circle(lon: float, lat: float, km: float) -> Area:
    if not (-180 <= lon <= 180 and -90 <= lat <= 90):
        raise ValueError("circle centre must be valid lon,lat")
    if not 0 < km <= 2000:
        raise ValueError("circle radius must be between 0 and 2000 km")
    dlat = km / 110.574
    dlon = km / (111.32 * max(np.cos(np.radians(lat)), 0.01))
    b = (max(-180.0, lon - dlon), max(-90.0, lat - dlat), min(180.0, lon + dlon), min(90.0, lat + dlat))
    return Area(b, circle=(lon, lat, km))


def area_km2(a: Area) -> float:
    """Approximate area of the shape (for display and sanity checks)."""
    if a.circle:
        return float(np.pi * a.circle[2] ** 2)
    if a.polygon:
        pts = np.asarray(a.polygon, float)
        lat0 = np.radians(pts[:, 1].mean())
        x = np.radians(pts[:, 0]) * EARTH_KM * np.cos(lat0)
        y = np.radians(pts[:, 1]) * EARTH_KM
        return float(abs(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1))) / 2)
    w, s, e, n = a.bbox
    return float(EARTH_KM ** 2 * abs(np.radians(e - w)) * abs(np.sin(np.radians(n)) - np.sin(np.radians(s))))


def _floats(v: str, n: int | None, what: str) -> list[float]:
    try:
        out = [float(x) for x in v.split(",")]
    except ValueError:
        raise ValueError(f"{what} must be comma-separated numbers")
    if n is not None and len(out) != n:
        raise ValueError(f"{what} needs {n} numbers")
    if not all(np.isfinite(out)):
        raise ValueError(f"{what} must be finite numbers")
    return out


def parse(bbox: str | None = None, poly: str | None = None, circle: str | None = None) -> Area:
    """Area from query-string params. Precedence: poly, then circle, then bbox. Raises ValueError."""
    if poly:
        pts = [tuple(_floats(p, 2, "poly vertex")) for p in poly.strip().strip(";").split(";")]
        return from_polygon(pts)  # type: ignore[arg-type]
    if circle:
        lon, lat, km = _floats(circle, 3, "circle (lon,lat,radius_km)")
        return from_circle(lon, lat, km)
    if bbox:
        w, s, e, n = _floats(bbox, 4, "bbox (W,S,E,N)")
        if not (-180 <= w < e <= 180 and -90 <= s < n <= 90):
            raise ValueError("bbox must satisfy W<E, S<N within lon/lat bounds")
        return Area((w, s, e, n))
    raise ValueError("give an area: bbox=W,S,E,N, poly=lon,lat;lon,lat;… or circle=lon,lat,km")


def to_params(a: Area) -> dict[str, str]:
    """Inverse of `parse` (used to store alert areas and build links)."""
    r = lambda v: repr(round(float(v), 4))  # noqa: E731
    if a.polygon:
        return {"poly": ";".join(f"{r(x)},{r(y)}" for x, y in a.polygon)}
    if a.circle:
        return {"circle": ",".join(r(v) for v in a.circle)}
    return {"bbox": ",".join(r(v) for v in a.bbox)}
