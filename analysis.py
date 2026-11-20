#!/usr/bin/env python3
"""
Valuation-style analysis on top of the comparables database.

  python3 analysis.py         # prints the adjustment grid and the sensitivity table

grid(rows)         The best let-agreed comparables, each adjusted (time, layout, features) to be
                   like the subject flat, then combined into one indicated market rent.
sensitivity(rows)  The median agreed rent under every reasonable choice of radius and period,
                   with a bootstrap 90% interval, to show the conclusion doesn't hinge on the filters.

Both are used by the Excel export and the dashboard.
"""
import os
import random
import re
import statistics
from datetime import date

import openrent_comps as oc


def _months_ago(d):
    try:
        return (date.today() - date.fromisoformat(d[:10])).days / 30.44
    except (TypeError, ValueError):
        return None


def _age(d):
    """Months since d; unknown dates count as very old (0 is a valid age, so no `or`)."""
    m = _months_ago(d)
    return 999 if m is None else m


def _is_flat(r):
    return not re.search(r"house|bungalow|cottage", (r["property_type"] or "").lower())


def adjustments_for(r):
    """List of (label, percent) that turn this comparable into a like-for-like for the subject.
    Uses, in order of preference: your manual review, the AI photo review, then the listing text."""
    A, out = oc.ADJUSTMENTS, []
    f = oc.desc_flags(r["description"])
    ai = r.get("ai") or {}
    layout = (r.get("layout") or "").lower()
    cond = (r.get("condition") or "").lower()
    if "single" in layout:
        f["single_bedroom"], f["two_doubles"] = True, False
    elif "two double" in layout:
        f["two_doubles"], f["single_bedroom"] = True, False
    if f.get("two_doubles") or (not f.get("single_bedroom") and (r.get("max_tenants") or 0) >= 4):
        out.append(("Two double bedrooms (yours: double + single)", A["two_doubles"]))
    if cond == "newly refurbished" or (not cond and f.get("refurbished")):
        out.append(("Newly refurbished", A["refurbished"]))
    elif cond == "dated":
        out.append(("Dated condition", A["dated"]))
    elif cond == "poor":
        out.append(("Poor condition", A["poor"]))
    if r.get("building_type") == "modern_block" and oc.SUBJECT.get("building_type") != "modern_block":
        out.append((f"Modern/new-build block ({r.get('building_basis') or 'listing'}; yours: Victorian conversion)", A["modern_block"]))
    subj_st = (oc.LOCATION_INFO or {}).get("subject_station") or {}
    if r.get("station_km") is not None and subj_st.get("km") is not None:
        pct = max(-oc.STATION_CAP_PCT, min(oc.STATION_CAP_PCT, (r["station_km"] - subj_st["km"]) * oc.STATION_PCT_PER_KM))
        if abs(pct) >= 0.5:
            nearer = "nearer" if pct < 0 else "further from"
            out.append((f"{r['station_km']:.1f} km to {r['station']} ({nearer} a station than yours: "
                        f"{subj_st['km']:.2f} km to {subj_st['name']})", round(pct, 1)))
    if max(r["baths"] or 1, ai.get("bathrooms") or 1) >= 2:
        out.append(("Second bathroom", A["extra_bathroom"]))
    garden = ai.get("garden") if ai.get("garden") not in (None, "unclear") else (None if r["garden"] is None else ("private" if r["garden"] else "none"))
    if garden == "none" and oc.SUBJECT["garden"]:
        out.append(("No garden", A["no_garden"]))
    epc = (r.get("epc") or "").strip().upper()[:1]
    order = "ABCDEFG"
    if epc in order and oc.SUBJECT_EPC in order and order.index(epc) < order.index(oc.SUBJECT_EPC):
        gap = order.index(oc.SUBJECT_EPC) - order.index(epc)
        key = "epc_much_better" if gap >= 3 else "epc_better" if gap == 2 else "epc_bit_better"
        out.append((f"Energy rating {epc} (yours: {oc.SUBJECT_EPC}; single glazing, some secondary)", A[key]))
    if r.get("area_m2") and oc.SUBJECT_FLOOR_M2:
        pct = ((oc.SUBJECT_FLOOR_M2 / r["area_m2"]) ** oc.SIZE_ELASTICITY - 1) * 100
        pct = max(-oc.SIZE_CAP_PCT, min(oc.SIZE_CAP_PCT, pct))
        if abs(pct) >= 0.5:
            out.append((f"Size {r['area_m2']:.0f} m² (yours: {oc.SUBJECT_FLOOR_M2:.0f} m²)", round(pct, 1)))
    if r.get("loc_factor"):
        pct = (1 / r["loc_factor"] - 1) * 100
        if abs(pct) >= 0.5:
            out.append((f"Location: {r['area_name']} (flat prices ×{r['area_ratio']:.2f} yours)", round(pct, 1)))
    parking = ai.get("parking") if ai.get("parking") not in (None, "unclear") else (None if r["parking"] is None else ("shared_or_communal" if r["parking"] else "none"))
    if parking in ("none", "permit_or_street") and oc.SUBJECT["parking"]:
        out.append(("No parking", A["no_parking"]))
    elif not oc.SUBJECT["parking"] and (parking == "private_or_allocated" or (not ai.get("parking") and r["parking"])):
        out.append(("Own parking (yours: shared forecourt others use)", A["own_parking"]))
    return out


def open_defects():
    """Deductions for the subject flat's defects that the defects log shows as still unresolved."""
    try:
        import defects_log
        summ = defects_log.summary()[0]
    except Exception:
        return []
    return [{"issue": s["issue"], "pct": oc.DEFECT_ADJUSTMENTS[s["issue"]], "days": s["days"], "since": s["first_report"]}
            for s in summ if s["status"] == "unresolved" and s["issue"] in oc.DEFECT_ADJUSTMENTS]


def grid(rows):
    pool = [r for r in rows
            if not r["exclude_reason"] and r["status"] == "let_agreed" and r["price_pcm"]
            and (r["distance_km"] or 99) <= oc.GRID_MAX_KM and _is_flat(r)
            and _age(r["let_agreed_date"]) <= oc.GRID_MAX_MONTHS]
    pool.sort(key=lambda r: (-r["score"], r["distance_km"] or 99))
    picked = pool[:oc.GRID_SIZE]
    lines = []
    for r in picked:
        adj = adjustments_for(r)
        f = oc.desc_flags(r["description"])
        ai = r.get("ai") or {}
        layout_known = bool(f.get("two_doubles") or f.get("single_bedroom") or r.get("layout"))
        time_pct = (r["time_factor"] - 1) * 100
        total_pct = time_pct + sum(p for _, p in adj)
        lines.append({
            "id": r["id"], "title": r["title"], "url": r["url"], "km": r["distance_km"],
            "let": r["let_agreed_date"], "score": r["score"], "rent": r["price_pcm"],
            "time_pct": round(time_pct, 1), "adjustments": [{"label": l, "pct": p} for l, p in adj],
            "total_pct": round(total_pct, 1), "adjusted": round(r["price_pcm"] * (1 + total_pct / 100)),
            "check": "" if layout_known else ("Bedroom sizes unclear even from the photos" if ai else
                                              "Bedroom sizes inferred from max tenants; AI photo review pending"),
            "ai": {k: ai.get(k) for k in ("summary", "bedroom_evidence", "condition_evidence", "confidence")} if ai else None,
        })
    if not lines:
        return {"lines": [], "conclusion": None}
    adj = [l["adjusted"] for l in lines]
    w = [l["score"] or 1 for l in lines]
    weighted = sum(a * x for a, x in zip(adj, w)) / sum(w)
    defects = open_defects()
    cond = sum(d["pct"] for d in defects)
    # Grid lines whose floor area is unknown got no size adjustment. Show what the answer would be if they
    # are the typical size of 2-bed comparables whose area we do know, so the gap is visible, not hidden.
    known = sorted(r["area_m2"] for r in rows if r.get("area_m2") and not r["exclude_reason"])
    typical = statistics.median(known) if known else None
    no_area = [l for l, r in zip(lines, picked) if not r.get("area_m2")]
    size_note = None
    if typical and oc.SUBJECT_FLOOR_M2 and no_area:
        pct = max(-oc.SIZE_CAP_PCT, min(oc.SIZE_CAP_PCT, ((oc.SUBJECT_FLOOR_M2 / typical) ** oc.SIZE_ELASTICITY - 1) * 100))
        adj2 = [l["adjusted"] * (1 + pct / 100) if l in no_area else l["adjusted"] for l in lines]
        w2 = sum(a * x for a, x in zip(adj2, w)) / sum(w)
        size_note = {"typical_m2": round(typical), "pct": round(pct, 1), "n_without_area": len(no_area),
                     "weighted": round(w2), "indicated": round(w2 * (1 + cond / 100))}
    # Location reference check: instead of the subject's own neighbourhood price, use the typical
    # neighbourhood price of lets within 1 km (answers "the subject street is more like the next, dearer neighbourhood").
    loc_alt = None
    info = oc.LOCATION_INFO or {}
    if info.get("ref_price") and (info.get("subject") or {}).get("price"):
        # The other reference: the subject's own neighbourhood price instead of the 1 km level (or vice versa).
        ref = info["subject"]["price"] if info.get("reference") == "local_1km" else info["ref_price"]
        f = (ref / info["ref_price"]) ** info["beta"] if info.get("reference") == "local_1km" else (ref / info["subject"]["price"]) ** info["beta"]
        base = size_note["indicated"] if size_note else round(weighted * (1 + cond / 100))
        loc_alt = {"alt_label": "your own neighbourhood" if info.get("reference") == "local_1km" else "the 1 km level",
                   "ref_price": round(ref), "subject_price": round(info["subject"]["price"]), "main_ref_price": round(info["ref_price"]),
                   "factor_pct": round((f - 1) * 100, 1), "indicated": round(base * f)}
    return {
        "lines": lines,
        "conclusion": {
            "n": len(lines), "weighted_mean": round(weighted), "median": round(statistics.median(adj)),
            "low": min(adj), "high": max(adj), "condition_pct": round(cond, 1), "defects": defects, "size_note": size_note, "loc_alt": loc_alt,
            "location": {k: info.get(k) for k in ("beta", "basis")} if info else None,
            "indicated": round(weighted * (1 + cond / 100)),
            "indicated_low": round(min(adj) * (1 + cond / 100)), "indicated_high": round(max(adj) * (1 + cond / 100)),
        },
        "settings": {"max_km": oc.GRID_MAX_KM, "max_months": oc.GRID_MAX_MONTHS, "size": oc.GRID_SIZE},
    }


def _boot_ci(vals, n=2000, seed=7):
    if len(vals) < 5:
        return None, None
    rnd = random.Random(seed)
    meds = sorted(statistics.median(rnd.choices(vals, k=len(vals))) for _ in range(n))
    return round(meds[int(0.05 * n)]), round(meds[int(0.95 * n) - 1])


def lets_in_window(lets, km, months):
    return [r for r in lets if r["distance_km"] is not None and r["distance_km"] <= km
            and _age(r["let_agreed_date"]) <= months]


def sensitivity(rows, rent=None, field="price_loc", _inner=False):
    rent = rent or oc.RENT_HISTORY[-1][1]
    lets = [r for r in rows if not r["exclude_reason"] and r["status"] == "let_agreed" and r.get(field)]
    cells = []
    for km in (1.0, 2.0, 3.0, 4.0):
        for months in (12, 24, 36):
            vals = [r[field] for r in lets_in_window(lets, km, months)]
            if not vals:
                cells.append({"km": km, "months": months, "n": 0})
                continue
            lo, hi = _boot_ci(vals)
            med = round(statistics.median(vals))
            cells.append({"km": km, "months": months, "n": len(vals), "median": med, "ci_low": lo, "ci_high": hi,
                          "gap": round(rent - med), "share_cheaper": round(100 * sum(v < rent for v in vals) / len(vals))})
    filled = [c for c in cells if c["n"]]
    out = {"rent": rent, "cells": cells, "field": field,
           "above_in": sum(c["gap"] > 0 for c in filled), "of": len(filled),
           "gap_min": min((c["gap"] for c in filled), default=None),
           "gap_max": max((c["gap"] for c in filled), default=None)}
    if not _inner:
        u = sensitivity(rows, rent, "price_today", _inner=True)   # same views without the location adjustment
        out["unadjusted"] = {k: u[k] for k in ("above_in", "of", "gap_min", "gap_max")}
        out["location"] = oc.LOCATION_INFO
    return out


def time_on_market(rows, threshold=None):
    """Do dearer listings take longer to let? Compares days advertised either side of your rent."""
    threshold = threshold or oc.RENT_HISTORY[-1][1]
    out = {}
    for label, status in (("advertised", "available"), ("let", "let_agreed")):
        live = [r for r in rows if not r["exclude_reason"] and r["status"] == status and r["days_advertised"] is not None
                and (r["distance_km"] or 99) <= oc.RADIUS_KM and (status == "available" or _age(r["let_agreed_date"]) <= 24)]
        hi = [r["days_advertised"] for r in live if r["price_pcm"] >= threshold]
        lo = [r["days_advertised"] for r in live if r["price_pcm"] < threshold]
        out[label] = {"n_high": len(hi), "n_low": len(lo),
                      "median_high": statistics.median(hi) if hi else None,
                      "median_low": statistics.median(lo) if lo else None}
    out["cuts"] = [{"title": r["title"], "url": r["url"], "from": r["first_price"], "to": r["price_pcm"]}
                   for r in rows if not r["exclude_reason"] and r["price_cut"] > 0]
    out["threshold"] = threshold
    return out


def matrix(rows):
    """Comparables table for MR1 section 12 / MR3: the grid comparables plus, by a fixed rule,
    every let-agreed 2-bed flat within 1 km let in the last 12 months. Nothing chosen by price."""
    g = grid(rows)
    ids = {l["id"] for l in g["lines"]}
    rule = [r for r in rows if not r["exclude_reason"] and r["status"] == "let_agreed" and _is_flat(r)
            and (r["distance_km"] or 99) <= 1.0 and _age(r["let_agreed_date"]) <= 12]
    ids |= {r["id"] for r in rule}
    adj = {l["id"]: l for l in g["lines"]}
    out = []
    for r in sorted((r for r in rows if r["id"] in ids), key=lambda r: r["distance_km"] or 99):
        parts = [p.strip() for p in (r["title"] or "").split(",")]
        ai = r.get("ai") or {}
        out.append({
            "id": r["id"], "street": parts[1] if len(parts) >= 3 else "", "postcode": r["postcode_district"],
            "distance_km": r["distance_km"], "type": r["property_type"], "layout": r.get("layout") or "",
            "floor": r.get("floor") or "", "area_m2": r.get("area_m2"), "per_m2": r.get("per_m2"),
            "furnishing": r["furnishing"], "garden": ai.get("garden") or ("yes" if r["garden"] else "no" if r["garden"] is not None else ""),
            "parking": ai.get("parking") or ("yes" if r["parking"] else "no" if r["parking"] is not None else ""),
            "condition": r.get("condition") or "", "first_price": r["first_price"], "final_price": r["price_pcm"],
            "price_today": r["price_today"], "let_agreed": r["let_agreed_date"], "first_listed": r["first_listed"],
            "days_advertised": r["days_advertised"], "adjusted": adj.get(r["id"], {}).get("adjusted"),
            "in_grid": r["id"] in adj, "summary": ai.get("summary") or "", "url": r["url"],
            "photos": os.path.join("openrent_data", "photos", str(r["id"])) if os.path.isdir(os.path.join(oc.DATA_DIR, "photos", str(r["id"]))) else "",
        })
    return out


def main():
    rows = oc.load_rows(oc.db())
    g = grid(rows)
    print("Adjustment grid")
    for l in g["lines"]:
        adj = "; ".join(f"{a['label']} {a['pct']:+.0f}%" for a in l["adjustments"]) or "none"
        print(f"  £{l['rent']:>5,.0f}  time {l['time_pct']:+.1f}%  {adj:<60} -> £{l['adjusted']:,}  {l['title'][:40]}")
    c = g["conclusion"]
    if c:
        if c.get("loc_alt"):
            la = c["loc_alt"]
            print(f"  Location check: using {la['alt_label']} (£{la['ref_price']:,} instead of £{la['main_ref_price']:,}) "
                  f"moves the result {la['factor_pct']:+.1f}% to £{la['indicated']:,}")
        if c["size_note"]:
            sn = c["size_note"]
            print(f"  Size caveat: {sn['n_without_area']} comparables have no floor area; if they are the typical "
                  f"{sn['typical_m2']} m², each rises {sn['pct']:+.1f}% and the indicated rent becomes £{sn['indicated']:,}")
        print(f"  Weighted £{c['weighted_mean']:,} (median £{c['median']:,}, range £{c['low']:,}–£{c['high']:,}); "
              f"after condition {c['condition_pct']:+.0f}%: £{c['indicated']:,}")
    s = sensitivity(rows)
    print(f"\nSensitivity (time- and location-adjusted let-agreed medians; your rent £{s['rent']:,.2f})")
    for x in s["cells"]:
        if x["n"]:
            print(f"  ≤{x['km']:.0f} km, last {x['months']:>2} mo: n={x['n']:<3} median £{x['median']:,} "
                  f"(90% CI £{x['ci_low'] or 0:,}–£{x['ci_high'] or 0:,})  you pay {x['gap']:+,}")
    print(f"  Your rent is above the median in {s['above_in']} of {s['of']} views.")


if __name__ == "__main__":
    main()
