"""Pitch figures as standalone SVG (no plotting dependency):  python -m pipeline.figures [region]

Writes data/figures/<region>_before_after.svg and <region>_validation.svg.
Colours: Okabe-Ito (colourblind-safe).
"""
from __future__ import annotations

import json
import sys
from html import escape

import pandas as pd

from . import config
from .harmonize import harmonize, ratios_for_region

C = {"modis": "#E69F00", "viirs": "#0072B2", "harm": "#009E73", "grid": "#d9d9d9", "text": "#222"}
W, H, PAD = 760, 360, dict(l=64, r=20, t=56, b=44)


def _scale(v0, v1, p0, p1):
    return lambda v: p0 + (v - v0) / (v1 - v0 or 1) * (p1 - p0)


def _nice(v: float) -> float:
    import math
    e = 10 ** math.floor(math.log10(v / 4))
    return next(e * k for k in (1, 1.25, 1.5, 2, 2.5, 3, 4, 5, 7.5, 10) if e * k * 4 >= v) * 4


def _frame(title: str, subtitle: str, x0: int, x1: int, ymax: float, ylabel: str, every: int = 5):
    ymax = _nice(ymax)
    sx = _scale(x0 - 0.5, x1 + 0.5, PAD["l"], W - PAD["r"])
    sy = _scale(0, ymax, H - PAD["b"], PAD["t"])
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" font-family="system-ui,sans-serif" '
           f'font-size="12" fill="{C["text"]}"><rect width="{W}" height="{H}" fill="#fff"/>',
           f'<text x="{PAD["l"]}" y="22" font-size="16" font-weight="600">{escape(title)}</text>',
           f'<text x="{PAD["l"]}" y="40" fill="#555">{escape(subtitle)}</text>']
    for k in range(5):
        v = ymax * k / 4
        out.append(f'<line x1="{PAD["l"]}" x2="{W - PAD["r"]}" y1="{sy(v):.1f}" y2="{sy(v):.1f}" stroke="{C["grid"]}"/>'
                   f'<text x="{PAD["l"] - 6}" y="{sy(v) + 4:.1f}" text-anchor="end">{v:,.0f}</text>')
    for yr in range(x0, x1 + 1):
        if yr % every == 0:
            out.append(f'<text x="{sx(yr):.1f}" y="{H - PAD["b"] + 16}" text-anchor="middle">{yr}</text>')
    out.append(f'<text transform="translate(16 {(H + PAD["t"] - PAD["b"]) / 2}) rotate(-90)" text-anchor="middle" '
               f'fill="#555">{escape(ylabel)}</text>')
    return out, sx, sy


def _line(points, sx, sy, color, width=2.5, dash=""):
    d = " ".join(f"{'M' if i == 0 else 'L'}{sx(x):.1f},{sy(y):.1f}" for i, (x, y) in enumerate(points))
    da = f' stroke-dasharray="{dash}"' if dash else ""
    return f'<path d="{d}" fill="none" stroke="{color}" stroke-width="{width}"{da}/>'


def _legend(items, x, y):
    out = []
    for i, (label, color, dash) in enumerate(items):
        yy = y + i * 18
        da = f' stroke-dasharray="{dash}"' if dash else ""
        out.append(f'<line x1="{x}" x2="{x + 22}" y1="{yy}" y2="{yy}" stroke="{color}" stroke-width="3"{da}/>'
                   f'<text x="{x + 28}" y="{yy + 4}">{escape(label)}</text>')
    return out


def before_after(region: str) -> str:
    w, s, e, n = config.REGIONS[region]["bbox"]
    monthly = pd.read_parquet(config.GRID / "monthly.parquet")
    monthly = monthly[monthly.lon.between(w, e) & monthly.lat.between(s, n)]
    meta = json.loads(config.META.read_text())
    last_full_year = int(meta["last_full_month"][:4]) - (meta["last_full_month"][5:] != "12")
    monthly = monthly[monthly.year <= last_full_year]
    raw = monthly.groupby(["year", "family"])["fire_cell_days"].sum().unstack(fill_value=0)
    ratios = ratios_for_region(pd.read_parquet(config.HARMONIZED / "ratios.parquet"), region)
    harm = harmonize(monthly, ratios).groupby("year")["fire_cell_days"].sum()
    x0, x1 = int(raw.index.min()), int(raw.index.max())
    modis = list(raw["MODIS"].items())
    viirs = [(y, v) for y, v in raw["VIIRS"].items() if y >= config.OVERLAP_START_YEAR]
    ymax = max(raw.max().max(), harm.max()) * 1.05
    out, sx, sy = _frame(f"{config.REGIONS[region]['name']}: one fire record instead of two",
                         "Annual fire cell-days. VIIRS sees more fires than MODIS; splicing the two fakes a jump in 2012.", x0, x1, ymax, "fire cell-days per year")
    out.append(f'<line x1="{sx(config.OVERLAP_START_YEAR - 0.5):.1f}" x2="{sx(config.OVERLAP_START_YEAR - 0.5):.1f}" '
               f'y1="{PAD["t"]}" y2="{H - PAD["b"]}" stroke="#999" stroke-dasharray="3 3"/>'
               f'<text x="{sx(config.OVERLAP_START_YEAR - 0.5) + 4:.1f}" y="{PAD["t"] + 12}" fill="#777">VIIRS starts</text>')
    out.append(_line(list(harm.items()), sx, sy, C["harm"], 5))
    out.append(_line(modis, sx, sy, C["modis"], 2, "6 4"))
    out.append(_line(viirs, sx, sy, C["viirs"], 2))
    out += _legend([("raw MODIS (Terra + Aqua)", C["modis"], "6 4"), ("raw VIIRS (Suomi NPP)", C["viirs"], ""),
                    ("harmonized record (VIIRS-equivalent)", C["harm"], "")], PAD["l"] + 12, PAD["t"] + 16)
    if meta.get("synthetic"):
        out.append(f'<text x="{W - PAD["r"]}" y="{H - 8}" text-anchor="end" fill="#a00">synthetic demo data</text>')
    return "\n".join(out + ["</svg>"])


def validation(region: str) -> str:
    v = json.loads((config.HARMONIZED / "validation.json").read_text())[region]
    m = pd.DataFrame(v["monthly"])
    m["t"] = m.year + (m.month - 0.5) / 12
    ymax = max(m.predicted.max(), m.observed.max()) * 1.1
    x0, x1 = int(m.year.min()), int(m.year.max())
    out, _, sy = _frame(
        f"Harmonized MODIS predicts real VIIRS within {v['annual_abs_error_pct']}% a year",
        f"Fit on {v['train_years'][0]}–{v['train_years'][1]}, tested on {v['test_years'][0]}–{v['test_years'][1]} "
        f"(never seen). Simple ratio baseline: {v['baseline']['annual_abs_error_pct']}%.",
        x0, x1, ymax, "fire cell-days per month", every=1)
    sx2 = _scale(x0, x1 + 1, PAD["l"], W - PAD["r"])
    out.append(_line(list(zip(m.t, m.observed)), sx2, sy, C["viirs"], 2.5))
    out.append(_line(list(zip(m.t, m.predicted)), sx2, sy, C["harm"], 2, "5 3"))
    out += _legend([("observed VIIRS", C["viirs"], ""), ("predicted from MODIS", C["harm"], "5 3")],
                   W - PAD["r"] - 200, PAD["t"] + 16)
    return "\n".join(out + ["</svg>"])


def main(regions: list[str]) -> None:
    out = config.DATA / "figures"
    out.mkdir(exist_ok=True)
    for r in regions:
        (out / f"{r}_before_after.svg").write_text(before_after(r))
        (out / f"{r}_validation.svg").write_text(validation(r))
        print(f"wrote {out}/{r}_*.svg")


if __name__ == "__main__":
    main(sys.argv[1:] or list(config.REGIONS))
