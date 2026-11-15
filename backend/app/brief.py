"""Grounded briefing: plain-language summary that may only use numbers the app computed.

Facts are computed first. The text is written either by a deterministic template or, when
ANTHROPIC_API_KEY is set, by Claude with the facts as its only source. Either way every number in the
text is checked against the facts; anything that doesn't match is marked "[unverified]".
"""
from __future__ import annotations

import calendar as cal
import json
import os
import re

MONTHS = list(cal.month_name)
MODEL = "claude-opus-5-5"
NUM = re.compile(r"(?<![\w.])-?\d[\d,]*(?:\.\d+)?")

SYSTEM = (
    "You write short situation briefings for wildfire managers and emergency responders. "
    "Use ONLY the numbers in the provided JSON facts; never estimate, extrapolate or add numbers that are not "
    "there (no percentages you computed yourself, no counts of years unless given). You may round a number to "
    "fewer decimals. If a fact is null, say it is unknown or leave it out. Units: 'fire cell-days' = grid cells "
    "(~5 km) with at least one satellite fire detection on a day, in VIIRS-equivalent units. "
    "Write 4-6 plain sentences, no headings, no bullet points, no markdown."
)


def build_facts(cur: dict, summ: dict, prec: dict, val: dict | None, synthetic: bool) -> dict:
    f = {
        "month_name": MONTHS[cur["month"]], "year": cur["year"], "current_value": cur["value"],
        "historical_median": cur["median"], "ratio_to_median": cur["ratio"], "highest_since_year": cur["highest_since"],
        "rank_among_years": cur["rank"], "n_years": cur["n_years"], "month_is_partial": cur["partial"],
        "days_covered": cur["days_covered"],
        "peak_year": (summ.get("peak_year") or {}).get("year"),
        "peak_year_value": (summ.get("peak_year") or {}).get("value"),
        "typical_peak_month": MONTHS[summ["typical_peak_month"]] if summ.get("typical_peak_month") else None,
        "trend_per_decade_pct": summ.get("trend_per_decade_pct"),
        "precedent_n_events": prec.get("n_events"),
        "precedent_median_duration_days": prec.get("median_duration_days"),
        "precedent_p90_duration_days": prec.get("p90_duration_days"),
        "precedent_median_area_km2": prec.get("median_area_km2"),
        "largest_event_start": (prec.get("largest") or {}).get("start"),
        "largest_event_area_km2": (prec.get("largest") or {}).get("area_km2"),
        "largest_event_duration_days": (prec.get("largest") or {}).get("duration_days"),
        "harmonization_annual_error_pct": (val or {}).get("annual_abs_error_pct"),
        "synthetic_demo_data": synthetic,
    }
    if f["largest_event_start"]:
        f["largest_event_year"] = int(f["largest_event_start"][:4])
    return f


def _fmt(v: float) -> str:
    return f"{v:,.0f}" if abs(v) >= 10 else f"{v:.1f}".rstrip("0").rstrip(".")


def template_text(f: dict) -> str:
    m, y = f["month_name"], f["year"]
    so_far = f" (first {f['days_covered']} days)" if f["month_is_partial"] else ""
    parts = [f"{m} {y}{so_far}: {_fmt(f['current_value'])} fire cell-days, against a median of "
             f"{_fmt(f['historical_median'])} for the same days of {m} in past years."]
    r = f["ratio_to_median"]
    if r is None:
        parts.append(f"{m} usually sees no fire activity here, so any detection is unusual.")
    elif r >= 1.5:
        since = (f"the highest since {f['highest_since_year']}" if f["highest_since_year"]
                 else f"the highest {m} on record")
        parts.append(f"That is {r:g}× normal, {since}.")
    elif r <= 0.67:
        parts.append(f"That is {r:g}× normal: quieter than usual.")
    else:
        parts.append(f"That is close to normal ({r:g}× the median).")
    if f["typical_peak_month"]:
        parts.append(f"Fire activity in this area typically peaks in {f['typical_peak_month']}"
                     + (f"; the most active full year was {f['peak_year']} with {_fmt(f['peak_year_value'])} "
                        "fire cell-days." if f["peak_year"] else "."))
    if f["precedent_n_events"]:
        s = (f"Fires that started in {m} here burned a median of {_fmt(f['precedent_median_duration_days'])} days "
             f"(1 in 10 lasted {_fmt(f['precedent_p90_duration_days'])} days or more)")
        if f.get("largest_event_year"):
            s += (f"; the largest began in {f['largest_event_year']} and covered about "
                  f"{_fmt(f['largest_event_area_km2'])} km² over {f['largest_event_duration_days']} days")
        parts.append(s + ".")
    if f["trend_per_decade_pct"] is not None and abs(f["trend_per_decade_pct"]) >= 5:
        word = "rising" if f["trend_per_decade_pct"] > 0 else "falling"
        parts.append(f"The long-term record is {word} by about {_fmt(abs(f['trend_per_decade_pct']))}% per decade.")
    if f["synthetic_demo_data"]:
        parts.append("Note: these figures come from synthetic demo data, not real satellite observations.")
    return " ".join(parts)


def _allowed(f: dict) -> set[float]:
    vals: set[float] = {1.0}  # "1 in 10" phrasing in the template
    vals.add(10.0)
    for k, v in f.items():
        if isinstance(v, bool) or v is None:
            continue
        if isinstance(v, (int, float)):
            x = abs(float(v))
            vals.update({x, round(x), round(x, 1), round(x, 2)})
            if x >= 1000 and not (k == "year" or k.endswith("_year")):  # "about 15,100" / "15,000"
                vals.update({round(x, -2), round(x, -3)})
        elif isinstance(v, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
            y, mo, d = v.split("-")
            vals.update({float(y), float(int(mo)), float(int(d))})
    return vals


def verify(text: str, f: dict) -> tuple[str, list[str]]:
    """Mark numbers in `text` not traceable to facts. Returns (marked_text, unverified_numbers)."""
    allowed = _allowed(f)
    bad: list[str] = []

    def check(m: re.Match) -> str:
        raw = m.group(0)
        try:
            v = float(raw.replace(",", ""))
        except ValueError:
            return raw
        ok = any(abs(abs(v) - a) <= 0.051 for a in allowed)
        if ok:
            return raw
        bad.append(raw)
        return f"{raw} [unverified]"

    return NUM.sub(check, text), bad


def llm_text(f: dict) -> str | None:
    """Ask Claude for the briefing. Returns None if unavailable so the caller can fall back."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return None
    try:
        import anthropic
    except ImportError:
        return None
    try:
        client = anthropic.Anthropic(timeout=30.0)
        resp = client.beta.messages.create(
            model=MODEL, max_tokens=2000, system=SYSTEM,
            output_config={"effort": "low"},
            betas=["server-side-fallback-2026-07-01"], fallbacks="default",
            messages=[{"role": "user", "content": "Facts (JSON):\n" + json.dumps(f) + "\n\nWrite the briefing."}],
        )
    except anthropic.APIError:
        return None
    if resp.stop_reason == "refusal":
        return None
    text = "".join(b.text for b in resp.content if b.type == "text").strip()
    return text or None


def brief(f: dict) -> dict:
    text = llm_text(f)
    model = MODEL if text else "template"
    text = text or template_text(f)
    marked, bad = verify(text, f)
    return {"text": marked, "facts": f, "verified": not bad, "unverified_numbers": bad, "model": model}
