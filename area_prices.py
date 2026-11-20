#!/usr/bin/env python3
"""
Neighbourhood price levels, so comparables in dearer or cheaper areas can be adjusted to the
subject flat's location (flats in much dearer or cheaper neighbourhoods aren't like-for-like).

  python3 area_prices.py          # load ONS small-area prices, place every comparable, estimate the effect

Source: ONS House Price Statistics for Small Areas (HPSSA dataset 2, table 1e): median price paid for
flats/maisonettes by MSOA (Middle layer Super Output Area, ~7,000 residents). To smooth noise we use the
average of the latest four rolling-year figures. Each comparable is placed in an MSOA by reverse-geocoding
its map position with postcodes.io (free, no key).

How strongly rents follow prices is estimated from our own let-agreed comparables (log-log regression);
see estimate_elasticity(). The adjustment for a comparable is (subject area price / its area price) ** β.
"""
import json
import math
import os
import urllib.request

import openrent_comps as oc

HERE = os.path.dirname(os.path.abspath(__file__))
AREA_DIR = os.path.join(HERE, "area_data")
HPSSA_PAGE = "https://www.ons.gov.uk/peoplepopulationandcommunity/housing/datasets/medianhousepricesbymiddlelayersuperoutputarea"
LONDON_LAS = None  # all London boroughs (codes E09...)
SUBJECT_POSTCODE = oc.SUBJECT_POSTCODE   # set in config_local.py

SCHEMA = """
CREATE TABLE IF NOT EXISTS area_prices (msoa TEXT PRIMARY KEY, name TEXT, la TEXT, price REAL, quarters TEXT, source TEXT);
CREATE TABLE IF NOT EXISTS area_cache (latlng TEXT PRIMARY KEY, msoa TEXT, msoa_name TEXT);
"""


def _latest_file():
    import re
    req = urllib.request.Request(HPSSA_PAGE, headers={"User-Agent": "Mozilla/5.0"})
    html = urllib.request.urlopen(req, timeout=30, context=oc.SSL_CTX).read().decode()
    href = re.search(r'href="(/file\?uri=[^"]+\.xlsx)"', html).group(1)
    edition = href.split("/")[-2]
    path = os.path.join(AREA_DIR, f"hpssa2_{edition}.xlsx")
    if not os.path.exists(path):
        os.makedirs(AREA_DIR, exist_ok=True)
        print(f"Downloading ONS small-area prices ({edition}, ~70MB)…")
        req = urllib.request.Request("https://www.ons.gov.uk" + href, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=300, context=oc.SSL_CTX) as r, open(path + ".tmp", "wb") as f:
            f.write(r.read())
        os.replace(path + ".tmp", path)
    return path, edition


def load_prices(con):
    from openpyxl import load_workbook
    import glob
    files = sorted(glob.glob(os.path.join(AREA_DIR, "hpssa2_*.xlsx")))
    try:
        path, edition = _latest_file()
    except Exception as e:
        if not files:
            raise
        path, edition = files[-1], os.path.basename(files[-1])[7:-5]
        print(f"(couldn't check ONS for a newer edition: {e})")
    have = con.execute("SELECT COUNT(*), MAX(source) FROM area_prices").fetchone()
    if have[0] and have[1] == edition:
        return edition
    ws = load_workbook(path, read_only=True)["1e"]
    rows = ws.iter_rows(min_row=3, values_only=True)
    hdr = next(rows)
    con.execute("DELETE FROM area_prices")
    n = 0
    for r in rows:
        if not r or not str(r[0] or "").startswith("E09"):          # London boroughs only
            continue
        vals = [v for v in r[-4:] if isinstance(v, (int, float))]
        if not vals:
            continue
        con.execute("INSERT OR REPLACE INTO area_prices VALUES (?,?,?,?,?,?)",
                    (r[2], r[3], r[1], sum(vals) / len(vals), f"{hdr[-4]} to {hdr[-1]} ({len(vals)} of 4)", edition))
        n += 1
    con.commit()
    print(f"Loaded flat prices for {n} London neighbourhoods (ONS HPSSA, {edition}).")
    return edition


def _post_json(url, payload):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json", "User-Agent": "rent-comparables/1.0"})
    return json.loads(urllib.request.urlopen(req, timeout=60, context=oc.SSL_CTX).read())


def place_properties(con):
    """Reverse-geocode each comparable's position to an MSOA (cached; postcodes.io bulk endpoint)."""
    cached = {r[0] for r in con.execute("SELECT latlng FROM area_cache")}
    todo = {}
    for lat, lng in con.execute("SELECT lat, lng FROM properties WHERE lat IS NOT NULL"):
        k = f"{lat:.5f},{lng:.5f}"
        if k not in cached:
            todo[k] = (lat, lng)
    keys = list(todo)
    for i in range(0, len(keys), 100):
        batch = keys[i:i + 100]
        res = _post_json("https://api.postcodes.io/postcodes?filter=msoa,codes",
                         {"geolocations": [{"latitude": todo[k][0], "longitude": todo[k][1], "radius": 500, "limit": 1} for k in batch]})
        for k, item in zip(batch, res.get("result") or []):
            hit = (item.get("result") or [None])[0]
            if hit:
                con.execute("INSERT OR REPLACE INTO area_cache VALUES (?,?,?)",
                            (k, (hit.get("codes") or {}).get("msoa"), hit.get("msoa")))
        con.commit()
    return len(keys)


def subject_area(con):
    row = con.execute("SELECT msoa, msoa_name FROM area_cache WHERE latlng='subject'").fetchone()
    if not row:
        req = urllib.request.Request(f"https://api.postcodes.io/postcodes/{SUBJECT_POSTCODE.replace(' ', '')}",
                                     headers={"User-Agent": "rent-comparables/1.0"})
        r = json.loads(urllib.request.urlopen(req, timeout=30, context=oc.SSL_CTX).read())["result"]
        row = (r["codes"]["msoa"], r["msoa"])
        con.execute("INSERT OR REPLACE INTO area_cache VALUES ('subject',?,?)", row)
        con.commit()
    price = con.execute("SELECT price, name FROM area_prices WHERE msoa=?", (row[0],)).fetchone()
    return {"msoa": row[0], "name": price[1] if price else row[1], "price": price[0] if price else None}


def lookup(con):
    """{ 'lat,lng': (msoa, name, price) } for every placed comparable."""
    try:
        return {k: (m, n, p) for k, m, n, p in con.execute(
            "SELECT c.latlng, c.msoa, a.name, a.price FROM area_cache c LEFT JOIN area_prices a ON a.msoa = c.msoa")}
    except Exception:
        return {}


def estimate_elasticity(rows):
    """Slope of log(rent in today's money) on log(area price ratio) across let-agreed flats, with a
    standard error. Uses rows before any location exclusion. Returns dict or None."""
    pts = [(math.log(r["area_ratio"]), math.log(r["price_today"])) for r in rows
           if r.get("area_ratio") and r.get("price_today") and r["status"] == "let_agreed"
           and not (r["exclude_reason"] and not r["exclude_reason"].startswith("different price area"))
           and not __import__("re").search(r"house|bungalow|cottage", (r["property_type"] or "").lower())]
    if len(pts) < 20:
        return None
    n = len(pts)
    mx = sum(x for x, _ in pts) / n
    my = sum(y for _, y in pts) / n
    sxx = sum((x - mx) ** 2 for x, _ in pts)
    if sxx == 0:
        return None
    b = sum((x - mx) * (y - my) for x, y in pts) / sxx
    resid = sum((y - my - b * (x - mx)) ** 2 for x, y in pts) / (n - 2)
    se = math.sqrt(resid / sxx)
    return {"beta": b, "se": se, "n": n, "low": b - 1.96 * se, "high": b + 1.96 * se}


def main():
    con = oc.db()
    con.executescript(SCHEMA)
    ed = load_prices(con)
    n = place_properties(con)
    s = subject_area(con)
    print(f"Placed {n} new comparable positions. Subject: {s['name']} ({s['msoa']}), median flat price £{s['price']:,.0f}.")
    rows = oc.load_rows(oc.db())
    e = estimate_elasticity(rows)
    if e:
        print(f"Estimated rent sensitivity to area price: β = {e['beta']:.2f} (95% range {e['low']:.2f} to {e['high']:.2f}, n={e['n']})")
    from collections import Counter
    c = Counter((r.get("area_name"), round(r["area_ratio"], 2)) for r in rows if r.get("area_ratio"))
    for (name, ratio), k in sorted(c.items(), key=lambda x: -x[0][1]):
        print(f"  {name:<34} ×{ratio:.2f}  ({k} listings)")


if __name__ == "__main__":
    main()


# ---------------------------------------------------------------------------
# Stations (rail, Overground, DLR) from OpenStreetMap, saved in area_data/stations_osm.json.
STATIONS_FILE = os.path.join(AREA_DIR, "stations_osm.json")
_STATIONS = None


def stations():
    """[(name, lat, lng)], one per station name (OSM can have several nodes per station)."""
    global _STATIONS
    if _STATIONS is None:
        if not os.path.exists(STATIONS_FILE):
            q = ('[out:json];(node["railway"="station"](51.40,-0.09,51.50,0.07);'
                 'node["public_transport"="station"]["train"="yes"](51.40,-0.09,51.50,0.07););out;')
            import urllib.parse
            req = urllib.request.Request("https://overpass-api.de/api/interpreter",
                                         data=urllib.parse.urlencode({"data": q}).encode(),
                                         headers={"User-Agent": "rent-comparables/1.0"})
            with urllib.request.urlopen(req, timeout=90, context=oc.SSL_CTX) as r, open(STATIONS_FILE, "wb") as f:
                f.write(r.read())
        seen = {}
        for e in json.load(open(STATIONS_FILE)).get("elements", []):
            n = (e.get("tags") or {}).get("name")
            if n and n not in seen and "lat" in e:
                seen[n] = (n, e["lat"], e["lon"])
        _STATIONS = list(seen.values())
    return _STATIONS


def nearest_station(lat, lng):
    if lat is None or lng is None:
        return None, None
    best = min(stations(), key=lambda s: oc.km(lat, lng, s[1], s[2]))
    return best[0], round(oc.km(lat, lng, best[1], best[2]), 2)
