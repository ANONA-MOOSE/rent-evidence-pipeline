#!/usr/bin/env python3
"""
OpenRent comparable-rent collector (personal research use, low request rate).

  python3 openrent_comps.py crawl     # run every 1-2 weeks until you need the evidence
  python3 openrent_comps.py stats     # quick summary in the terminal
  python3 openrent_comps.py export    # build comparables.xlsx (needs: pip install openpyxl)

How it works (verified against the live site, Nov 2026):
  1. Area search pages embed JSON-like arrays for EVERY listing in the area
     (ids, price, beds, baths, garden, parking, bills, lat/lng, first-listed date,
     live vs let-agreed). Adding isLive=false includes recently let-agreed listings.
     No scrolling or browser needed.
  2. Each property page (openrent.co.uk/<id>) has the full description, the
     features table, and for let properties: "taken off the market (as of <date>)".
     Those pages stay up for years, so this is the "agreed" date.
  3. Each property links to /search/searchbylandlord?landlordID=N, which lists that
     landlord's whole history of listing ids (live and let-agreed).
     We fetch those ids and keep the 2-beds within RADIUS_KM of your flat.

Nothing is filtered by price at collection time on purpose: a tribunal will give
more weight to an honest, complete set of comparables than to a price-capped one.
"""

import gzip
import json
import math
import os
import random
import re
import sqlite3
import statistics
import ssl
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta
from html import unescape

# ----------------------------------------------------------------------------
# CONFIG — edit these
# ----------------------------------------------------------------------------
# Personal values (your flat, rent, defects, search areas) live in config_local.py, which is git-ignored.
# The defaults below are a neutral example; see config_example.py.
HOME_LAT, HOME_LNG = 51.4613, -0.0108          # example: central Lewisham
SUBJECT_POSTCODE = "SE13 7EZ"                  # example postcode
RADIUS_KM = 4.0                           # keep comparables within this distance (location-adjusted beyond the core)
TARGET_BEDS = 2

SEARCH_TERMS = ["Lewisham, London"]       # each returns everything within ~3km of the term

# Location: each comparable's neighbourhood (ONS median flat price for its MSOA, see area_prices.py) is
# compared with the subject's. Rent adjustment = (subject area price / comparable area price) ** β.
# β is estimated from our own let-agreed flats ("auto"), kept within LOCATION_BETA_LIMITS; otherwise
# LOCATION_BETA_FALLBACK. Comparables whose area price is more than LOC_MAX_RATIO times (or less than
# LOC_MIN_RATIO times) the subject's are a different market and are excluded, whatever their rent.
LOCATION_BETA = "auto"
LOCATION_BETA_FALLBACK = 0.6
LOCATION_BETA_LIMITS = (0.3, 1.0)
# Reference price level: "local_1km" = typical neighbourhood price of lets within 1 km (cautious; the
# subject's own MSOA price may be pulled down by its mix of flats), or "subject_msoa".
LOCATION_REFERENCE = "local_1km"

# Station access: straight-line km to the nearest rail/Overground/DLR station (OpenStreetMap). A comparable
# nearer a station than yours is superior: STATION_PCT_PER_KM
# per km of difference, capped at ±STATION_CAP_PCT. Judgement, shown line by line.
STATION_PCT_PER_KM = 3.0
STATION_CAP_PCT = 4.0
LOC_MAX_RATIO = 1.25
LOC_MIN_RATIO = 0.80
OUTLIER_IQR_K = 1.5        # after all adjustments, lets beyond Tukey fences (k x IQR, log scale) are excluded

SUBJECT = {                               # your flat, used for the similarity score and adjustments
    "property_type": "Flat", "bathrooms": 1, "garden": False, "parking": False,
    "ground_floor": False, "building_type": "period_conversion",
}
RENT_HISTORY = [("2025-01", 1800.00)]      # your rent, from each start date
PROPOSED_RENT_RANGE = None                # (low, high) of an expected proposal, shown as a band
SUBJECT_FLOOR_M2 = None                   # from your EPC
SUBJECT_EPC = "D"

# Size: where a comparable's floor area is known, its rent is scaled by (your m² / its m²) ** SIZE_ELASTICITY.
# Rent rises less than proportionally with size; 0.4 is a judgement (doubling the size adds ~32%), capped at ±SIZE_CAP_PCT.
SIZE_ELASTICITY = 0.4
SIZE_CAP_PCT = 12.0

# Adjustment grid (analysis.py): the best let-agreed comparables, each adjusted to be like your flat.
# Percentages are judgement calls, the same way a surveyor would make them. Edit freely; every one
# is shown in the grid so the tribunal can see (and you can defend) each step.
ADJUSTMENTS = {
    "two_doubles":    -5.0,   # comparable has two double bedrooms; yours is a double + a single
    "refurbished":    -5.0,   # comparable advertised as newly refurbished / renovated
    "extra_bathroom": -4.0,   # comparable has 2+ bathrooms
    "no_garden":      +2.0,   # comparable has no garden; yours has a small section of a divided garden
    "no_parking":     +1.0,   # comparable has no parking (applies only if SUBJECT["parking"] is True)
    "own_parking":    -1.0,   # comparable has its own/off-street parking; yours is a shared forecourt others use
    "epc_much_better": -3.0,  # comparable rated A or B; yours is E (single glazed, uninsulated solid walls)
    "epc_better":     -2.0,   # comparable rated C
    "epc_bit_better": -1.0,   # comparable rated D
    "modern_block":   -3.0,   # new-build / modern purpose-built block (lift, newness, amenities); yours is a period conversion
    "dated":          +3.0,   # comparable visibly dated (AI review of photos)
    "poor":           +6.0,   # comparable in poor condition / disrepair (AI review of photos)
}
# Deductions for your flat's own defects, by issue name in the defects log (evidence/defects/<issue>/).
# Applied automatically ONLY while the log shows the issue unresolved: the tribunal values the flat as it
# is on the day it decides. For scale, 2026 decisions gave about £50/month (~2.5%) per documented defect
# and £175/month for several; 15% for severe, wide-ranging disrepair.
DEFECT_ADJUSTMENTS = {}       # e.g. {"Kitchen floor": -4.0}; set yours in config_local.py
DASHBOARD_TITLE = "Two-bed rent comparables"
PUBLIC_DESCRIPTION = "an example two-bedroom flat"
PUBLIC_SITE_URL = ""
PUBLIC_REPO_URL = ""
PUBLIC_BLOCKLIST = []

GRID_SIZE = 8                 # comparables in the grid
GRID_MAX_KM = 2.0
GRID_MAX_MONTHS = 24

DELAY_S = (3.0, 6.0)                      # polite gap between requests
MAX_PROPERTY_FETCHES_PER_RUN = 200        # caps one run at roughly 15 minutes
LANDLORD_RECRAWL_DAYS = 21

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "openrent_data")
DB_PATH = os.path.join(DATA_DIR, "openrent.db")
SNAP_DIR = os.path.join(DATA_DIR, "snapshots")
BASE = "https://www.openrent.co.uk"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")

# Private overrides (your flat). Never committed.
try:
    from config_local import *  # noqa: F401,F403
except ImportError:
    pass

# ----------------------------------------------------------------------------
# HTTP
# ----------------------------------------------------------------------------
def _ssl_ctx():
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        pass
    for p in ("/etc/ssl/cert.pem", "/opt/homebrew/etc/openssl@3/cert.pem"):
        if os.path.exists(p):
            return ssl.create_default_context(cafile=p)
    return ssl.create_default_context()

SSL_CTX = _ssl_ctx()
_last_request = 0.0
_requests_made = 0

def fetch(path_or_url):
    """GET with a polite random delay. Returns (final_url, html) or (None, None)."""
    global _last_request, _requests_made
    url = path_or_url if path_or_url.startswith("http") else BASE + path_or_url
    wait = random.uniform(*DELAY_S) - (time.time() - _last_request)
    if wait > 0:
        time.sleep(wait)
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "en-GB,en;q=0.9"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=30, context=SSL_CTX) as r:
                _last_request = time.time()
                _requests_made += 1
                return r.geturl(), r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            _last_request = time.time()
            if e.code in (404, 410):
                return None, None
            if e.code in (403, 429):
                print(f"  ! {e.code} from OpenRent — backing off 60s"); time.sleep(60)
            else:
                print(f"  ! {e.code} from OpenRent — retrying"); time.sleep(10)
        except Exception as e:
            print(f"  ! {e} — retrying"); time.sleep(10)
    print("  ! OpenRent request failed after retries")
    return None, None

# ----------------------------------------------------------------------------
# Parsing
# ----------------------------------------------------------------------------
def js_array(html, name):
    m = re.search(name + r"\s*=\s*\[(.*?)\]", html, re.S)
    if not m:
        return None
    out = []
    for raw in m.group(1).split(","):
        raw = raw.strip().strip('"\'')
        if raw == "":
            continue
        try:
            out.append(float(raw) if "." in raw else int(raw))
        except ValueError:
            out.append(raw)
    return out

def parse_search(html):
    """Returns a list of dicts, one per listing on a search/landlord page."""
    ids = js_array(html, "PROPERTYIDS") or []
    cols = {k: js_array(html, k) for k in (
        "islivelistBool", "prices", "bedrooms", "bathrooms", "isshared", "isstudio",
        "furnished", "unfurnished", "gardens", "parkings", "bills", "dateFirstListedMs",
        "PROPERTYLISTLATITUDES", "PROPERTYLISTLONGITUDES")}
    rows = []
    for i, pid in enumerate(ids):
        g = lambda k: (cols[k][i] if cols[k] and i < len(cols[k]) else None)
        rows.append({
            "id": str(pid), "live": g("islivelistBool"), "price": g("prices"),
            "beds": g("bedrooms"), "baths": g("bathrooms"), "shared": g("isshared"),
            "studio": g("isstudio"), "garden": g("gardens"), "parking": g("parkings"),
            "bills": g("bills"), "first_listed_ms": g("dateFirstListedMs"),
            "lat": g("PROPERTYLISTLATITUDES"), "lng": g("PROPERTYLISTLONGITUDES"),
        })
    return rows

def strip_tags(s):
    s = re.sub(r"<br\s*/?>", "\n", s)
    s = re.sub(r"<svg.*?</svg>", "", s, flags=re.S)
    s = re.sub(r"<[^>]+>", " ", s)
    s = unescape(s).replace("\xa0", " ")
    return re.sub(r"[ \t]+", " ", re.sub(r"\n\s*", "\n", s)).strip()

def money(s):
    m = re.search(r"£\s*([\d,]+(?:\.\d+)?)", s or "")
    return float(m.group(1).replace(",", "")) if m else None

def parse_property(html, url):
    d = {"url": url}
    m = re.search(r"<h1[^>]*>(.*?)</h1>", html, re.S)
    d["title"] = strip_tags(m.group(1)) if m else None
    t = d["title"] or ""
    mb = re.match(r"(\d+)\s+Bed\s+(.+?),", t)
    d["beds"] = int(mb.group(1)) if mb else (0 if t.lower().startswith("studio") else None)
    d["property_type"] = mb.group(2).strip() if mb else None
    d["postcode_district"] = t.rsplit(",", 1)[-1].strip() if "," in t else None

    m = re.search(r">\s*(\d+)\s*<span[^>]*>\s*bathrooms", html)
    d["baths"] = int(m.group(1)) if m else None
    m = re.search(r">\s*(\d+)\s+tenants max", html)
    d["max_tenants"] = int(m.group(1)) if m else None

    m = re.search(r'data-lat="(-?[\d.]+)"\s*data-lng="(-?[\d.]+)"', html)
    d["lat"], d["lng"] = (float(m.group(1)), float(m.group(2))) if m else (None, None)

    m = re.search(r"searchbylandlord\?landlordID=(\d+)", html)
    d["landlord_id"] = m.group(1) if m else None
    m = re.search(r"Meet the Landlord</h2>(.*?)Response", html, re.S)
    d["landlord_name"] = strip_tags(m.group(1)).strip() if m else None

    m = re.search(r'id="descriptionText"[^>]*>(.*?)<button', html, re.S)
    d["description"] = strip_tags(m.group(1)) if m else None

    # features tables: text cells, or tick/cross icons
    feats = {}
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S):
        cells = re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row, re.S)
        if len(cells) < 2:
            continue
        key = strip_tags(cells[0]).strip().rstrip(":")
        val_html = cells[1]
        val = strip_tags(val_html).strip()
        if not val:
            if "text-success" in val_html: val = True
            elif "text-danger" in val_html: val = False
        if key and len(key) < 40:
            feats[key] = val
    d["features"] = feats
    d["price_pcm"] = money(feats.get("Rent PCM") if isinstance(feats.get("Rent PCM"), str) else None)
    if d["price_pcm"] is None:
        d["price_pcm"] = money(re.search(r"<title>(.*?)</title>", html, re.S).group(1)) if "<title>" in html else None
    d["deposit"] = money(feats.get("Deposit") if isinstance(feats.get("Deposit"), str) else None)
    d["furnishing"] = feats.get("Furnishing") if isinstance(feats.get("Furnishing"), str) else None
    d["epc"] = feats.get("EPC Rating") if isinstance(feats.get("EPC Rating"), str) else None
    d["garden"], d["parking"], d["bills_included"] = feats.get("Garden"), feats.get("Parking"), feats.get("Bills Included")
    d["available_from_text"] = feats.get("Available From") if isinstance(feats.get("Available From"), str) else None

    m = re.search(r"taken off the market \(as of ([^)]+)\)", html)
    d["status"] = "let_agreed" if (m or "Let Agreed - This property is no longer" in html) else "available"
    d["let_agreed_date"] = None
    if m:
        try:
            d["let_agreed_date"] = datetime.strptime(m.group(1).strip(), "%A, %d %B %Y").date().isoformat()
        except ValueError:
            d["let_agreed_date"] = m.group(1).strip()

    d["flags"] = desc_flags(d["description"])
    return d

def desc_flags(description):
    desc = (description or "").lower()
    return {
        "ground_floor": bool(re.search(r"ground[- ]floor|garden flat", desc)),
        "single_bedroom": bool(re.search(r"single (bed)?room|box ?room|one double and one single|1 double.{0,20}1 single", desc)),
        "two_doubles": bool(re.search(r"two double|2 double|both (bed)?rooms? (are )?doubles?", desc)),
        "refurbished": bool(re.search(r"newly refurb|recently refurb|brand new (kitchen|bathroom|flooring|carpets)|newly renovated|luxury|fully refurbished", desc)),
        # Building type: a modern block (new-build, purpose-built, lift, concierge, named developments) vs a
        # period conversion like the subject. Both or neither -> unknown (the AI photo review decides).
        "modern_block": bool(re.search(r"new[- ]?build|newly built|built in 20[012]\d|purpose[- ]built|concierge|\blifts?\b|"
                                       r"catford green|lewisham gateway|lewisham exchange|on[- ]site gym|residents'? (lounge|gym)|"
                                       r"modern (block|development|building)|new development|juliet balcon", desc)),
        "period": bool(re.search(r"victorian|edwardian|georgian|period (property|conversion|building|house|features)|"
                                 r"conversion|converted|high ceilings?|bay windows?|original features", desc)),
        "ensuite": "en-suite" in desc or "ensuite" in desc or "en suite" in desc,
        "hmo_or_room": bool(re.search(r"\broom (to|for) rent\b|per room|flatshare|house ?share", desc)),
        "short_let": bool(re.search(r"short[- ]let|holiday let|serviced apartment", desc)),
    }

def km(lat1, lng1, lat2, lng2):
    if None in (lat1, lng1, lat2, lng2):
        return None
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return round(2 * r * math.asin(math.sqrt(a)), 2)

# ----------------------------------------------------------------------------
# Storage
# ----------------------------------------------------------------------------
SCHEMA = """
CREATE TABLE IF NOT EXISTS properties (
  id TEXT PRIMARY KEY, url TEXT, title TEXT, property_type TEXT, postcode_district TEXT,
  beds INTEGER, baths INTEGER, max_tenants INTEGER,
  price_pcm REAL, deposit REAL, furnishing TEXT, epc TEXT,
  garden INTEGER, parking INTEGER, bills_included INTEGER,
  status TEXT, let_agreed_date TEXT, first_listed TEXT, available_from_text TEXT,
  lat REAL, lng REAL, distance_km REAL,
  landlord_id TEXT, description TEXT, flags_json TEXT, features_json TEXT,
  found_via TEXT, first_seen TEXT, last_checked TEXT
);
CREATE TABLE IF NOT EXISTS observations (           -- one row each time price/status changes
  property_id TEXT, observed_at TEXT, status TEXT, price_pcm REAL, snapshot TEXT
);
CREATE TABLE IF NOT EXISTS landlords (
  id TEXT PRIMARY KEY, name TEXT, n_listings INTEGER, last_crawled TEXT
);
CREATE TABLE IF NOT EXISTS skipped (                 -- ids fetched but not relevant (wrong size/area)
  id TEXT PRIMARY KEY, reason TEXT, checked TEXT, title TEXT, landlord_id TEXT
);
CREATE TABLE IF NOT EXISTS floor_area (            -- filled by epc_lookup.py
  property_id TEXT PRIMARY KEY, area_m2 REAL, basis TEXT, n_matches INTEGER,
  range_m2 TEXT, matched_addresses TEXT, updated TEXT
);
CREATE TABLE IF NOT EXISTS review (                  -- your / Claude's manual assessment, never overwritten by crawls
  property_id TEXT PRIMARY KEY, include INTEGER, layout TEXT, floor TEXT, condition TEXT,
  adjustment_note TEXT, reviewed_at TEXT
);
"""

def db():
    os.makedirs(SNAP_DIR, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.executescript(SCHEMA)
    for col in ("title", "landlord_id"):          # upgrade older databases
        try:
            con.execute(f"ALTER TABLE skipped ADD COLUMN {col} TEXT")
        except sqlite3.OperationalError:
            pass
    return con

def rule_out(con, pid, reason, title=None, landlord_id=None):
    con.execute("INSERT OR REPLACE INTO skipped VALUES (?,?,?,?,?)", (pid, reason, now(), title, landlord_id))

def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

def b(v):
    return None if v is None or v == "" else int(bool(v))

def save_snapshot(pid, html):
    path = os.path.join(SNAP_DIR, f"{pid}_{datetime.now().strftime('%Y-%m-%d')}.html.gz")
    with gzip.open(path, "wt", encoding="utf-8") as f:
        f.write(html)
    return os.path.relpath(path, DATA_DIR)

def upsert_property(con, d, found_via, search_row=None):
    cur = con.execute("SELECT status, price_pcm, first_seen, first_listed FROM properties WHERE id=?", (d["id"],))
    old = cur.fetchone()
    first_listed = old[3] if old else None
    if search_row and search_row.get("first_listed_ms"):
        first_listed = datetime.fromtimestamp(search_row["first_listed_ms"] / 1000, timezone.utc).date().isoformat()
    con.execute("""INSERT INTO properties VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
      ON CONFLICT(id) DO UPDATE SET url=excluded.url, title=excluded.title, property_type=excluded.property_type,
        postcode_district=excluded.postcode_district, beds=excluded.beds, baths=excluded.baths,
        max_tenants=excluded.max_tenants, price_pcm=excluded.price_pcm, deposit=excluded.deposit,
        furnishing=excluded.furnishing, epc=excluded.epc, garden=excluded.garden, parking=excluded.parking,
        bills_included=excluded.bills_included, status=excluded.status, let_agreed_date=excluded.let_agreed_date,
        first_listed=excluded.first_listed, available_from_text=excluded.available_from_text,
        lat=excluded.lat, lng=excluded.lng, distance_km=excluded.distance_km, landlord_id=excluded.landlord_id,
        description=excluded.description, flags_json=excluded.flags_json, features_json=excluded.features_json,
        last_checked=excluded.last_checked""",
        (d["id"], d["url"], d["title"], d["property_type"], d["postcode_district"], d["beds"], d["baths"],
         d["max_tenants"], d["price_pcm"], d["deposit"], d["furnishing"], d["epc"],
         b(d["garden"]), b(d["parking"]), b(d["bills_included"]), d["status"], d["let_agreed_date"],
         first_listed, d["available_from_text"], d["lat"], d["lng"], d["distance_km"], d["landlord_id"],
         d["description"], json.dumps(d["flags"]), json.dumps(d["features"], default=str),
         found_via, old[2] if old else now(), now()))
    changed = (not old) or old[0] != d["status"] or old[1] != d["price_pcm"]
    return changed

# ----------------------------------------------------------------------------
# Crawl
# ----------------------------------------------------------------------------
def fetch_and_store_property(con, pid, found_via, search_row=None):
    url, html = fetch(f"/{pid}")
    if not html:
        rule_out(con, pid, "gone")
        return None
    d = parse_property(html, url)
    d["id"] = pid
    if d["lat"] is None and search_row:
        d["lat"], d["lng"] = search_row.get("lat"), search_row.get("lng")
    d["distance_km"] = km(HOME_LAT, HOME_LNG, d["lat"], d["lng"])
    if d["beds"] != TARGET_BEDS:
        rule_out(con, pid, f"beds={d['beds']}", d["title"], d["landlord_id"])
        return d
    if d["distance_km"] is None or d["distance_km"] > RADIUS_KM:
        rule_out(con, pid, f"distance={d['distance_km']}", d["title"], d["landlord_id"])
        return d
    if upsert_property(con, d, found_via, search_row):
        snap = save_snapshot(pid, html)
        con.execute("INSERT INTO observations VALUES (?,?,?,?,?)", (pid, now(), d["status"], d["price_pcm"], snap))
        print(f"  + {d['status']:<10} £{d['price_pcm'] or 0:>6.0f}  {d['distance_km']:>4}km  {d['title']}"
              + (f"  (let {d['let_agreed_date']})" if d["let_agreed_date"] else ""))
    con.commit()
    return d

def crawl():
    con = db()
    budget = MAX_PROPERTY_FETCHES_PER_RUN
    known = {r[0]: (r[1], r[2]) for r in con.execute("SELECT id, status, price_pcm FROM properties")}
    skipped = {r[0] for r in con.execute("SELECT id FROM skipped")}

    # 1. Area searches (live + recently let agreed)
    search_rows = {}
    for term in SEARCH_TERMS:
        q = urllib.parse.quote(term)
        print(f"[search] {term}")
        _, html = fetch(f"/properties-to-rent/?term={q}&isLive=false")
        if not html:
            continue
        for r in parse_search(html):
            if r["beds"] == TARGET_BEDS and not r["shared"] and not r["studio"]:
                if km(HOME_LAT, HOME_LNG, r["lat"], r["lng"]) is not None and km(HOME_LAT, HOME_LNG, r["lat"], r["lng"]) <= RADIUS_KM:
                    search_rows[r["id"]] = r
    print(f"[search] {len(search_rows)} two-bed listings within {RADIUS_KM}km")

    # 2. Property pages: new ones, or ones whose status/price changed in search
    landlord_ids = set()
    # let-agreed first (strongest evidence), then nearest first
    order = sorted(search_rows.items(), key=lambda kv: (kv[1]["live"] == 1, km(HOME_LAT, HOME_LNG, kv[1]["lat"], kv[1]["lng"])))
    for pid, r in order:
        status = "available" if r["live"] == 1 else "let_agreed"
        if pid in known and known[pid] == (status, r["price"]):
            continue
        if budget <= 0:
            break
        budget -= 1
        d = fetch_and_store_property(con, pid, "search", r)
        if d and d.get("landlord_id"):
            landlord_ids.add(d["landlord_id"])
    # 2b. Listings we last saw advertised that have dropped out of search: re-check
    #     them so we catch the flip to let agreed (and its date).
    stale_cutoff = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
    stale = [r[0] for r in con.execute(
        "SELECT id FROM properties WHERE status='available' AND last_checked < ?", (stale_cutoff,))
        if r[0] not in search_rows]
    if stale:
        print(f"[recheck] {len(stale)} previously advertised listings no longer in search")
    for pid in stale:
        if budget <= 0:
            break
        budget -= 1
        fetch_and_store_property(con, pid, "recheck")

    landlord_ids |= {r[0] for r in con.execute("SELECT DISTINCT landlord_id FROM properties WHERE landlord_id IS NOT NULL")}

    # 3. Landlord histories
    cutoff = (datetime.now(timezone.utc) - timedelta(days=LANDLORD_RECRAWL_DAYS)).isoformat()
    recent = {r[0] for r in con.execute("SELECT id FROM landlords WHERE last_crawled > ?", (cutoff,))}
    for lid in sorted(landlord_ids - recent):
        if budget <= 0:
            print("[landlord] fetch budget used up — the rest will be picked up next run")
            break
        _, html = fetch(f"/search/searchbylandlord?landlordID={lid}")
        if not html:
            continue
        rows = parse_search(html)
        # card links carry the bedroom count in the slug, e.g. /2-bed-flat-glenton-road-se13/3075322
        slugs = {m.group(2): m.group(1) for m in re.finditer(r'href="/property-to-rent/[^/"]+/([^/"]+)/(\d+)"', html)}
        todo = []
        for r in rows:
            pid = r["id"]
            if pid in known or pid in skipped or pid in search_rows:
                continue
            dist = km(HOME_LAT, HOME_LNG, r["lat"], r["lng"])
            slug = slugs.get(pid, "")
            if dist is not None and dist > RADIUS_KM + 0.5:
                rule_out(con, pid, f"prefilter: {dist}km away", slug, lid)
                continue
            if slug and not slug.startswith(f"{TARGET_BEDS}-bed"):
                rule_out(con, pid, "prefilter: " + slug.split("-")[0] + " bed", slug, lid)
                continue
            todo.append(pid)
        print(f"[landlord {lid}] {len(rows)} listings in history, {len(todo)} nearby {TARGET_BEDS}-beds to check")
        finished = True
        for pid in todo:
            if budget <= 0:
                finished = False
                break
            budget -= 1
            fetch_and_store_property(con, pid, f"landlord:{lid}")
            skipped.add(pid)
        if finished:
            con.execute("INSERT OR REPLACE INTO landlords VALUES (?,?,?,?)", (lid, None, len(rows), now()))
        con.commit()

    con.commit()
    print(f"\nDone: {_requests_made} requests. "
          f"{con.execute('SELECT COUNT(*) FROM properties').fetchone()[0]} comparables in database.")

# ----------------------------------------------------------------------------
# Scoring + export
# ----------------------------------------------------------------------------
def similarity(row):
    """Transparent 0-100 score: how like-for-like is this property? (not how cheap)"""
    s, why = 0, []
    dist = row["distance_km"] or 99
    pts = 30 if dist <= 0.5 else 25 if dist <= 1 else 18 if dist <= 1.5 else 10 if dist <= 2 else 4
    s += pts; why.append(f"{dist}km")
    if row["status"] == "let_agreed":
        s += 15
    ref = row["let_agreed_date"] or row["first_listed"]
    try:
        age = (datetime.now().date() - datetime.fromisoformat(ref).date()).days
        s += 15 if age <= 183 else 8 if age <= 365 else 0
        mo = age // 30
        when = "this month" if age < 31 else f"{mo} month{'s' if mo != 1 else ''} ago"
        why.append(("let " if row["status"] == "let_agreed" else "listed ") + when)
    except (TypeError, ValueError):
        pass
    if (row["property_type"] or "").lower().startswith(SUBJECT["property_type"].lower()):
        s += 10; why.append("flat")
    if row["baths"] == SUBJECT["bathrooms"]:
        s += 8
    if bool(row["garden"]) == SUBJECT["garden"]:
        s += 6
    if bool(row["parking"]) == SUBJECT["parking"]:
        s += 4
    f = desc_flags(row["description"])
    layout, floor, cond = (row.get("layout") or "").lower(), (row.get("floor") or "").lower(), (row.get("condition") or "").lower()
    if "single" in layout:
        f["single_bedroom"], f["two_doubles"] = True, False
    elif "two double" in layout:
        f["two_doubles"], f["single_bedroom"] = True, False
    if floor:
        f["ground_floor"] = floor == "ground"
    if cond:
        f["refurbished"] = cond == "newly refurbished"
    if f.get("ground_floor") == SUBJECT["ground_floor"]:
        s += 6; why.append("ground floor")
    if f.get("single_bedroom"):
        s += 6; why.append("has a single bedroom")
    elif f.get("two_doubles"):
        why.append("two doubles (superior)")
    elif row.get("max_tenants") == 3:
        s += 4; why.append("max 3 tenants, likely double + single")
    elif (row.get("max_tenants") or 0) >= 4:
        why.append("max 4+ tenants, likely two doubles (superior)")
    if f.get("refurbished"):
        s -= 6; why.append("refurbished (superior)")
    bt = row.get("building_type")
    if bt == SUBJECT["building_type"]:
        s += 6; why.append("period conversion")
    elif bt == "modern_block":
        s -= 6; why.append("modern block (different kind of flat)")
    return max(0, min(100, s)), ", ".join(why)

def ons_factors(con):
    """{ 'YYYY-MM': factor } bringing a rent from that month to the latest ONS month (Lewisham 2-bed index)."""
    try:
        idx = dict(con.execute("SELECT period, index_2bed FROM ons_rents WHERE area='Lewisham' AND index_2bed IS NOT NULL"))
    except sqlite3.OperationalError:
        return {}, None
    if not idx:
        return {}, None
    latest = max(idx)
    return {p: idx[latest] / v for p, v in idx.items()}, latest

LOCATION_INFO = {}


def load_rows(con):
    factors, ons_latest = ons_factors(con)
    import ai_review
    import area_prices
    con.executescript(ai_review.SCHEMA)
    con.executescript(area_prices.SCHEMA)
    areas = area_prices.lookup(con)
    try:
        subj_area = area_prices.subject_area(con)
    except Exception:
        subj_area = {"price": None}
    con.row_factory = sqlite3.Row
    rows = [dict(r) for r in con.execute("""
        SELECT p.*, r.include, r.layout, r.floor, r.condition, r.adjustment_note,
               fa.area_m2, fa.basis AS area_basis, fa.n_matches AS area_n, fa.range_m2 AS area_range,
               ai.result AS ai_json
        FROM properties p LEFT JOIN review r ON r.property_id = p.id
        LEFT JOIN floor_area fa ON fa.property_id = p.id
        LEFT JOIN ai_review ai ON ai.property_id = p.id""")]
    # Price history we've seen (one observation per price/status change, since we started crawling).
    hist = {}
    for pid, price in con.execute("SELECT property_id, price_pcm FROM observations WHERE price_pcm IS NOT NULL ORDER BY observed_at"):
        hist.setdefault(pid, []).append(price)
    today = datetime.now().date()
    for r in rows:
        f = desc_flags(r["description"])
        # Time on the market and price cuts: listings that sat unlet or were cut are evidence the
        # market wouldn't bear the asking rent.
        prices = [p for i, p in enumerate(hist.get(r["id"], [])) if i == 0 or p != hist[r["id"]][i - 1]]
        r["first_price"] = prices[0] if prices else r["price_pcm"]
        r["price_changes"] = max(0, len(prices) - 1)
        r["price_cut"] = round(r["first_price"] - r["price_pcm"]) if r["first_price"] and r["price_pcm"] else 0
        try:
            end = datetime.fromisoformat(r["let_agreed_date"]).date() if r["let_agreed_date"] else today
            r["days_advertised"] = (end - datetime.fromisoformat(r["first_listed"]).date()).days if r["first_listed"] else None
        except ValueError:
            r["days_advertised"] = None
        r["ai"] = json.loads(r["ai_json"]) if r.get("ai_json") else None
        ai = r["ai"] or {}
        # AI findings fill anything not set by a manual review (manual always wins).
        if not r["layout"] and ai.get("second_bedroom") in ("double", "single"):
            r["layout"] = "two doubles" if ai["second_bedroom"] == "double" else "double + single"
        if not r["condition"] and ai.get("condition") not in (None, "unclear"):
            r["condition"] = ai["condition"].replace("_", " ")
        if not r["floor"] and ai.get("floor_level") not in (None, "unclear"):
            r["floor"] = ai["floor_level"].replace("_", " ")
        bt = ai.get("building_type")
        epc_letter = (r.get("epc") or "").strip().upper()[:1]
        if bt in ("period_conversion", "modern_block", "older_purpose_built", "house"):
            r["building_type"], r["building_basis"] = bt, "AI photo review"
        elif f.get("modern_block") and not f.get("period"):
            r["building_type"], r["building_basis"] = "modern_block", "listing text"
        elif f.get("period") and not f.get("modern_block"):
            r["building_type"], r["building_basis"] = "period_conversion", "listing text"
        elif epc_letter in ("A", "B"):        # A/B ratings are, in practice, modern construction
            r["building_type"], r["building_basis"] = "modern_block", f"energy rating {epc_letter}"
        else:
            r["building_type"], r["building_basis"] = None, ""
        r["exclude_reason"] = ("beyond radius" if (r["distance_km"] or 0) > RADIUS_KM else
                               "bills included" if r["bills_included"] else
                               "room/HMO" if f.get("hmo_or_room") else
                               "short let" if f.get("short_let") else
                               "excluded on review" if r["include"] == 0 else
                               "AI: not a self-contained 2-bed" if ai.get("is_self_contained_two_bed") is False
                               and ai.get("confidence") == "high" else "")
        r["score"], r["score_why"] = similarity(r)
        # A floor plan read by the AI review beats an EPC-based estimate; a size stated in the text is equal.
        if ai.get("floor_area_m2") and 20 <= ai["floor_area_m2"] <= 200 and (r.get("area_basis") or "").startswith(("EPC", "needs", "no ")) or \
                (ai.get("floor_area_m2") and not r.get("area_m2")):
            r["area_m2"], r["area_basis"] = round(ai["floor_area_m2"], 1), "AI: " + (ai.get("floor_area_source") or "floor plan")
        r["per_m2"] = round(r["price_pcm"] / r["area_m2"], 2) if r.get("area_m2") and r["price_pcm"] else None
        # Rent in today's money: scale by the ONS Lewisham 2-bed index from the month it was let
        # (or listed) to the latest ONS month. Lets newer than the ONS data are left as they are.
        ref = (r["let_agreed_date"] or r["first_listed"] or (r["first_seen"] or "")[:10])[:7]
        r["time_factor"] = round(factors.get(ref, 1.0), 4) if ref and ons_latest and ref <= ons_latest else 1.0
        r["price_today"] = round(r["price_pcm"] * r["time_factor"]) if r["price_pcm"] else None
        a = areas.get(f"{r['lat']:.5f},{r['lng']:.5f}") if r["lat"] is not None else None
        r["msoa"], r["area_name"], r["area_price"] = a if a else (None, None, None)
        r["station"], r["station_km"] = area_prices.nearest_station(r["lat"], r["lng"])

    # Reference price level for "like the subject's location".
    near = sorted(r["area_price"] for r in rows if r["area_price"] and not r["exclude_reason"]
                  and r["status"] == "let_agreed" and (r["distance_km"] or 99) <= 1.0)
    ref_price = (statistics.median(near) if LOCATION_REFERENCE == "local_1km" and near else subj_area.get("price"))
    for r in rows:
        r["area_ratio"] = (r["area_price"] / ref_price) if r["area_price"] and ref_price else None

    # How strongly do rents here follow neighbourhood prices? Estimate from our own lets, then adjust.
    est = area_prices.estimate_elasticity(rows)
    if LOCATION_BETA == "auto" and est:
        beta, basis = min(max(est["beta"], LOCATION_BETA_LIMITS[0]), LOCATION_BETA_LIMITS[1]), "estimated"
    else:
        beta, basis = (LOCATION_BETA if isinstance(LOCATION_BETA, (int, float)) else LOCATION_BETA_FALLBACK), "set"
    global LOCATION_INFO
    LOCATION_INFO = {"beta": round(beta, 2), "basis": basis, "estimate": est, "subject": subj_area,
                     "reference": LOCATION_REFERENCE, "ref_price": ref_price,
                     "subject_station": dict(zip(("name", "km"), area_prices.nearest_station(HOME_LAT, HOME_LNG))),
                     "max_ratio": LOC_MAX_RATIO, "min_ratio": LOC_MIN_RATIO}
    for r in rows:
        if r["area_ratio"]:
            r["loc_factor"] = round(r["area_ratio"] ** beta, 4)
            r["price_loc"] = round(r["price_today"] / r["loc_factor"]) if r["price_today"] else None
            if not r["exclude_reason"] and not (LOC_MIN_RATIO <= r["area_ratio"] <= LOC_MAX_RATIO):
                r["exclude_reason"] = f"different price area ({r['area_name']}: flats ×{r['area_ratio']:.2f} the local level)"
        else:
            r["loc_factor"], r["price_loc"] = None, r["price_today"]

    # Statistical outliers: after time and location adjustment, lets far outside the rest (Tukey fences on
    # the log scale) are excluded and labelled, so one odd let can't move the result either way.
    vals = sorted(math.log(r["price_loc"]) for r in rows if not r["exclude_reason"] and r["status"] == "let_agreed" and r.get("price_loc"))
    if len(vals) >= 12:
        q1, q3 = vals[len(vals) // 4], vals[(3 * len(vals)) // 4]
        lo, hi = q1 - OUTLIER_IQR_K * (q3 - q1), q3 + OUTLIER_IQR_K * (q3 - q1)
        for r in rows:
            if not r["exclude_reason"] and r.get("price_loc") and not (lo <= math.log(r["price_loc"]) <= hi):
                r["exclude_reason"] = f"statistical outlier (adjusted £{r['price_loc']:,} outside £{math.exp(lo):,.0f}–£{math.exp(hi):,.0f})"

    # Same flat advertised twice at once (landlords sometimes double-list):
    # same title + same rent + dates within 3 weeks -> keep the earliest id only.
    seen = {}
    for r in sorted(rows, key=lambda r: int(r["id"])):
        if r["exclude_reason"]:
            continue
        ref = r["let_agreed_date"] or r["first_listed"] or r["first_seen"][:10]
        key = ((r["title"] or "").lower(), r["price_pcm"])
        for prev_ref, prev_id, prev_status in seen.get(key, []):
            try:
                both_live = r["status"] == prev_status == "available"
                if both_live or abs((datetime.fromisoformat(ref) - datetime.fromisoformat(prev_ref)).days) <= 21:
                    r["exclude_reason"] = f"duplicate of {prev_id}"
                    break
            except (TypeError, ValueError):
                pass
        if not r["exclude_reason"]:
            seen.setdefault(key, []).append((ref, r["id"], r["status"]))
    return rows

def quartiles(vals):
    v = sorted(x for x in vals if x)
    if not v:
        return None
    q = lambda p: v[min(len(v) - 1, int(round(p * (len(v) - 1))))]
    return {"n": len(v), "lower_q": q(0.25), "median": q(0.5), "upper_q": q(0.75), "min": v[0], "max": v[-1]}

def stats():
    con = db()
    rows = [r for r in load_rows(con) if not r["exclude_reason"]]
    year_ago = (datetime.now().date() - timedelta(days=365)).isoformat()
    groups = {
        "All 2-bed comparables": rows,
        "Let agreed (any date)": [r for r in rows if r["status"] == "let_agreed"],
        "Let agreed, last 12 months": [r for r in rows if r["status"] == "let_agreed" and (r["let_agreed_date"] or "") >= year_ago],
        "Let agreed, last 12m, within 1km": [r for r in rows if r["status"] == "let_agreed" and (r["let_agreed_date"] or "") >= year_ago and (r["distance_km"] or 99) <= 1],
        "Currently advertised": [r for r in rows if r["status"] == "available"],
    }
    for name, g in groups.items():
        q = quartiles([r["price_pcm"] for r in g])
        print(f"{name:<36}" + (f"n={q['n']:<4} median £{q['median']:,.0f}  IQR £{q['lower_q']:,.0f}–£{q['upper_q']:,.0f}" if q else "n=0"), end="")
        qm = quartiles([r["per_m2"] for r in g])
        print(f"   | £/m² n={qm['n']} median £{qm['median']:.2f}" if qm else "")
    return groups

def export(path=None):
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Font, PatternFill, Alignment
    except ImportError:
        sys.exit("Export needs openpyxl:  pip3 install openpyxl")
    path = path or os.path.join(DATA_DIR, f"comparables_{datetime.now():%Y-%m-%d}.xlsx")
    con = db()
    rows = load_rows(con)
    groups = stats()

    cols = [("Score", "score", 7), ("Status", "status", 11), ("Rent £pcm", "price_pcm", 10),
            ("In today's money", "price_today", 10), ("Location-adjusted", "price_loc", 10),
            ("Neighbourhood", "area_name", 22), ("Area price ×local", "area_ratio", 9), ("Building", "building_type", 14), ("Nearest station", "station", 14), ("Station km", "station_km", 7),
            ("Let agreed on", "let_agreed_date", 13), ("First listed", "first_listed", 12),
            ("Days advertised", "days_advertised", 9), ("Price cut £", "price_cut", 8),
            ("Distance km", "distance_km", 9), ("Postcode", "postcode_district", 9),
            ("Title", "title", 38), ("Type", "property_type", 12), ("Baths", "baths", 6), ("Max tenants", "max_tenants", 8),
            ("Garden", "garden", 7), ("Parking", "parking", 7), ("Furnishing", "furnishing", 14),
            ("EPC", "epc", 5), ("Floor m² (est.)", "area_m2", 9), ("£ per m²", "per_m2", 8),
            ("Area basis", "area_basis", 26), ("Area range m²", "area_range", 10), ("Why scored", "score_why", 34), ("Review note", "adjustment_note", 30),
            ("Excluded because", "exclude_reason", 16), ("URL", "url", 40), ("Description", "description", 60)]
    head_fill = PatternFill("solid", start_color="1F4E78")

    wb = Workbook()
    ws = wb.active
    ws.title = "Summary"
    ws["A1"] = "Two-bedroom rental comparables near the subject flat (source: OpenRent)"
    ws["A1"].font = Font(bold=True, size=13)
    ws["A2"] = f"Generated {datetime.now():%d %B %Y}. Radius {RADIUS_KM}km. Bills-included, room/HMO and short lets excluded."
    ws.append([])
    ws.append(["Group", "Count", "Lower quartile", "Median", "Upper quartile", "Min", "Max",
               "n with floor area", "Median £/m²", "Implied rent for subject"])
    for c in ws[4]:
        c.font = Font(bold=True, color="FFFFFF"); c.fill = head_fill
    for name, g in groups.items():
        q = quartiles([r["price_pcm"] for r in g])
        qm = quartiles([r["per_m2"] for r in g])
        row = [name] + ([q["n"], q["lower_q"], q["median"], q["upper_q"], q["min"], q["max"]] if q else [0, None, None, None, None, None])
        row += [qm["n"], qm["median"], round(qm["median"] * SUBJECT_FLOOR_M2) if SUBJECT_FLOOR_M2 else "set SUBJECT_FLOOR_M2"] if qm else [0]
        ws.append(row)
    ws.append([])
    ws.append(["Floor areas are estimates from EPC certificates on the same street (see 'Area basis'); "
               "£/m² tends to be higher for smaller flats, so treat the implied rent as a cross-check, not a valuation."])
    ws.column_dimensions["A"].width = 38
    for col in "IJ":
        ws.column_dimensions[col].width = 16
        for c in ws[col][4:]:
            c.number_format = "£#,##0.00" if col == "I" else "£#,##0"
    for col in "CDEFG":
        ws.column_dimensions[col].width = 15
        for c in ws[col][4:]:
            c.number_format = "£#,##0"

    def sheet(title, data):
        s = wb.create_sheet(title)
        s.append([c[0] for c in cols])
        for c in s[1]:
            c.font = Font(bold=True, color="FFFFFF"); c.fill = head_fill
            c.alignment = Alignment(wrap_text=True, vertical="top")
        for r in data:
            vals = []
            for _, k, _w in cols:
                v = r.get(k)
                if k in ("garden", "parking"):
                    v = "" if v is None else ("Yes" if v else "No")
                if k == "status":
                    v = "Let agreed" if v == "let_agreed" else "Advertised"
                vals.append(v)
            s.append(vals)
            link = s.cell(row=s.max_row, column=[k for _, k, _ in cols].index("url") + 1)
            link.hyperlink = r["url"]; link.style = "Hyperlink"
        for i, (_, _, w) in enumerate(cols, 1):
            s.column_dimensions[s.cell(1, i).column_letter].width = w
        for col in "CD":
            for c in s[col][1:]:
                c.number_format = "£#,##0"
        s.freeze_panes = "B2"
        s.auto_filter.ref = s.dimensions

    good = sorted([r for r in rows if not r["exclude_reason"]], key=lambda r: (-r["score"], r["price_pcm"] or 0))
    sheet("Best matches", good[:100])
    sheet("Let agreed", [r for r in good if r["status"] == "let_agreed"])
    sheet("Everything", sorted(rows, key=lambda r: -r["score"]))

    import analysis
    def header(s, names):
        s.append(names)
        for c in s[s.max_row]:
            c.font = Font(bold=True, color="FFFFFF"); c.fill = head_fill
            c.alignment = Alignment(wrap_text=True, vertical="top")

    g = analysis.grid(rows)
    s = wb.create_sheet("Adjustment grid", 1)
    s["A1"] = "Adjusted comparables: each let-agreed rent adjusted to be like the subject flat"
    s["A1"].font = Font(bold=True, size=13)
    s["A2"] = (f"Best {g.get('settings', {}).get('size', '')} let-agreed flats within {GRID_MAX_KM:g} km, let in the last "
               f"{GRID_MAX_MONTHS} months, ranked by similarity (never by price). Time adjustment uses the ONS "
               "Lewisham 2-bed index. Other percentages are judgement and listed in full.")
    s.append([])
    header(s, ["Comparable", "Distance km", "Let agreed", "Agreed rent", "Time adj.", "Other adjustments",
               "Total adj.", "Adjusted rent", "Similarity", "Evidence / to check", "Listing"])
    for l in g["lines"]:
        s.append([l["title"], l["km"], l["let"], l["rent"], l["time_pct"] / 100,
                  "; ".join(f"{a['label']} {a['pct']:+.0f}%" for a in l["adjustments"]) or "None",
                  l["total_pct"] / 100, l["adjusted"], l["score"],
                  " ".join(x for x in ([f"AI ({l['ai']['confidence']}): {l['ai']['bedroom_evidence']} {l['ai']['condition_evidence']}"] if l["ai"] else []) + [l["check"]] if x),
                  l["url"]])
        s.cell(s.max_row, 11).hyperlink = l["url"]; s.cell(s.max_row, 11).style = "Hyperlink"
    c = g["conclusion"]
    if c:
        s.append([])
        for label, v in [("Similarity-weighted average of adjusted rents", c["weighted_mean"]),
                         ("Median of adjusted rents", c["median"]),
                         ("Range of adjusted rents", f"£{c['low']:,} – £{c['high']:,}"),
                         ("Subject's unrepaired defects (" + (", ".join(f"{d['issue']} {d['pct']:+.1f}%" for d in c["defects"]) or "none open") + ")", None),
                         ("Indicated open-market rent for the subject", c["indicated"]),
                         ("Indicated range", f"£{c['indicated_low']:,} – £{c['indicated_high']:,}"),
                         ("Current rent", RENT_HISTORY[-1][1])]:
            s.append([label, None, None, v])
            s.cell(s.max_row, 1).font = Font(bold=label.startswith("Indicated open"))
            s.cell(s.max_row, 4).font = Font(bold=label.startswith("Indicated open"))
    for col, w in zip("ABCDEFGHIJK", (38, 9, 11, 11, 9, 60, 9, 12, 9, 30, 40)):
        s.column_dimensions[col].width = w
    for row in s.iter_rows(min_row=5):
        for cell in row:
            if cell.column_letter in "DH" and isinstance(cell.value, (int, float)):
                cell.number_format = "£#,##0"
            if cell.column_letter in "EG" and isinstance(cell.value, float):
                cell.number_format = "+0.0%;-0.0%;0.0%"

    mx = analysis.matrix(rows)
    s = wb.create_sheet("Comparables matrix", 2)
    s["A1"] = "Comparables matrix (for MR1 section 12 and the MR3 reply)"
    s["A1"].font = Font(bold=True, size=13)
    s["A2"] = ("Selection rule, fixed before looking at prices: the adjustment-grid flats plus every let-agreed 2-bed flat "
               "within 1 km let in the last 12 months. 'Final advertised' is the rent when let agreed; since 1 May 2026 "
               "a landlord may not accept more than the advertised rent, so it is the most the tenant could have agreed.")
    s.append([])
    header(s, ["Street", "Postcode", "Distance km", "Type", "Bedrooms", "Floor", "Floor area m²", "£ per m²",
               "Furnishing", "Garden", "Parking", "Condition", "First advertised", "Final advertised",
               "In today's money", "Adjusted to your flat", "Let agreed", "Days advertised", "Description (AI, from photos)",
               "Listing", "Saved photos"])
    for m in mx:
        s.append([m["street"], m["postcode"], m["distance_km"], m["type"], m["layout"], m["floor"], m["area_m2"],
                  m["per_m2"], m["furnishing"], m["garden"], m["parking"], m["condition"], m["first_price"],
                  m["final_price"], m["price_today"], m["adjusted"], m["let_agreed"], m["days_advertised"],
                  m["summary"], m["url"], m["photos"]])
        s.cell(s.max_row, 20).hyperlink = m["url"]; s.cell(s.max_row, 20).style = "Hyperlink"
    for col, w in zip("ABCDEFGHIJKLMNOPQRSTU", (20, 8, 8, 9, 14, 9, 9, 8, 13, 9, 11, 13, 10, 10, 10, 10, 11, 9, 50, 40, 26)):
        s.column_dimensions[col].width = w
    for row in s.iter_rows(min_row=5):
        for cell in row:
            if cell.column_letter in "HMNOP" and isinstance(cell.value, (int, float)):
                cell.number_format = "£#,##0"
    s.freeze_panes = "B5"

    sv = analysis.sensitivity(rows)
    s = wb.create_sheet("Sensitivity", 2)
    s["A1"] = "Does the conclusion depend on the filters? Median agreed rent (today's money, adjusted to your location) under each choice"
    s["A1"].font = Font(bold=True, size=13)
    s["A2"] = (f"Your rent (£{sv['rent']:,.2f}) is above the median in {sv['above_in']} of {sv['of']} views, "
               f"by £{sv['gap_min']:,}–£{sv['gap_max']:,} a month. 90% interval from 2,000 bootstrap resamples. "
               f"Without the location adjustment: above in {sv['unadjusted']['above_in']} of {sv['unadjusted']['of']}, "
               f"by £{sv['unadjusted']['gap_min']:,}–£{sv['unadjusted']['gap_max']:,}. Location: ONS median flat price by MSOA; "
               f"rent sensitivity β = {sv['location'].get('beta')} ({sv['location'].get('basis')} from our lets).")
    s.append([])
    header(s, ["Within km", "Let in last (months)", "Lets", "Median", "90% interval low", "90% interval high",
               "You pay above median", "% of lets cheaper than you"])
    for x in sv["cells"]:
        s.append([x["km"], x["months"], x["n"], x.get("median"), x.get("ci_low"), x.get("ci_high"),
                  x.get("gap"), (x["share_cheaper"] / 100) if x.get("n") else None])
    for col, w in zip("ABCDEFGH", (10, 12, 8, 11, 12, 12, 14, 14)):
        s.column_dimensions[col].width = w
    for row in s.iter_rows(min_row=5):
        for cell in row:
            if cell.column_letter in "DEFG" and cell.value is not None:
                cell.number_format = "£#,##0"
            if cell.column_letter == "H" and cell.value is not None:
                cell.number_format = "0%"

    import tribunal_decisions
    td = tribunal_decisions.rows(con)
    if td:
        s = wb.create_sheet("Tribunal decisions", 3)
        s["A1"] = f"First-tier Tribunal rent decisions (Section 13) within {tribunal_decisions.TRIBUNAL_KM:g} km"
        s["A1"].font = Font(bold=True, size=13)
        s["A2"] = ("Source: Rent Rise Check (rentrisecheck.uk), extracted from GOV.UK decisions. Figures are machine-"
                   "extracted: check any decision you rely on against the GOV.UK link. Social-landlord cases decide a "
                   "market rent the landlord may charge, so they are listed separately.")
        s.append([])
        header(s, ["Decided", "Distance km", "Postcode", "Type", "Beds", "Landlord", "Current rent",
                   "Proposed", "Decided rent", "Change vs proposed", "New rules (notice from 1 May 2026)", "Decision",
                   "Checked vs GOV.UK", "Good-condition rent", "Deduction %", "How decided", "Lesson for your case"])
        for r in td:
            chg = (r["determined_rent"] / r["proposed_rent"] - 1) if r["determined_rent"] and r["proposed_rent"] else None
            s.append([r["decision_date"], r["distance_km"], r["postcode"], r["property_type"], r["bedrooms"],
                      "Private" if r["private"] else "Social", r["current_rent"], r["proposed_rent"],
                      r["determined_rent"], chg, "Yes" if r["post_act"] else "No", r["url"],
                      {1: "Matches", 0: "DIFFERS"}.get(r["verified"], "Not readable"), r["good_condition_rent"],
                      r["deduction_pct"], r["reasoning"], r["lesson"]])
            s.cell(s.max_row, 12).hyperlink = r["url"]; s.cell(s.max_row, 12).style = "Hyperlink"
            if r["comparable"]:
                for c in s[s.max_row][:11]:
                    c.font = Font(bold=True)
        for col, w in zip("ABCDEFGHIJKLMNOPQ", (11, 9, 10, 9, 6, 9, 11, 11, 11, 11, 14, 50, 12, 12, 9, 50, 50)):
            s.column_dimensions[col].width = w
        for row in s.iter_rows(min_row=5):
            for cell in row:
                if cell.column_letter in "GHIN" and isinstance(cell.value, (int, float)):
                    cell.number_format = "£#,##0"
                if cell.column_letter == "J" and cell.value is not None:
                    cell.number_format = "+0%;-0%;0%"
        s.append([])
        s.append(["Bold rows: private 2-bed flats (the closest match to yours)."])

    import defects_log
    dsum, ditems = defects_log.summary(con)
    pat = defects_log.pattern(con)
    s = wb.create_sheet("Defects log", 4)
    s["A1"] = "Condition and disrepair evidence (MR1 section 11), built from evidence/defects/"
    s["A1"].font = Font(bold=True, size=13)
    s["A2"] = ("Dates come from each file (photo taken, email sent, or date in the file name). Tribunals have given "
               "£50–£175 a month for documented, unchallenged defects and nothing for undocumented ones.")
    s.append([])
    s.append(["Pattern: " + f"{pat['issues']} issues logged, {pat['open']} still open; resolved ones took a median of "
              f"{pat['median_days_to_resolve'] if pat['median_days_to_resolve'] is not None else '–'} days from first report"
              + (f"; longest open: {pat['longest_open']['issue']} ({pat['longest_open']['days']} days)" if pat['longest_open'] else "")
              + ". Recurring themes: " + "; ".join(f"{k} ({', '.join(v)})" for k, v in pat["themes"].items()) + "."])
    s.append([])
    header(s, ["Issue", "Earliest evidence", "First written report", "Status", "Days since first report",
               "Written reports", "Agent replies", "Repair attempts", "Photos", "Latest entry"])
    for x in dsum:
        s.append([x["issue"], ("on or before " if x["first_approx"] else "") + (x["first"] or ""), x["first_report"],
                  x["status"], x["days"], x["reports"], x["acknowledged"], x["works"], x["photos"], x["latest_description"]])
    s.append([])
    header(s, ["Date", "Issue", "Type", "Date from", "Description", "Severity", "File"])
    for it in ditems:
        s.append([it["date"], it["issue"], it["kind"].replace("_", " "), it["basis"], it["description"], it["severity"], it["path"]])
    if not ditems:
        s.append(["No evidence yet: add photos, emails and notes to evidence/defects/<issue>/ (see evidence/README.txt)."])
    for col, w in zip("ABCDEFGHIJ", (22, 16, 13, 16, 60, 10, 50, 9, 8, 60)):
        s.column_dimensions[col].width = w

    h = wb.create_sheet("Price history")
    h.append(["Property", "Observed", "Status", "Rent £pcm", "Snapshot file"])
    for row in con.execute("SELECT property_id, observed_at, status, price_pcm, snapshot FROM observations ORDER BY property_id, observed_at"):
        h.append(list(row))
    wb.save(path)
    print(f"\nWrote {path}")
    try:
        import dashboard
        dashboard.build()
    except Exception as e:
        print(f"(dashboard not rebuilt: {e})")

def notify(title, message):
    """macOS notification (no-op elsewhere)."""
    import subprocess
    esc = lambda s: s.replace("\\", "\\\\").replace('"', '\\"')
    try:
        subprocess.run(["osascript", "-e", f'display notification "{esc(message)}" with title "{esc(title)}"'],
                       timeout=10, check=False)
    except Exception:
        pass

def daily():
    """Scheduled run: crawl, export, and notify about new close comparables."""
    con = db()
    before = {r[0]: r[1] for r in con.execute("SELECT id, status FROM properties")}
    crawl()
    try:
        import area_prices
        con2 = db(); con2.executescript(area_prices.SCHEMA)
        area_prices.load_prices(con2); area_prices.place_properties(con2)
    except Exception as e:
        print(f"(location step skipped: {e})")
    try:
        import ai_review
        ai_review.run()
    except Exception as e:
        print(f"(AI checks skipped: {e})")
    try:
        import defects_log
        defects_log.scan()
    except Exception as e:
        print(f"(defects log skipped: {e})")
    export()
    rows = load_rows(db())
    new = [r for r in rows if not r["exclude_reason"] and (r["distance_km"] or 99) <= GRID_MAX_KM
           and (r["id"] not in before or (r["status"] == "let_agreed" and before.get(r["id"]) != "let_agreed"))]
    print(f"\n{len(new)} new or newly let-agreed comparables within {GRID_MAX_KM:g} km")
    for r in new:
        print(f"  {r['status']:<10} £{r['price_pcm']:,.0f}  {r['distance_km']}km  {r['title']}  {r['url']}")
    lets = [r for r in new if r["status"] == "let_agreed"]
    if new:
        best = max(new, key=lambda r: r["score"])
        notify("Rent comparables",
               f"{len(new)} new within {GRID_MAX_KM:g} km ({len(lets)} let agreed). "
               f"Closest match: £{best['price_pcm']:,.0f}, {best['title']}")

if __name__ == "__main__":
    import urllib.parse
    cmd = sys.argv[1] if len(sys.argv) > 1 else "crawl"
    {"crawl": crawl, "stats": stats, "export": export, "daily": daily}.get(cmd, lambda: sys.exit(__doc__))()
