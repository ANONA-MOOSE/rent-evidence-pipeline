#!/usr/bin/env python3
"""
Load published First-tier Tribunal rent decisions near the subject flat.

  python3 tribunal_decisions.py          # downloads (at most weekly) and loads decisions within TRIBUNAL_KM

Source: Rent Rise Check (https://rentrisecheck.uk), an independent project that extracts the
figures from decisions published on GOV.UK and rebuilds its CSV nightly. Figures are machine-
extracted, so always check a decision you rely on against the GOV.UK original (url column).
"""
import csv
import json
import os
import time
import urllib.request

import openrent_comps as oc

CSV_URL = "https://rentrisecheck.uk/data/decisions.csv"
TRIB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tribunal_data")
CSV_PATH = os.path.join(TRIB_DIR, "decisions.csv")
TRIBUNAL_KM = 3.0
MAX_AGE_DAYS = 7

SCHEMA = """
CREATE TABLE IF NOT EXISTS tribunal_decisions (
  case_ref TEXT PRIMARY KEY, url TEXT, title TEXT, postcode TEXT, lat REAL, lng REAL, distance_km REAL,
  decision_date TEXT, notice_date TEXT, effective_date TEXT, outcome TEXT, property_type TEXT, bedrooms INTEGER,
  current_rent REAL, proposed_rent REAL, determined_rent REAL, post_act INTEGER, social_landlord INTEGER,
  bills_included INTEGER, needs_review INTEGER, loaded TEXT
);
"""


def download(force=False):
    os.makedirs(TRIB_DIR, exist_ok=True)
    if not force and os.path.exists(CSV_PATH) and time.time() - os.path.getmtime(CSV_PATH) < MAX_AGE_DAYS * 86400:
        return False
    req = urllib.request.Request(CSV_URL, headers={"User-Agent": "rent-comparables/1.0"})
    with urllib.request.urlopen(req, timeout=60, context=oc.SSL_CTX) as r:
        data = r.read()
    if not data.startswith(b"url,case_ref"):
        raise ValueError("Unexpected file format from Rent Rise Check")
    with open(CSV_PATH + ".tmp", "wb") as f:
        f.write(data)
    os.replace(CSV_PATH + ".tmp", CSV_PATH)
    return True


def _num(v, cast=float):
    try:
        return cast(float(v)) if v not in ("", None) else None
    except ValueError:
        return None


def load():
    con = oc.db()
    con.executescript(SCHEMA)
    con.execute("DELETE FROM tribunal_decisions")
    n = 0
    with open(CSV_PATH, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if r.get("case_type") != "section_13" or r.get("superseded_by"):
                continue
            lat, lng = _num(r.get("lat")), _num(r.get("lon"))
            if lat is None or lng is None:
                continue
            d = oc.km(oc.HOME_LAT, oc.HOME_LNG, lat, lng)
            url = r.get("url") or ""
            if d > TRIBUNAL_KM or not url.startswith("https://www.gov.uk/"):
                continue
            con.execute("INSERT OR REPLACE INTO tribunal_decisions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                r["case_ref"], url, r.get("title"), r.get("postcode"), lat, lng, round(d, 2),
                r.get("decision_date"), r.get("notice_date"), r.get("effective_date"), r.get("outcome"),
                r.get("property_type"), _num(r.get("bedrooms"), int), _num(r.get("current_rent")),
                _num(r.get("proposed_rent")), _num(r.get("determined_rent")), _num(r.get("post_act"), int),
                _num(r.get("social_landlord"), int), _num(r.get("bills_included"), int),
                _num(r.get("needs_review"), int), oc.now()))
            n += 1
    con.commit()
    return n


def rows(con=None):
    """Decisions as dicts, nearest-relevant first: private 2-bed flats, then other private, then social."""
    con = con or oc.db()
    try:
        cur = con.execute("SELECT * FROM tribunal_decisions")
    except Exception:
        return []
    cols = [c[0] for c in cur.description]
    out = [dict(zip(cols, r)) for r in cur]
    checks = {}
    try:
        for c in con.execute("SELECT case_ref, matches_dataset, good_condition_rent, deduction_pct, ai_result FROM tribunal_check"):
            checks[c[0]] = c[1:]
    except Exception:
        pass
    for r in out:
        m, good, ded, ai = checks.get(r["case_ref"], (None, None, None, None))
        ai = json.loads(ai) if ai else {}
        r["verified"] = m                       # 1 = GOV.UK text agrees, 0 = differs, None = not checked/readable
        r["good_condition_rent"] = good or ai.get("good_condition_rent_pcm")
        r["deduction_pct"] = ded
        r["deductions"] = ai.get("deductions") or []
        r["lesson"] = ai.get("lesson_for_tenant")
        r["reasoning"] = ai.get("key_reasoning")
    # The same case is sometimes published twice (e.g. a revised decision): keep one per address + date.
    seen, uniq = set(), []
    for r in sorted(out, key=lambda r: r["case_ref"]):
        key = ((r["postcode"] or ""), r["decision_date"], r["determined_rent"])
        if key not in seen:
            seen.add(key); uniq.append(r)
    for r in uniq:
        r["private"] = not r["social_landlord"]
        r["two_bed_flat"] = r["bedrooms"] == 2 and (r["property_type"] or "") in ("flat", "maisonette")
        r["comparable"] = r["private"] and r["two_bed_flat"] and not r["bills_included"]
    uniq.sort(key=lambda r: r["decision_date"] or "", reverse=True)        # newest first...
    uniq.sort(key=lambda r: (not r["comparable"], not r["private"]))      # ...within each group (stable sort)
    return uniq


def main():
    got = download()
    n = load()
    print(f"{'Downloaded and loaded' if got else 'Loaded'} {n} tribunal rent decisions within {TRIBUNAL_KM:g} km.")
    for r in rows():
        if r["comparable"]:
            f = lambda v: f"£{v:,.0f}" if v else "not stated"
            print(f"  {r['decision_date']}  {r['distance_km']}km  {r['postcode']}: current {f(r['current_rent'])}, "
                  f"proposed {f(r['proposed_rent'])}, decided {f(r['determined_rent'])}")


if __name__ == "__main__":
    main()
