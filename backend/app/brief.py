"""Plain-language briefing written from the computed numbers only (a deterministic template).

Every number in the text is a field of `facts`, so nothing is estimated. Events are clusters of satellite
detections, so their size and length are detection footprint and detection span, not burned area.
"""
from __future__ import annotations

import calendar as cal

MONTHS = list(cal.month_name)


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
        "trend_significant": summ.get("trend_significant"),
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
        s = (f"Fires that started in {m} here were detected over a median of "
             f"{_fmt(f['precedent_median_duration_days'])} days (1 in 10 for "
             f"{_fmt(f['precedent_p90_duration_days'])} days or more)")
        if f.get("largest_event_year"):
            s += (f"; the most persistent (most fire cell-days) began in {f['largest_event_year']}, with detections "
                  f"across about {_fmt(f['largest_event_area_km2'])} km² of grid cells over "
                  f"{f['largest_event_duration_days']} days")
        parts.append(s + ".")
    if f.get("trend_significant") and abs(f["trend_per_decade_pct"]) >= 5:
        word = "rising" if f["trend_per_decade_pct"] > 0 else "falling"
        parts.append(f"The long-term record is {word} by about {_fmt(abs(f['trend_per_decade_pct']))}% per decade.")
    if f["synthetic_demo_data"]:
        parts.append("Note: these figures come from synthetic demo data, not real satellite observations.")
    return " ".join(parts)


def brief(f: dict) -> dict:
    return {"text": template_text(f), "facts": f}
