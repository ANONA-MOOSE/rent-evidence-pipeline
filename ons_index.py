#!/usr/bin/env python3
"""
Load the ONS Price Index of Private Rents (PIPR) into the database.

  python3 ons_index.py                 # downloads the latest release, loads London boroughs

PIPR is the official government rent series (new AND existing tenancies), monthly from 2015,
by local authority and bedroom count. Source:
https://www.ons.gov.uk/economy/inflationandpriceindices/datasets/priceindexofprivaterentsukmonthlypricestatistics
"""
import os
import re
import sys
import urllib.request

import openrent_comps as oc

HERE = os.path.dirname(os.path.abspath(__file__))
ONS_DIR = os.path.join(HERE, "ons_data")
PAGE = ("https://www.ons.gov.uk/economy/inflationandpriceindices/datasets/"
        "priceindexofprivaterentsukmonthlypricestatistics")

SCHEMA = """
CREATE TABLE IF NOT EXISTS ons_rents (
  period TEXT, area_code TEXT, area TEXT, region TEXT,
  price_all REAL, price_2bed REAL, annual_change_2bed REAL, index_2bed REAL,
  price_flat REAL, annual_change_flat REAL, release TEXT,
  PRIMARY KEY (period, area_code)
);
"""

def latest_file():
    req = urllib.request.Request(PAGE, headers={"User-Agent": "Mozilla/5.0"})
    html = urllib.request.urlopen(req, timeout=30, context=oc.SSL_CTX).read().decode()
    m = re.search(r'href="(/file\?uri=[^"]+\.xlsx)"', html)
    if not m:
        sys.exit("Couldn't find the PIPR download link on the ONS page.")
    href = m.group(1)
    release = re.search(r"/(\d{1,2}[a-z]+\d{4})/", href).group(1)
    path = os.path.join(ONS_DIR, f"pipr_{release}.xlsx")
    if not os.path.exists(path):
        os.makedirs(ONS_DIR, exist_ok=True)
        print(f"Downloading ONS release {release} (~19MB)…")
        req = urllib.request.Request("https://www.ons.gov.uk" + href, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=120, context=oc.SSL_CTX) as r, open(path, "wb") as f:
            f.write(r.read())
    return path, release

def num(v):
    return float(v) if isinstance(v, (int, float)) else None

def main():
    from openpyxl import load_workbook
    path, release = latest_file()
    con = oc.db()
    con.executescript(SCHEMA)
    ws = load_workbook(path, read_only=True)["Table 1"]
    rows = ws.iter_rows(values_only=True)
    hdr = None
    for r in rows:
        if r and r[0] == "Time period":
            hdr = list(r)
            break
    col = {h: i for i, h in enumerate(hdr)}
    n = 0
    for r in rows:
        if not r or r[0] is None:
            continue
        region = r[col["Region or country name"]]
        area = r[col["Area name"]]
        if region != "London" and area not in ("London", "England", "United Kingdom"):
            continue
        con.execute("INSERT OR REPLACE INTO ons_rents VALUES (?,?,?,?,?,?,?,?,?,?,?)", (
            r[0].strftime("%Y-%m"), r[col["Area code"]], area, region,
            num(r[col["Rental price"]]), num(r[col["Rental price two bed"]]),
            num(r[col["Annual change two bed"]]), num(r[col["Index two bed"]]),
            num(r[col["Rental price flat maisonette"]]), num(r[col["Annual change flat maisonette"]]), release))
        n += 1
    con.commit()
    last = con.execute("SELECT period, price_2bed, annual_change_2bed FROM ons_rents WHERE area='Lewisham' ORDER BY period DESC LIMIT 1").fetchone()
    print(f"Loaded {n} monthly rows (London boroughs + London/England/UK) from release {release}.")
    print(f"Lewisham 2-bed average, {last[0]}: £{last[1]:,.0f} pcm, {last[2]:+.1f}% on a year earlier.")

if __name__ == "__main__":
    main()
