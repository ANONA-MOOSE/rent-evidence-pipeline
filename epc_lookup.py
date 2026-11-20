#!/usr/bin/env python3
"""
Estimate floor area (m²) for each comparable from the GOV.UK EPC register.

  python3 epc_lookup.py            # fills the floor_area table, then re-run: openrent_comps.py export

Needs a free API token from https://get-energy-performance-data.communities.gov.uk
(sign in -> "My account" -> bearer token). Put it in an env var EPC_API_TOKEN or in a
file called epc_token.txt next to this script.

How a comparable is matched (OpenRent only shows street + district, not house number):
  1. Its map coordinates -> nearby full postcodes (postcodes.io, free, no key).
  2. EPC certificates registered at those postcodes whose address is on the same street.
  3. Keep the latest certificate per address, of the same kind (flat/maisonette vs house),
     preferring ones with 3 habitable rooms (= 2 bedrooms + living room).
  4. Estimate = median floor area of those; we also record how many matched and the range.
If the listing description states the size (sq ft / m²), that figure is used instead.
"""

import json
import os
import re
import sqlite3
import statistics
import sys
import time
import urllib.parse
import urllib.request

import openrent_comps as oc

API = "https://api.get-energy-performance-data.communities.gov.uk"
HERE = os.path.dirname(os.path.abspath(__file__))
PROPERTY_TYPES = {0: "House", 1: "Bungalow", 2: "Flat", 3: "Maisonette", 4: "Park home"}
SQFT_TO_M2 = 0.092903

SCHEMA = """
CREATE TABLE IF NOT EXISTS epc_postcode_cache (postcode TEXT PRIMARY KEY, json TEXT, fetched TEXT);
CREATE TABLE IF NOT EXISTS epc_cert_cache (cert TEXT PRIMARY KEY, json TEXT, fetched TEXT);
CREATE TABLE IF NOT EXISTS geo_cache (latlng TEXT PRIMARY KEY, postcodes TEXT);
CREATE TABLE IF NOT EXISTS floor_area (
  property_id TEXT PRIMARY KEY, area_m2 REAL, basis TEXT, n_matches INTEGER,
  range_m2 TEXT, matched_addresses TEXT, updated TEXT
);
"""

def token():
    t = os.environ.get("EPC_API_TOKEN")
    p = os.path.join(HERE, "epc_token.txt")
    if not t and os.path.exists(p):
        t = open(p).read().strip()
    if not t:
        print("No EPC token yet, so only sizes stated in listings will be used.\n"
              "Get one free at https://get-energy-performance-data.communities.gov.uk "
              "and save it in epc_token.txt next to this script.\n")
    return t

def get_json(url, headers=None, pause=0.25):
    time.sleep(pause)
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "rent-comparables/1.0", **(headers or {})})
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=30, context=oc.SSL_CTX) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            if e.code == 401:
                sys.exit("EPC API rejected the token (401). Check epc_token.txt.")
            if e.code == 429:
                print("  rate limited, waiting 60s"); time.sleep(60); continue
            print(f"  HTTP {e.code} for {url}"); time.sleep(5)
        except Exception as e:
            print(f"  {e}; retrying"); time.sleep(5)
    return None

# ---------------------------------------------------------------------------
def nearby_postcodes(con, lat, lng):
    key = f"{lat:.5f},{lng:.5f}"
    row = con.execute("SELECT postcodes FROM geo_cache WHERE latlng=?", (key,)).fetchone()
    if row:
        return json.loads(row[0])
    d = get_json(f"https://api.postcodes.io/postcodes?lon={lng}&lat={lat}&radius=150&limit=6") or {}
    pcs = [r["postcode"] for r in (d.get("result") or [])]
    con.execute("INSERT OR REPLACE INTO geo_cache VALUES (?,?)", (key, json.dumps(pcs)))
    return pcs

def epc_search(con, tok, postcode):
    row = con.execute("SELECT json FROM epc_postcode_cache WHERE postcode=?", (postcode,)).fetchone()
    if row:
        return json.loads(row[0])
    d = get_json(f"{API}/api/domestic/search?postcode={urllib.parse.quote_plus(postcode)}&page_size=5000",
                 {"Authorization": f"Bearer {tok}"}) or {}
    data = d.get("data") or []
    if isinstance(data, dict):  # tolerate either {"data": [...]} or {"data": {"certificates": [...]}}
        data = next((v for v in data.values() if isinstance(v, list)), [])
    con.execute("INSERT OR REPLACE INTO epc_postcode_cache VALUES (?,?,?)", (postcode, json.dumps(data), oc.now()))
    return data

def epc_cert(con, tok, cert):
    row = con.execute("SELECT json FROM epc_cert_cache WHERE cert=?", (cert,)).fetchone()
    if row:
        return json.loads(row[0])
    d = get_json(f"{API}/api/certificate?certificate_number={cert}", {"Authorization": f"Bearer {tok}"}) or {}
    data = d.get("data") or {}
    con.execute("INSERT OR REPLACE INTO epc_cert_cache VALUES (?,?,?)", (cert, json.dumps(data), oc.now()))
    return data

def g(d, *keys):
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
    return None

def street_of(title):
    # "2 Bed Flat, Glenton Road, SE13" -> "glenton road"
    parts = [p.strip() for p in (title or "").split(",")]
    s = parts[1] if len(parts) >= 3 else ""
    s = re.sub(r"^(flat|apartment)?\s*[a-z0-9]{1,3}\s+(?=[a-z])", "", s.lower())  # "A Wellmeadow Road" -> "wellmeadow road"
    return "" if s in ("london", "lewisham") else s

def stated_area(desc):
    d = (desc or "").lower().replace(",", "")
    m = re.search(r"(\d{3,4})\s*(?:sq\.?\s*ft|sqft|square f(?:ee|oo)t)", d)
    if m and 250 <= int(m.group(1)) <= 2000:
        return round(int(m.group(1)) * SQFT_TO_M2, 1)
    m = re.search(r"(\d{2,3}(?:\.\d)?)\s*(?:m2|m²|sq\.?\s*m\b|sqm|square met)", d)
    if m and 25 <= float(m.group(1)) <= 200:
        return float(m.group(1))
    return None

# ---------------------------------------------------------------------------
def estimate(con, tok, p):
    area = stated_area(p["description"])
    if area:
        return area, "stated in listing", 1, "", ""
    if not tok:
        return None, "needs EPC token", 0, "", ""
    if p["lat"] is None:
        return None, "no location", 0, "", ""
    street = street_of(p["title"])
    want_flat = not re.search(r"house|bungalow|cottage", (p["property_type"] or "").lower())

    latest = {}
    for pc in nearby_postcodes(con, p["lat"], p["lng"]):
        for c in epc_search(con, tok, pc):
            addr = " ".join(str(c.get(k) or "") for k in ("addressLine1", "addressLine2", "addressLine3", "addressLine4")).lower()
            if street and street.split()[0] not in addr:
                continue
            key = c.get("uprn") or addr
            if key not in latest or (c.get("registrationDate") or "") > (latest[key].get("registrationDate") or ""):
                latest[key] = c

    cands = []
    for c in latest.values():
        full = epc_cert(con, tok, c["certificateNumber"])
        a = g(full, "total_floor_area", "totalFloorArea")
        ptype = g(full, "property_type", "propertyType")
        try:
            ptype = PROPERTY_TYPES.get(int(ptype), str(ptype))
        except (TypeError, ValueError):
            ptype = str(ptype or "")
        is_flat = ptype.lower() in ("flat", "maisonette")
        if a is None or is_flat != want_flat:
            continue
        rooms = g(full, "habitable_room_count", "number_habitable_rooms", "habitableRoomCount")
        addr = ", ".join(str(c.get(k)) for k in ("addressLine1", "addressLine2") if c.get(k))
        cands.append((float(a), int(rooms) if str(rooms or "").isdigit() else None, f"{addr} {c.get('postcode','')}"))

    if not cands:
        return None, "no EPC match", 0, "", ""
    three = [c for c in cands if c[1] == 3]           # 2 bedrooms + 1 living room
    pool, basis = (three, "EPC: same street, 3 habitable rooms") if three else \
                  ([c for c in cands if 40 <= c[0] <= 95], "EPC: same street, similar-size flats")
    if not pool:
        return None, "no EPC match", 0, "", ""
    areas = sorted(c[0] for c in pool)
    if len(pool) == 1:
        basis = basis.replace("same street", "single match")
    return (round(statistics.median(areas), 1), basis, len(pool),
            f"{areas[0]:.0f}–{areas[-1]:.0f}", "; ".join(c[2] for c in pool[:6]))

def main():
    tok = token()
    con = oc.db()
    con.executescript(SCHEMA)
    con.row_factory = sqlite3.Row
    done = {r[0] for r in con.execute("SELECT property_id FROM floor_area WHERE area_m2 IS NOT NULL")}
    props = [dict(r) for r in con.execute("SELECT id, title, property_type, lat, lng, description FROM properties")]
    todo = [p for p in props if p["id"] not in done]
    print(f"{len(todo)} comparables to look up ({len(done)} already done)")
    for i, p in enumerate(todo, 1):
        area, basis, n, rng, addrs = estimate(con, tok, p)
        con.execute("INSERT OR REPLACE INTO floor_area VALUES (?,?,?,?,?,?,?)",
                    (p["id"], area, basis, n, rng, addrs, oc.now()))
        con.commit()
        print(f"[{i}/{len(todo)}] {p['title'][:45]:<45} {('%.0f m²' % area) if area else '—':>7}  {basis} (n={n})")
    got = con.execute("SELECT COUNT(*) FROM floor_area WHERE area_m2 IS NOT NULL").fetchone()[0]
    print(f"\nFloor area found for {got} of {len(props)} comparables. Now run: openrent_comps.py export")

if __name__ == "__main__":
    main()
