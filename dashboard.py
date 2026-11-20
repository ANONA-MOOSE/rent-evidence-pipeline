#!/usr/bin/env python3
"""
Build a self-contained dashboard (openrent_data/dashboard.html) from the database.

  python3 dashboard.py        # also run automatically by: openrent_comps.py export

Open the file in any browser. It works offline, follows light/dark mode, and prints
cleanly (File > Print > Save as PDF) for an evidence bundle.
"""
import json
import os
import re
from datetime import date, datetime, timedelta

import analysis
import tribunal_decisions
import defects_log
import openrent_comps as oc

OUT = os.path.join(oc.DATA_DIR, "dashboard.html")


def timeline():
    """Case events and deadlines from case/timeline.csv."""
    import csv
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "case", "timeline.csv")
    if not os.path.exists(p):
        return []
    with open(p, newline="", encoding="utf-8") as f:
        return [r for r in csv.DictReader(f) if (r.get("date") or "").strip()]


def collection_health(con, rows):
    today = date.today()
    window = [today - timedelta(days=i) for i in range(6, -1, -1)]
    log = os.path.join(os.path.dirname(OUT), "..", "logs", "daily.log")
    runs = {}
    run = None
    if os.path.exists(log):
        with open(log, encoding="utf-8", errors="replace") as stream:
            for line in stream:
                header = re.fullmatch(r"=== (\d{4}-\d{2}-\d{2}) \d{2}:\d{2} ===", line.strip())
                if header:
                    run = {"completed": False, "requests": 0, "warnings": 0, "failures": 0}
                    runs.setdefault(date.fromisoformat(header[1]), []).append(run)
                elif run is not None and not run["completed"]:
                    done = re.match(r"Done: (\d+) requests\.", line)
                    if done:
                        run.update(completed=True, requests=int(done[1]))
                    elif line.startswith("  ! OpenRent request failed after retries"):
                        run["failures"] += 1
                    elif line.startswith("  ! "):
                        run["warnings"] += 1

    latest = max((day for day, entries in runs.items()
                  if any(entry["completed"] and entry["requests"] for entry in entries)), default=None)
    transitions = dict(con.execute("""
        WITH history AS (
            SELECT property_id, observed_at, status,
                   LAG(status) OVER (PARTITION BY property_id ORDER BY observed_at, rowid) AS previous
            FROM observations
        )
        SELECT substr(observed_at, 1, 10), COUNT(DISTINCT property_id)
        FROM history
        WHERE observed_at >= ? AND status = 'let_agreed' AND previous = 'available'
        GROUP BY substr(observed_at, 1, 10)
    """, (window[0].isoformat(),)))
    recent = [
        row for row in rows
        if not row["exclude_reason"] and row["price_pcm"] and row["status"] == "let_agreed"
        and analysis._is_flat(row) and row["distance_km"] is not None and row["distance_km"] <= 2
        and (today - timedelta(days=365)).isoformat() <= (row["let_agreed_date"] or "") <= today.isoformat()
    ]
    days = []
    for day in window:
        entries = runs.get(day, [])
        completed = [entry for entry in entries if entry["completed"]]
        if any(entry["requests"] for entry in completed):
            status = "Completed"
        elif completed:
            status = "No successful requests"
        elif entries:
            status = "Completion not recorded"
        elif day == today:
            status = "No run logged yet"
        else:
            status = "No run logged"
        days.append({"date": day.isoformat(), "status": status,
                     "warnings": sum(entry["warnings"] for entry in entries),
                     "failures": sum(entry["failures"] for entry in entries),
                     "transitions": transitions.get(day.isoformat(), 0)})
    return {
        "latest": latest.isoformat() if latest else None,
        "days": days,
        "total": len(rows),
        "nearby": len(recent),
        "area": sum(bool(row.get("area_m2")) for row in recent),
        "layout": sum(bool(row.get("layout") or any(
            oc.desc_flags(row["description"]).get(flag) for flag in ("two_doubles", "single_bedroom")
        )) for row in recent),
    }


def gather():
    con = oc.db()
    rows = oc.load_rows(con)
    comps = [{
        "id": r["id"], "title": r["title"], "price": r["price_pcm"], "today": r["price_today"], "loc": r.get("price_loc"), "area": r.get("area_name"),
        "ratio": round(r["area_ratio"], 2) if r.get("area_ratio") else None, "btype": r.get("building_type"), "station": r.get("station"), "stkm": r.get("station_km"), "status": r["status"],
        "let": r["let_agreed_date"], "listed": r["first_listed"] or (r["first_seen"] or "")[:10],
        "km": r["distance_km"], "score": r["score"], "why": r["score_why"], "url": r["url"],
        "type": r["property_type"], "baths": r["baths"], "tenants": r["max_tenants"],
        "garden": r["garden"], "parking": r["parking"], "m2": r.get("area_m2"), "perm2": r.get("per_m2"),
        "excluded": r["exclude_reason"], "days": r["days_advertised"], "cut": r["price_cut"],
    } for r in rows if r["price_pcm"]]

    ons, boroughs, ons_release = [], [], None
    try:
        ons = [{"p": p, "lew": a, "chg": c, "ldn": l} for p, a, c, l in con.execute("""
            SELECT a.period, a.price_2bed, a.annual_change_2bed, b.price_2bed
            FROM ons_rents a LEFT JOIN ons_rents b ON b.period = a.period AND b.area = 'London'
            WHERE a.area = 'Lewisham' AND a.period >= '2019-01' ORDER BY a.period""")]
        latest = con.execute("SELECT MAX(period), MAX(release) FROM ons_rents").fetchone()
        ons_release = latest[1]
        boroughs = [{"area": a, "price": p, "chg": c} for a, p, c in con.execute("""
            SELECT area, price_2bed, annual_change_2bed FROM ons_rents
            WHERE period = ? AND region = 'London' AND price_2bed IS NOT NULL ORDER BY price_2bed""", (latest[0],))]
    except Exception:
        pass  # ONS data not loaded yet: those panels hide themselves

    return {
        "generated": datetime.now().strftime("%d %B %Y"),
        "title": oc.DASHBOARD_TITLE,
        "radius": oc.RADIUS_KM,
        "rentHistory": [{"from": f, "rent": r} for f, r in oc.RENT_HISTORY],
        "proposed": list(oc.PROPOSED_RENT_RANGE) if oc.PROPOSED_RENT_RANGE else None,
        "subjectM2": oc.SUBJECT_FLOOR_M2,
        "comps": comps, "ons": ons, "boroughs": boroughs, "onsRelease": ons_release,
        "timeline": timeline(), "collection": collection_health(con, rows),
        "grid": analysis.grid(rows), "sens": analysis.sensitivity(rows),
        "tribunal": [{k: r[k] for k in ("url", "postcode", "distance_km", "decision_date", "property_type", "bedrooms",
                      "current_rent", "proposed_rent", "determined_rent", "post_act", "private", "comparable",
                      "verified", "good_condition_rent", "deduction_pct", "lesson", "reasoning")}
                     for r in tribunal_decisions.rows(con)],
        "tribunalKm": tribunal_decisions.TRIBUNAL_KM,
        "market": analysis.time_on_market(rows),
        "defects": defects_log.summary(con)[0], "pattern": defects_log.pattern(con),
        "matrixN": len(analysis.matrix(rows)),
    }


def build():
    data = gather()
    html = TEMPLATE.replace("/*__DATA__*/null", json.dumps(data, default=str).replace("</", "<\\/"))
    with open(OUT, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"Wrote {OUT}")
    return OUT


TEMPLATE = r"""<!doctype html>
<html lang="en-GB">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Rent comparables</title>
<style>
:root {
  color-scheme: light;
  --page: #f9f9f7; --surface: #fcfcfb; --ink: #0b0b0b; --ink-2: #52514e; --muted: #898781;
  --grid: #e1e0d9; --axis: #c3c2b7; --ring: rgba(11,11,11,0.10);
  --s1: #2a78d6; --s2: #eb6834; --s3: #1baf7a; --s2-wash: rgba(235,104,52,0.10);
  --neutral: #c3c2b7; --good: #006300; --bad: #d03b3b;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    color-scheme: dark;
    --page: #0d0d0d; --surface: #1a1a19; --ink: #f0efec; --ink-2: #c3c2b7; --muted: #898781;
    --grid: #2c2c2a; --axis: #383835; --ring: rgba(255,255,255,0.10);
    --s1: #3987e5; --s2: #d95926; --s3: #199e70; --s2-wash: rgba(217,89,38,0.16);
    --neutral: #4a4a46; --good: #0ca30c; --bad: #e66767;
  }
}
:root[data-theme="dark"] {
  color-scheme: dark;
  --page: #0d0d0d; --surface: #1a1a19; --ink: #f0efec; --ink-2: #c3c2b7; --muted: #898781;
  --grid: #2c2c2a; --axis: #383835; --ring: rgba(255,255,255,0.10);
  --s1: #3987e5; --s2: #d95926; --s3: #199e70; --s2-wash: rgba(217,89,38,0.16);
  --neutral: #4a4a46; --good: #0ca30c; --bad: #e66767;
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--page); color: var(--ink);
  font: 15px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif; }
main { max-width: 1120px; margin: 0 auto; padding: 32px 16px 64px; }
h1 { font-size: 26px; font-weight: 650; margin: 0 0 4px; letter-spacing: -0.01em; }
h2 { font-size: 17px; font-weight: 620; margin: 0 0 2px; }
.sub { color: var(--ink-2); margin: 0; }
.small { font-size: 13px; color: var(--muted); }
.filters { display: flex; flex-wrap: wrap; gap: 8px 20px; align-items: center; margin: 24px 0 16px; }
.seg { display: inline-flex; border: 1px solid var(--ring); border-radius: 999px; padding: 2px; background: var(--surface); }
.seg button { border: 0; background: transparent; color: var(--ink-2); font: inherit; font-size: 13px;
  padding: 5px 12px; border-radius: 999px; cursor: pointer; }
.seg button[aria-pressed="true"] { background: var(--ink); color: var(--surface); }
.seg button:focus-visible { outline: 2px solid var(--s1); outline-offset: 1px; }
.flabel { font-size: 13px; color: var(--muted); margin-right: 6px; }
.workflow { list-style: none; display: grid; grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 24px; padding: 0; margin: 12px 0 0; }
.workflow li { position: relative; border: 1px solid var(--grid); border-top: 3px solid var(--s1);
  border-radius: 10px; padding: 12px; }
.workflow li + li::before { content: ""; position: absolute; top: 50%; left: -17px;
  width: 8px; height: 8px; border: solid var(--muted); border-width: 2px 2px 0 0;
  transform: rotate(45deg); }
.workflow h3 { margin: 0 0 5px; font-size: 15px; }
.workflow p { margin: 0; }
@media (max-width: 720px) {
  .workflow { grid-template-columns: 1fr; gap: 18px; }
  .workflow li + li::before { top: -16px; left: calc(50% - 4px); transform: rotate(135deg); }
}
@media print { .workflow li { break-inside: avoid; } }
.grid { display: grid; gap: 16px; grid-template-columns: repeat(12, 1fr); }
.card { background: var(--surface); border: 1px solid var(--ring); border-radius: 14px; padding: 20px; min-width: 0; }
.span-12 { grid-column: span 12; } .span-8 { grid-column: span 8; } .span-6 { grid-column: span 6; }
.span-4 { grid-column: span 4; } .span-3 { grid-column: span 3; }
@media (max-width: 860px) { .span-8, .span-6, .span-4 { grid-column: span 12; } .span-3 { grid-column: span 6; } }
.hero { display: flex; flex-wrap: wrap; gap: 24px 48px; align-items: flex-end; }
.hero .big { font-size: 56px; font-weight: 650; line-height: 1; letter-spacing: -0.02em; }
.hero .cap { color: var(--ink-2); max-width: 520px; }
.tile .label { font-size: 13px; color: var(--ink-2); }
.tile .value { font-size: 28px; font-weight: 620; margin-top: 2px; }
.tile .delta { font-size: 13px; margin-top: 2px; color: var(--ink-2); }
.up-bad { color: var(--bad); } .down-good { color: var(--good); }
.legend { display: flex; flex-wrap: wrap; gap: 4px 16px; font-size: 13px; color: var(--ink-2); margin: 8px 0 4px; }
.key { display: inline-flex; align-items: center; gap: 6px; }
.key i { display: inline-block; width: 14px; height: 2px; border-radius: 2px; }
.key i.dot { width: 8px; height: 8px; border-radius: 50%; }
.key i.band { width: 14px; height: 10px; border-radius: 3px; }
svg { display: block; width: 100%; height: auto; overflow: visible; }
svg text { fill: var(--muted); font-size: 12px; font-variant-numeric: tabular-nums; }
svg .lbl { fill: var(--ink-2); font-size: 12px; font-variant-numeric: normal; }
svg .lbl-strong { fill: var(--ink); font-size: 12px; font-weight: 600; font-variant-numeric: normal; }
.gridline { stroke: var(--grid); stroke-width: 1; } .axis { stroke: var(--axis); stroke-width: 1; }
.hit { fill: transparent; cursor: pointer; }
.hit:focus { outline: none; } .hit:focus-visible + .mark, .mark.on { stroke: var(--ink); stroke-width: 2; }
#tip { position: fixed; pointer-events: none; z-index: 10; background: var(--surface); color: var(--ink);
  border: 1px solid var(--ring); border-radius: 10px; padding: 8px 10px; font-size: 13px; box-shadow: 0 6px 24px rgba(0,0,0,.12);
  max-width: 280px; opacity: 0; transition: opacity .08s; }
#tip strong { font-size: 15px; display: block; }
#tip .row { display: flex; align-items: center; gap: 6px; color: var(--ink-2); }
#tip .row i { width: 12px; height: 2px; display: inline-block; border-radius: 2px; }
table { width: 100%; border-collapse: collapse; font-size: 13px; }
th { text-align: left; font-weight: 600; color: var(--ink-2); border-bottom: 1px solid var(--axis); padding: 6px 8px; white-space: nowrap; }
td { border-bottom: 1px solid var(--grid); padding: 6px 8px; vertical-align: top; }
td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; }
.tablewrap { overflow-x: auto; }
a { color: var(--s1); }
.pill { display: inline-block; font-size: 12px; padding: 1px 8px; border-radius: 999px; border: 1px solid var(--ring); color: var(--ink-2); white-space: nowrap; }
details summary { cursor: pointer; color: var(--ink-2); font-size: 13px; margin-top: 10px; }
.note { color: var(--ink-2); font-size: 13px; }
.matrix td, .matrix th { text-align: center; }
.matrix td { padding: 10px 8px; }
.matrix .m { font-size: 18px; font-weight: 620; color: var(--ink); font-variant-numeric: tabular-nums; }
.matrix .g { color: var(--bad); font-weight: 600; }
.adjneg { color: var(--good); } .adjpos { color: var(--bad); }
.total td { font-weight: 650; border-top: 1px solid var(--axis); }
.note li { margin-bottom: 4px; }
@media print {
  body { background: #fff; } .filters { display: none; } .card { break-inside: avoid; border-color: #ddd; }
  #tip { display: none; } details { display: block; } details > * { display: block; }
}
</style>
</head>
<body>
<main>
  <header>
    <h1 id="title">Two-bed rent comparables</h1>
    <p class="sub">Comparable private lets from OpenRent and the ONS rent index · <span id="gen"></span></p>
  </header>

  <section class="card span-12" id="publicBanner" hidden style="margin-top:16px">
    <p style="margin:0"><strong>Public demo.</strong> <span id="publicText"></span></p>
  </section>

  <div class="filters" role="toolbar" aria-label="Filters">
    <span><span class="flabel">Period</span><span class="seg" data-f="period">
      <button data-v="12">Last 12 months</button><button data-v="24">24 months</button><button data-v="999">All</button></span></span>
    <span><span class="flabel">Within</span><span class="seg" data-f="km">
      <button data-v="1">1 km</button><button data-v="2">2 km</button><button data-v="99">All</button></span></span>
    <span><span class="flabel">Status</span><span class="seg" data-f="status">
      <button data-v="let">Let agreed</button><button data-v="all">Let + advertised</button></span></span>
    <span><span class="flabel">Rents</span><span class="seg" data-f="adj">
      <button data-v="loc">Adjusted to your area</button><button data-v="today">In today's money</button><button data-v="raw">As let</button></span></span>
  </div>

  <div class="grid">
    <section class="card span-12" aria-label="Headline">
      <div class="hero">
        <div><div class="small" id="heroLabel">Your rent vs median agreed rent</div><div class="big" id="heroValue">–</div></div>
        <p class="cap" id="heroCap"></p>
      </div>
    </section>

    <section class="card span-3 tile"><div class="label">Your current rent</div><div class="value" id="tRent"></div><div class="delta" id="tRentD"></div></section>
    <section class="card span-3 tile"><div class="label" id="tMedL">Median agreed rent</div><div class="value" id="tMed"></div><div class="delta" id="tMedD"></div></section>
    <section class="card span-3 tile"><div class="label">Lewisham 2-bed average (ONS)</div><div class="value" id="tOns"></div><div class="delta" id="tOnsD"></div></section>
    <section class="card span-3 tile"><div class="label">Comparables in view</div><div class="value" id="tN"></div><div class="delta" id="tND"></div></section>

    <section class="card span-12" aria-labelledby="h-workflow">
      <h2 id="h-workflow">How the dashboard works</h2>
      <ol class="workflow" role="list">
        <li>
          <h3>Collect nearby listings</h3>
          <p class="small">The daily crawl searches OpenRent for nearby two-bedroom homes and checks landlords’ past listings.</p>
        </li>
        <li>
          <h3>Track prices and status</h3>
          <p class="small">The database records asking and agreed rents, tracks changes and keeps listing snapshots as evidence.</p>
        </li>
        <li>
          <h3>Find comparable flats</h3>
          <p class="small">Filters narrow the results by date, distance and status. The shortlist ranks flats by similarity without using rent.</p>
        </li>
        <li>
          <h3>Estimate a like-for-like rent</h3>
          <p class="small">The <a href="#h-grid">adjustment table</a> shows how rent trends, location, features and condition affect the estimate.</p>
        </li>
      </ol>
    </section>

    <section class="card span-12" aria-labelledby="h-collection">
      <h2 id="h-collection">Collection health</h2>
      <p id="collectionSummary"></p>
      <p class="small" id="collectionCoverage"></p>
      <details open><summary>Last 7 days</summary>
        <div class="tablewrap"><table id="collectionTable">
          <thead><tr><th>Date</th><th>Scheduled crawl</th><th>Request warnings</th><th>Exhausted retries</th><th>Newly let agreed</th></tr></thead>
          <tbody></tbody>
        </table></div>
        <p class="small">Warnings include retry attempts. Exhausted retries are tracked from this dashboard update onward. Newly let agreed counts listings previously seen as available. Older lets discovered later add to the database separately.</p>
      </details>
    </section>

    <section class="card span-12" aria-labelledby="h-dates" id="datesCard">
      <h2 id="h-dates">Key dates</h2>
      <p class="small">From case/timeline.csv. Deadlines count down; past events are under "Earlier".</p>
      <div class="tablewrap"><table id="datesTbl"></table></div>
      <details><summary>Earlier</summary><div class="tablewrap"><table id="datesPast"></table></div></details>
    </section>

    <section class="card span-12" aria-labelledby="h-grid" id="gridCard">
      <h2 id="h-grid">Adjusted comparables: what your flat would let for</h2>
      <p class="small" id="gridSub"></p>
      <div class="hero" style="margin:14px 0 18px">
        <div><div class="small">Indicated open-market rent</div><div class="big" id="gridValue">–</div></div>
        <p class="cap" id="gridCap"></p>
      </div>
      <div class="tablewrap"><table id="gridTbl"></table></div>
    </section>

    <section class="card span-12" aria-labelledby="h-sens" id="sensCard">
      <h2 id="h-sens">Does the answer depend on the filters?</h2>
      <p class="small" id="sensSub"></p>
      <div class="tablewrap"><table id="sensTbl" class="matrix"></table></div>
    </section>

    <section class="card span-12" aria-labelledby="h-trib" id="tribCard">
      <h2 id="h-trib">What the tribunal has decided nearby</h2>
      <p class="small" id="tribSub"></p>
      <div class="legend" id="lg-trib"></div>
      <div id="c-trib"></div>
      <details><summary>All nearby decisions, including social-landlord cases</summary><div class="tablewrap"><table id="tribTbl"></table></div></details>
    </section>

    <section class="card span-6" aria-labelledby="h-mkt" id="mktCard">
      <h2 id="h-mkt">Do dearer flats take longer to let?</h2>
      <p class="small" id="mktSub"></p>
      <div class="tablewrap"><table id="mktTbl" class="matrix"></table></div>
      <p class="small" id="mktCuts"></p>
    </section>

    <section class="card span-6" aria-labelledby="h-def" id="defCard">
      <h2 id="h-def">Condition evidence</h2>
      <p class="small" id="defSub">Built automatically from the evidence folder. Dated photos and reports are what turn a defect into a deduction.</p>
      <div class="tablewrap"><table id="defTbl"></table></div>
    </section>

    <section class="card span-12" aria-labelledby="h-dist">
      <h2 id="h-dist">Where your rent sits among comparable lets</h2>
      <p class="small">Each dot is one 2-bed let. Hover for details.</p>
      <div class="legend" id="lg-dist"></div>
      <div id="c-dist"></div>
    </section>

    <section class="card span-8" aria-labelledby="h-time">
      <h2 id="h-time">Agreed rents over time</h2>
      <p class="small">Dots: agreed rents by date let. Lines: the official Lewisham 2-bed average and your rent.</p>
      <div class="legend" id="lg-time"></div>
      <div id="c-time"></div>
    </section>

    <section class="card span-4" aria-labelledby="h-growth">
      <h2 id="h-growth">How fast your rent rose</h2>
      <p class="small">Change since your tenancy began, compared with the Lewisham 2-bed index.</p>
      <div class="legend" id="lg-growth"></div>
      <div id="c-growth"></div>
    </section>

    <section class="card span-12" aria-labelledby="h-boro" id="boroCard">
      <h2 id="h-boro">Average 2-bed rent by London borough</h2>
      <p class="small" id="boroSub"></p>
      <div id="c-boro"></div>
    </section>

    <section class="card span-12" aria-labelledby="h-table">
      <h2 id="h-table">Closest comparables</h2>
      <p class="small">Ranked by similarity to your flat (distance, recency, type, garden, ground floor, layout), not by price.</p>
      <div class="tablewrap"><table id="tbl"></table></div>
      <details><summary>Show every comparable in view</summary><div class="tablewrap"><table id="tblAll"></table></div></details>
    </section>

    <section class="card span-12 note" aria-labelledby="h-notes">
      <h2 id="h-notes">About this data</h2>
      <ul id="notes"></ul>
    </section>
  </div>
</main>
<div id="tip" role="status" aria-live="polite"></div>

<script>
const D = /*__DATA__*/null;
const $ = (s) => document.querySelector(s);
const gbp = (v, dp = 0) => v == null ? "–" : "£" + Number(v).toLocaleString("en-GB", {minimumFractionDigits: dp, maximumFractionDigits: dp});
const pct = (v) => (v > 0 ? "+" : "") + v.toFixed(1) + "%";
const fmtDate = (s) => s ? new Date(s + (s.length === 7 ? "-01" : "")).toLocaleDateString("en-GB", {month: "short", year: "numeric"}) : "–";
const median = (a) => { const v = a.filter(x => x != null).sort((x, y) => x - y); if (!v.length) return null;
  const m = Math.floor(v.length / 2); return v.length % 2 ? v[m] : (v[m - 1] + v[m]) / 2; };
const quant = (a, p) => { const v = a.filter(x => x != null).sort((x, y) => x - y); if (!v.length) return null;
  const i = (v.length - 1) * p, lo = Math.floor(i); return v[lo] + (v[Math.min(lo + 1, v.length - 1)] - v[lo]) * (i - lo); };
const NS = "http://www.w3.org/2000/svg";
function el(tag, attrs = {}, parent) { const e = document.createElementNS(NS, tag);
  for (const k in attrs) e.setAttribute(k, attrs[k]); if (parent) parent.appendChild(e); return e; }
function text(parent, x, y, s, cls, anchor = "start") { const t = el("text", {x, y, "text-anchor": anchor}, parent);
  if (cls) t.setAttribute("class", cls); t.textContent = s; return t; }
function niceTicks(lo, hi, n = 5) { const span = hi - lo, step0 = span / n, mag = Math.pow(10, Math.floor(Math.log10(step0)));
  const step = [1, 2, 2.5, 5, 10].map(m => m * mag).find(s => span / s <= n) || mag * 10;
  const out = []; for (let v = Math.ceil(lo / step) * step; v <= hi + 1e-9; v += step) out.push(v); return out; }
function legend(id, items) { const L = $(id); L.innerHTML = ""; items.forEach(([label, color, kind]) => {
  const k = document.createElement("span"); k.className = "key"; const i = document.createElement("i");
  if (kind) i.className = kind; if (kind === "ring") { i.className = "dot"; i.style.border = "2px solid " + color; i.style.background = "transparent"; } else i.style.background = color; k.appendChild(i); k.appendChild(document.createTextNode(label)); L.appendChild(k); }); }
const css = (v) => getComputedStyle(document.documentElement).getPropertyValue(v).trim();

// ---------- tooltip ----------
const tip = $("#tip");
function showTip(evt, lines) {
  tip.innerHTML = ""; lines.forEach(([val, label, color], i) => {
    if (i === 0) { const s = document.createElement("strong"); s.textContent = val; tip.appendChild(s);
      if (label) { const d = document.createElement("div"); d.textContent = label; d.style.color = "var(--ink-2)"; tip.appendChild(d); } return; }
    const r = document.createElement("div"); r.className = "row";
    if (color) { const k = document.createElement("i"); k.style.background = color; r.appendChild(k); }
    const b = document.createElement("b"); b.textContent = val; b.style.color = "var(--ink)"; r.appendChild(b);
    r.appendChild(document.createTextNode(" " + (label || ""))); tip.appendChild(r); });
  const r = evt.target.getBoundingClientRect ? evt.target.getBoundingClientRect() : null;
  const x = evt.clientX ?? (r ? r.x + r.width / 2 : 0), y = evt.clientY ?? (r ? r.y : 0);
  tip.style.opacity = 1; const w = tip.offsetWidth, h = tip.offsetHeight;
  tip.style.left = Math.min(window.innerWidth - w - 8, Math.max(8, x + 14)) + "px";
  tip.style.top = Math.max(8, y - h - 12) + "px";
}
const hideTip = () => { tip.style.opacity = 0; };

// ---------- state ----------
const state = { period: "12", km: "99", status: "let", adj: "today" };
try { Object.assign(state, JSON.parse(localStorage.getItem("rc-filters") || "{}")); } catch (e) {}
const today = new Date();
const currentRent = D.rentHistory.length ? D.rentHistory[D.rentHistory.length - 1].rent : null;
const tenancyStart = D.rentHistory.length ? D.rentHistory[0].from : null;
const valid = D.comps.filter(c => !c.excluded);
function inView() {
  const months = +state.period, maxKm = +state.km;
  return valid.filter(c => {
    if (state.status === "let" && c.status !== "let_agreed") return false;
    if (c.km == null || c.km > maxKm) return false;
    const d = c.status === "let_agreed" ? c.let : c.listed;
    if (!d) return months >= 999;
    return (today - new Date(d)) / 864e5 <= months * 30.44;
  });
}

// ---------- headline + tiles ----------
function renderTiles(rows) {
  const prices = rows.map(r => r.price), med = median(prices);
  const word = state.status === "let" ? "agreed" : "asking and agreed";
  $("#tRent").textContent = gbp(currentRent, 2);
  if (D.rentHistory.length > 1) { const first = D.rentHistory[0].rent;
    $("#tRentD").innerHTML = ""; const s = document.createElement("span"); s.className = "up-bad";
    s.textContent = pct((currentRent / first - 1) * 100); $("#tRentD").append(s, " since " + fmtDate(tenancyStart)); }
  $("#tMedL").textContent = `Median ${word} rent` + (state.adj === "loc" ? " (adjusted to your area)" : state.adj === "today" ? " (today's money)" : "");
  $("#tMed").textContent = gbp(med);
  const lq = quant(prices, .25), uq = quant(prices, .75);
  $("#tMedD").textContent = lq != null ? `Middle half: ${gbp(lq)}–${gbp(uq)}` : "No comparables in view";
  if (D.ons.length) { const last = D.ons[D.ons.length - 1];
    $("#tOns").textContent = gbp(last.lew);
    $("#tOnsD").textContent = `${pct(last.chg)} on a year earlier · ${fmtDate(last.p)}`; }
  else { $("#tOns").textContent = "–"; $("#tOnsD").textContent = "Run ons_index.py to load"; }
  const nLet = rows.filter(r => r.status === "let_agreed").length;
  $("#tN").textContent = rows.length;
  $("#tND").textContent = state.status === "let" ? "all let agreed" : `${nLet} let agreed · ${rows.length - nLet} advertised`;

  if (med != null && currentRent) {
    const diff = currentRent - med, above = prices.filter(p => p < currentRent).length;
    $("#heroValue").textContent = (diff >= 0 ? "+" : "−") + gbp(Math.abs(diff)) + " / month";
    $("#heroLabel").textContent = `Your rent vs median ${word} rent`;
    $("#heroCap").textContent = `You pay ${gbp(currentRent, 2)}. Of ${prices.length} comparable 2-bed listings in view, ` +
      `${above} (${Math.round(above / prices.length * 100)}%) are cheaper. ` +
      (D.proposed ? `At ${gbp(D.proposed[0])}–${gbp(D.proposed[1])}, you would pay more than ` +
        `${Math.round(prices.filter(p => p < D.proposed[0]).length / prices.length * 100)}% of them.` : "");
  } else { $("#heroValue").textContent = "–"; $("#heroCap").textContent = "No comparables match these filters."; }
}

// ---------- chart 1: strip plot of rents ----------
function renderDist(rows) {
  const box = $("#c-dist"); box.innerHTML = "";
  legend("#lg-dist", [["Agreed rent", css("--s1"), "dot"],
    ...(rows.some(c => c.status !== "let_agreed") ? [["Asking rent", css("--s3"), "dot"]] : []), ["Your rent", css("--s2")],
    ...(D.proposed ? [["Expected proposal", css("--s2-wash"), "band"]] : [])]);
  if (!rows.length) { box.textContent = "No comparables match these filters."; return; }
  const W = Math.max(300, box.clientWidth), H = 210, m = {l: 16, r: 16, t: 34, b: 34};
  const vals = rows.map(r => r.price).concat([currentRent], D.proposed || []);
  const lo = Math.floor((Math.min(...vals) - 50) / 100) * 100, hi = Math.ceil((Math.max(...vals) + 50) / 100) * 100;
  const x = v => m.l + (v - lo) / (hi - lo) * (W - m.l - m.r);
  const svg = el("svg", {viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": "Strip plot of comparable rents with your rent marked"}, box);
  niceTicks(lo, hi, W < 600 ? 4 : 8).forEach(t => { el("line", {x1: x(t), x2: x(t), y1: m.t, y2: H - m.b, class: "gridline"}, svg);
    text(svg, x(t), H - m.b + 18, gbp(t), null, "middle"); });
  el("line", {x1: m.l, x2: W - m.r, y1: H - m.b, y2: H - m.b, class: "axis"}, svg);
  if (D.proposed) { el("rect", {x: x(D.proposed[0]), y: m.t, width: x(D.proposed[1]) - x(D.proposed[0]), height: H - m.t - m.b,
      fill: "var(--s2-wash)", rx: 4}, svg);
    if (W >= 600) text(svg, x(D.proposed[1]) + 6, m.t + 12, "Expected proposal", "lbl"); }
  // beeswarm-ish: stack dots that would overlap
  const r = 5, placed = [];
  [...rows].sort((a, b) => a.price - b.price).forEach(c => {
    const cx = x(c.price); let k = 0, cy;
    while (true) { cy = (H - m.b - m.t) / 2 + m.t + (k % 2 ? 1 : -1) * Math.ceil(k / 2) * (2 * r + 2);
      if (!placed.some(p => Math.hypot(p[0] - cx, p[1] - cy) < 2 * r + 2)) break; k++; if (k > 40) break; }
    placed.push([cx, cy]);
    const g = el("g", {}, svg);
    const hit = el("circle", {cx, cy, r: 12, class: "hit", tabindex: 0, role: "img",
      "aria-label": `${c.title}, ${gbp(c.price)} per month`}, g);
    const dot = el("circle", {cx, cy, r, fill: c.status === "let_agreed" ? "var(--s1)" : "var(--s3)", stroke: "var(--surface)", "stroke-width": 2, class: "mark"}, g);
    const show = e => { dot.classList.add("on"); showTip(e, [[gbp(c.price) + " pcm", c.title + (c.raw !== c.price ? ` (${c.status === "let_agreed" ? "agreed at" : "asking price"} ${gbp(c.raw)})` : "")],
      [c.status === "let_agreed" ? "Let agreed " + fmtDate(c.let) : "Advertised since " + fmtDate(c.listed), ""],
      [c.km + " km", "from your flat"]]); };
    hit.addEventListener("pointermove", show); hit.addEventListener("focus", show);
    hit.addEventListener("pointerleave", () => { dot.classList.remove("on"); hideTip(); });
    hit.addEventListener("blur", () => { dot.classList.remove("on"); hideTip(); });
    hit.addEventListener("click", () => window.open(c.url, "_blank", "noopener"));
  });
  const med = median(rows.map(r => r.price));
  el("line", {x1: x(med), x2: x(med), y1: m.t - 6, y2: H - m.b, stroke: "var(--ink-2)", "stroke-width": 1}, svg);
  text(svg, x(med), m.t - 10, "Median " + gbp(med), "lbl", "middle");
  el("line", {x1: x(currentRent), x2: x(currentRent), y1: m.t - 6, y2: H - m.b, stroke: "var(--s2)", "stroke-width": 2}, svg);
  const yl = Math.abs(x(currentRent) - x(med)) < 90 ? H - m.b - 6 : m.t - 10;
  text(svg, x(currentRent) + (yl === m.t - 10 ? 0 : 6), yl, "You " + gbp(currentRent), "lbl-strong", yl === m.t - 10 ? "middle" : "start");
}

// ---------- chart 2: over time ----------
function renderTime(rows) {
  const box = $("#c-time"); box.innerHTML = "";
  legend("#lg-time", [["Agreed rent (one let)", css("--s1"), "dot"], ["ONS Lewisham 2-bed average", css("--s3")], ["Your rent", css("--s2")]]);
  const lets = rows.filter(r => r.status === "let_agreed" && r.let).map(r => ({...r, price: r.raw ?? r.price}));
  const W = Math.max(300, box.clientWidth), H = 320, m = {l: 56, r: 92, t: 14, b: 30};
  const t0 = new Date(Math.min(new Date(tenancyStart || "2024-01-01"), ...lets.map(r => new Date(r.let))) - 30 * 864e5);
  const t1 = today;
  const ons = D.ons.filter(o => new Date(o.p + "-01") >= t0);
  const vals = lets.map(r => r.price).concat(ons.map(o => o.lew), D.rentHistory.map(h => h.rent));
  const lo = Math.floor((Math.min(...vals) - 100) / 250) * 250, hi = Math.ceil((Math.max(...vals) + 100) / 250) * 250;
  const x = d => m.l + (new Date(d) - t0) / (t1 - t0) * (W - m.l - m.r), y = v => H - m.b - (v - lo) / (hi - lo) * (H - m.t - m.b);
  const svg = el("svg", {viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": "Agreed rents over time with the ONS Lewisham average and your rent"}, box);
  niceTicks(lo, hi, 5).forEach(t => { el("line", {x1: m.l, x2: W - m.r, y1: y(t), y2: y(t), class: "gridline"}, svg);
    text(svg, m.l - 8, y(t) + 4, gbp(t), null, "end"); });
  for (let yr = t0.getFullYear(); yr <= t1.getFullYear(); yr++) { const d = new Date(yr, 0, 1);
    if (d >= t0 && d <= t1) text(svg, x(d), H - m.b + 18, String(yr), null, "middle"); }
  el("line", {x1: m.l, x2: W - m.r, y1: H - m.b, y2: H - m.b, class: "axis"}, svg);
  lets.forEach(c => el("circle", {cx: x(c.let), cy: y(c.price), r: 4, fill: "var(--s1)", "fill-opacity": .75, stroke: "var(--surface)", "stroke-width": 2}, svg));
  if (ons.length) {
    el("path", {d: ons.map((o, i) => (i ? "L" : "M") + x(o.p + "-01") + "," + y(o.lew)).join(""), fill: "none", stroke: "var(--s3)", "stroke-width": 2, "stroke-linejoin": "round"}, svg);
    const lo_ = ons[ons.length - 1]; const ly = Math.abs(y(lo_.lew) - y(currentRent || 0)) < 16 ? y(lo_.lew) + 14 : y(lo_.lew) + 4;
    el("line", {x1: x(lo_.p + "-01"), x2: W - m.r + 4, y1: y(lo_.lew), y2: y(lo_.lew), stroke: "var(--s3)", "stroke-width": 1, "stroke-opacity": .5}, svg);
    text(svg, W - m.r + 8, ly, "ONS " + gbp(lo_.lew), "lbl");
  }
  if (D.rentHistory.length) {
    let p = ""; D.rentHistory.forEach((h, i) => { const nx = i + 1 < D.rentHistory.length ? D.rentHistory[i + 1].from + "-01" : t1;
      p += (i ? "L" : "M") + x(h.from + "-01") + "," + y(h.rent) + "L" + x(nx) + "," + y(h.rent); });
    el("path", {d: p, fill: "none", stroke: "var(--s2)", "stroke-width": 2, "stroke-linejoin": "round"}, svg);
    text(svg, W - m.r + 8, y(currentRent) + 4, "You " + gbp(currentRent), "lbl-strong");
  }
  // crosshair
  const cross = el("line", {y1: m.t, y2: H - m.b, stroke: "var(--axis)", "stroke-width": 1, opacity: 0}, svg);
  const ov = el("rect", {x: m.l, y: m.t, width: W - m.l - m.r, height: H - m.t - m.b, fill: "transparent"}, svg);
  ov.addEventListener("pointermove", e => {
    const pt = svg.createSVGPoint(); pt.x = e.clientX; pt.y = e.clientY; const sx = pt.matrixTransform(svg.getScreenCTM().inverse()).x;
    const d = new Date(+t0 + (sx - m.l) / (W - m.l - m.r) * (t1 - t0)); const key = d.toISOString().slice(0, 7);
    cross.setAttribute("x1", sx); cross.setAttribute("x2", sx); cross.setAttribute("opacity", 1);
    const o = ons.reduce((b, v) => !b || Math.abs(new Date(v.p + "-01") - d) < Math.abs(new Date(b.p + "-01") - d) ? v : b, null);
    const h = [...D.rentHistory].reverse().find(h => h.from <= key);
    const near = lets.filter(c => Math.abs(new Date(c.let) - d) < 45 * 864e5).map(c => c.price);
    const lines = [[fmtDate(key), ""]];
    if (h) lines.push([gbp(h.rent, 2), "your rent", css("--s2")]);
    if (o) lines.push([gbp(o.lew), "ONS Lewisham average", css("--s3")]);
    lines.push([near.length ? gbp(median(near)) : "–", `median of ${near.length} lets within 6 weeks`, css("--s1")]);
    showTip(e, lines);
  });
  ov.addEventListener("pointerleave", () => { cross.setAttribute("opacity", 0); hideTip(); });
}

// ---------- chart 3: growth indexed ----------
function renderGrowth() {
  const box = $("#c-growth"); box.innerHTML = "";
  if (!D.ons.length || !D.rentHistory.length) { box.textContent = "Load ONS data to compare."; return; }
  legend("#lg-growth", [["Your rent", css("--s2")], ["Lewisham 2-bed index", css("--s3")]]);
  const start = D.rentHistory[0].from, base = D.ons.find(o => o.p === start) || D.ons.find(o => o.p >= start);
  const ons = D.ons.filter(o => o.p >= base.p);
  const lastP = ons[ons.length - 1].p;
  const you = []; D.rentHistory.forEach((h, i) => { const nx = i + 1 < D.rentHistory.length ? D.rentHistory[i + 1].from : null;
    you.push([h.from, (h.rent / D.rentHistory[0].rent - 1) * 100]);
    you.push([nx ? prevMonth(nx) : lastP, (h.rent / D.rentHistory[0].rent - 1) * 100]); });
  function prevMonth(p) { const d = new Date(p + "-01"); d.setMonth(d.getMonth() - 1); return d.toISOString().slice(0, 7); }
  const onsPts = ons.map(o => [o.p, (o.lew / base.lew - 1) * 100]);
  const W = Math.max(280, box.clientWidth), H = 320, m = {l: 40, r: 64, t: 14, b: 30};
  const all = you.concat(onsPts).map(p => p[1]); const lo = Math.min(0, ...all) - 1, hi = Math.max(...all) + 1;
  const t0 = new Date(base.p + "-01"), t1 = new Date(lastP + "-01");
  const x = p => m.l + (new Date(p + "-01") - t0) / (t1 - t0) * (W - m.l - m.r), y = v => H - m.b - (v - lo) / (hi - lo) * (H - m.t - m.b);
  const svg = el("svg", {viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": "Percentage change in your rent versus the Lewisham index"}, box);
  niceTicks(lo, hi, 5).forEach(t => { el("line", {x1: m.l, x2: W - m.r, y1: y(t), y2: y(t), class: t === 0 ? "axis" : "gridline"}, svg);
    text(svg, m.l - 6, y(t) + 4, (t > 0 ? "+" : "") + t + "%", null, "end"); });
  for (let yr = t0.getFullYear(); yr <= t1.getFullYear(); yr++) { const p = yr + "-01"; if (new Date(p + "-01") >= t0) text(svg, x(p), H - m.b + 18, String(yr), null, "middle"); }
  const path = (pts, c) => el("path", {d: pts.map((p, i) => (i ? "L" : "M") + x(p[0]) + "," + y(p[1])).join(""), fill: "none", stroke: c, "stroke-width": 2, "stroke-linejoin": "round"}, svg);
  path(onsPts, "var(--s3)"); path(you, "var(--s2)");
  const ye = you[you.length - 1], oe = onsPts[onsPts.length - 1];
  const gap = Math.abs(y(ye[1]) - y(oe[1])) < 16 ? 9 : 0;
  el("circle", {cx: x(ye[0]), cy: y(ye[1]), r: 4, fill: "var(--s2)", stroke: "var(--surface)", "stroke-width": 2}, svg);
  el("circle", {cx: x(oe[0]), cy: y(oe[1]), r: 4, fill: "var(--s3)", stroke: "var(--surface)", "stroke-width": 2}, svg);
  text(svg, x(ye[0]) + 8, y(ye[1]) + 4 - gap, pct(ye[1]), "lbl-strong");
  text(svg, x(oe[0]) + 8, y(oe[1]) + 4 + gap, pct(oe[1]), "lbl");
}

// ---------- chart 4: boroughs ----------
function renderBoroughs() {
  const card = $("#boroCard"), box = $("#c-boro"); box.innerHTML = "";
  if (!D.boroughs.length) { card.style.display = "none"; return; }
  const B = D.boroughs.filter(b => b.area !== "City of London" && b.area !== "Kensington and Chelsea" && b.area !== "Westminster" ? true : true);
  $("#boroSub").textContent = `ONS Price Index of Private Rents, ${fmtDate(D.ons.length ? D.ons[D.ons.length - 1].p : "")}. ` +
    "Covers new and existing tenancies. Lewisham highlighted; your rent marked for scale.";
  const W = Math.max(300, box.clientWidth), rowH = 18, m = {l: W < 600 ? 128 : 170, r: 16, t: 22, b: 26}, H = m.t + m.b + B.length * rowH;
  const hi = Math.ceil(Math.max(...B.map(b => b.price), currentRent || 0) / 500) * 500;
  const x = v => m.l + v / hi * (W - m.l - m.r);
  const svg = el("svg", {viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": "Average 2-bed rent by London borough"}, box);
  niceTicks(0, hi, W < 600 ? 3 : 6).forEach(t => { el("line", {x1: x(t), x2: x(t), y1: m.t, y2: H - m.b, class: t ? "gridline" : "axis"}, svg);
    text(svg, x(t), H - m.b + 18, gbp(t), null, "middle"); });
  B.forEach((b, i) => {
    const yy = m.t + i * rowH, isL = b.area === "Lewisham", h = 12;
    text(svg, m.l - 10, yy + h - 2, b.area, isL ? "lbl-strong" : "lbl", "end");
    const w = x(b.price) - x(0);
    const bar = el("path", {d: `M${x(0)},${yy}h${w - 4}a4,4 0 0 1 4,4v${h - 8}a4,4 0 0 1 -4,4h${-(w - 4)}z`,
      fill: isL ? "var(--s1)" : "var(--neutral)", class: "mark"}, svg);
    if (isL) text(svg, x(b.price) + 6, yy + h - 2, gbp(b.price), "lbl-strong");
    const hit = el("rect", {x: 0, y: yy - 3, width: W, height: rowH, class: "hit", tabindex: -1}, svg);
    const show = e => { bar.classList.add("on"); showTip(e, [[gbp(b.price) + " pcm", b.area], [pct(b.chg), "on a year earlier"]]); };
    hit.addEventListener("pointermove", show);
    hit.addEventListener("pointerleave", () => { bar.classList.remove("on"); hideTip(); });
  });
  if (currentRent) { el("line", {x1: x(currentRent), x2: x(currentRent), y1: m.t - 8, y2: H - m.b, stroke: "var(--s2)", "stroke-width": 2}, svg);
    text(svg, x(currentRent), m.t - 10, "Your rent " + gbp(currentRent), "lbl-strong", "middle"); }
}

// ---------- tables ----------
function tableInto(id, rows) {
  const t = $(id); t.innerHTML = "";
  const head = ["Score", "Rent", "Status", "Date", "Distance", "Property", "Neighbourhood", "Days advertised", "Floor m²", "Why it matches"];
  const tr = t.createTHead().insertRow(); head.forEach((h, i) => { const th = document.createElement("th"); th.textContent = h;
    if ([0, 1, 4, 6, 8].includes(i)) th.className = "num"; tr.appendChild(th); });
  const tb = t.createTBody();
  rows.forEach(c => { const r = tb.insertRow();
    const cell = (v, cls) => { const td = r.insertCell(); if (cls) td.className = cls; if (v instanceof Node) td.appendChild(v); else td.textContent = v ?? "–"; return td; };
    cell(c.score, "num"); cell(gbp(c.price), "num");
    const p = document.createElement("span"); p.className = "pill"; p.textContent = c.status === "let_agreed" ? "Let agreed" : "Advertised"; cell(p);
    cell(c.status === "let_agreed" ? fmtDate(c.let) : fmtDate(c.listed)); cell(c.km != null ? c.km + " km" : "–", "num");
    const a = document.createElement("a"); a.href = c.url; a.target = "_blank"; a.rel = "noopener"; a.textContent = c.title; cell(a);
    cell((c.area ? `${c.area} (×${c.ratio})` : "–") + (c.station ? ` · ${c.stkm} km to ${c.station}` : "") + (c.btype ? ` · ${c.btype.replace(/_/g, " ")}` : "")); cell(c.days ?? "–", "num"); cell(c.m2 ? Math.round(c.m2) : "–", "num"); cell(c.why);
  });
}

function renderDates() {
  const T = D.timeline || [], card = $("#datesCard"); if (!T.length) { card.style.display = "none"; return; }
  const iso = today.toISOString().slice(0, 10);
  const days = d => Math.round((new Date(d) - new Date(iso)) / 864e5);
  const live = T.filter(t => t.status !== "done" || (t.deadline && t.deadline >= iso)).sort((a, b) => (a.deadline || a.date).localeCompare(b.deadline || b.date));
  const past = T.filter(t => !live.includes(t)).sort((a, b) => b.date.localeCompare(a.date));
  const fill = (id, rows, showDue) => { const t = $(id); t.innerHTML = "";
    const tr = t.createTHead().insertRow(); (showDue ? ["Date", "What", "Status", "Deadline", "Notes"] : ["Date", "What", "Notes"]).forEach(h => {
      const th = document.createElement("th"); th.textContent = h; tr.appendChild(th); });
    const tb = t.createTBody();
    rows.forEach(x => { const r = tb.insertRow(); const c = v => { const td = r.insertCell(); td.textContent = v || "–"; return td; };
      c(new Date(x.date).toLocaleDateString("en-GB", {day: "numeric", month: "short", year: "numeric"})); c(x.event);
      if (showDue) { const s = document.createElement("span"); s.className = "pill"; s.textContent = x.status; r.insertCell().appendChild(s);
        const td = c(x.deadline ? new Date(x.deadline).toLocaleDateString("en-GB", {day: "numeric", month: "short", year: "numeric"}) : "");
        if (x.deadline) { const n = days(x.deadline); const d = document.createElement("div"); d.className = "small";
          d.textContent = n > 0 ? `in ${n} day${n === 1 ? "" : "s"}` : n === 0 ? "today" : `${-n} days ago`; if (n <= 7 && n >= 0) d.style.color = "var(--bad)"; td.appendChild(d); } }
      c(x.notes); }); };
  fill("#datesTbl", live, true); fill("#datesPast", past, false);
}

function renderGrid() {
  const G = D.grid, card = $("#gridCard");
  if (!G || !G.lines.length) { card.style.display = "none"; return; }
  const c = G.conclusion, S = G.settings;
  $("#gridSub").textContent = `The ${c.n} most similar let-agreed flats within ${S.max_km} km, let in the last ${S.max_months} months, ` +
    "each adjusted to be like your flat: first to today's money with the ONS Lewisham index, then for differences in layout and features. " +
    "Fixed: not affected by the filters above.";
  $("#gridValue").textContent = gbp(c.indicated);
  const diff = currentRent - c.indicated;
  $("#gridCap").textContent = `Similarity-weighted average of the adjusted rents is ${gbp(c.weighted_mean)} ` +
    `(range ${gbp(c.low)}–${gbp(c.high)})` + (c.defects.length ? `, less ${Math.abs(c.condition_pct)}% for your flat's defects still open (` +
      c.defects.map(d => `${d.issue.toLowerCase()} ${d.pct}%, reported ${fmtDate(d.since)}`).join("; ") + "). " : ". No open defects deducted. ") +
    `You pay ${gbp(currentRent, 2)}, which is ${gbp(Math.abs(diff))} a month ${diff >= 0 ? "above" : "below"} it.` +
    (c.size_note ? ` Size caveat: your flat is ${D.subjectM2} m²; ${c.size_note.n_without_area} of these comparables have no known floor area. ` +
      `If they are the typical ${c.size_note.typical_m2} m², the indicated rent rises to ${gbp(c.size_note.indicated)}.` : "") +
    (c.loc_alt ? ` Location check: measured against ${c.loc_alt.alt_label} (${gbp(c.loc_alt.ref_price)} median flat price instead of ` +
      `${gbp(c.loc_alt.main_ref_price)}), the figure would be ${gbp(c.loc_alt.indicated)}.` : "");
  const t = $("#gridTbl"); t.innerHTML = "";
  const head = ["Comparable", "Let", "Distance", "Agreed rent", "Time", "Other adjustments", "Adjusted"];
  const tr = t.createTHead().insertRow(); head.forEach((h, i) => { const th = document.createElement("th"); th.textContent = h;
    if ([2, 3, 4, 6].includes(i)) th.className = "num"; tr.appendChild(th); });
  const tb = t.createTBody();
  const signed = (v) => { const s = document.createElement("span"); s.textContent = (v > 0 ? "+" : v < 0 ? "−" : "±") + Math.abs(v).toFixed(1) + "%";
    s.className = v < 0 ? "adjneg" : v > 0 ? "adjpos" : ""; return s; };
  G.lines.forEach(l => { const r = tb.insertRow();
    const cell = (v, cls) => { const td = r.insertCell(); if (cls) td.className = cls; if (v instanceof Node) td.appendChild(v); else td.textContent = v ?? "–"; return td; };
    const a = document.createElement("a"); a.href = l.url; a.target = "_blank"; a.rel = "noopener"; a.textContent = l.title; cell(a);
    cell(fmtDate(l.let)); cell(l.km + " km", "num"); cell(gbp(l.rent), "num"); cell(signed(l.time_pct), "num");
    const td = cell(""); if (!l.adjustments.length) td.textContent = "None";
    l.adjustments.forEach((x, i) => { const d = document.createElement("div"); d.append(x.label + " ", signed(x.pct)); td.appendChild(d); });
    if (l.ai) { const d = document.createElement("div"); d.className = "small";
      d.textContent = "AI photo review (" + l.ai.confidence + " confidence): " + [l.ai.bedroom_evidence, l.ai.condition_evidence].filter(Boolean).join(" ");
      td.appendChild(d); }
    if (l.check) { const d = document.createElement("div"); d.className = "small"; d.textContent = l.check; td.appendChild(d); }
    cell(gbp(l.adjusted), "num"); });
  const add = (label, val) => { const r = tb.insertRow(); r.className = "total"; const a = r.insertCell(); a.colSpan = 6; a.textContent = label;
    const b = r.insertCell(); b.className = "num"; b.textContent = val; };
  add("Similarity-weighted average", gbp(c.weighted_mean));
  add(c.defects.length ? `Less your flat's open defects (${c.defects.map(d => d.issue.toLowerCase() + " " + d.pct + "%").join(", ")})` : "No open defects to deduct", gbp(c.indicated));
}

function renderSens() {
  const S = D.sens, card = $("#sensCard");
  if (!S || !S.of) { card.style.display = "none"; return; }
  const L = S.location || {};
  $("#sensSub").textContent = `Median agreed rent for every combination of distance and period, in today's money and adjusted to your location ` +
    `(ONS median flat prices by neighbourhood; rents move about ${L.beta} as much as prices, ${L.basis} from these lets). ` +
    `Your rent is above the median in ${S.above_in} of ${S.of}, by ${gbp(S.gap_min)}–${gbp(S.gap_max)} a month` +
    (S.unadjusted ? ` (without the location adjustment: ${S.unadjusted.above_in} of ${S.unadjusted.of}, by ${gbp(S.unadjusted.gap_min)}–${gbp(S.unadjusted.gap_max)})` : "") +
    ". The range under each figure is a 90% bootstrap interval for the median.";
  const t = $("#sensTbl"); t.innerHTML = "";
  const months = [...new Set(S.cells.map(c => c.months))], kms = [...new Set(S.cells.map(c => c.km))];
  const tr = t.createTHead().insertRow(); ["", ...months.map(m => `Let in last ${m} months`)].forEach(h => {
    const th = document.createElement("th"); th.textContent = h; tr.appendChild(th); });
  const tb = t.createTBody();
  kms.forEach(km => { const r = tb.insertRow(); const th = document.createElement("th"); th.textContent = `Within ${km} km`; r.appendChild(th);
    months.forEach(m => { const c = S.cells.find(x => x.km === km && x.months === m); const td = r.insertCell();
      if (!c || !c.n) { td.textContent = "–"; return; }
      const big = document.createElement("div"); big.className = "m"; big.textContent = gbp(c.median);
      const g = document.createElement("div"); g.className = "g"; g.textContent = `you pay ${c.gap >= 0 ? "+" : "−"}${gbp(Math.abs(c.gap))}`;
      const sm = document.createElement("div"); sm.className = "small";
      sm.textContent = `${c.n} lets` + (c.ci_low ? ` · ${gbp(c.ci_low)}–${gbp(c.ci_high)}` : "");
      td.append(big, g, sm); }); });
}

const bedLabel = t => t.property_type === "room" ? "room" : t.bedrooms === 0 ? "studio" : `${t.bedrooms ?? "?"}-bed ${t.property_type || ""}`.trim();
function renderTribunal() {
  const T = (D.tribunal || []), card = $("#tribCard"), box = $("#c-trib"); box.innerHTML = "";
  const pts = T.filter(t => t.private && t.proposed_rent && t.determined_rent).slice(0, 14);
  if (!T.length) { card.style.display = "none"; return; }
  const below = pts.filter(t => t.determined_rent < t.proposed_rent).length;
  $("#tribSub").textContent = `Section 13 decisions by the First-tier Tribunal within ${D.tribunalKm} km. Private landlords, newest first; ` +
    `private 2-bed flats in bold. In ${below} of ${pts.length} the tribunal set the rent below what the landlord proposed. ` +
    `Figures from Rent Rise Check, re-checked automatically against each GOV.UK decision (${T.filter(t => t.verified === 1).length} of ${T.length} confirmed). Click a row to open the decision.`;
  legend("#lg-trib", [["Rent before the notice", css("--muted"), "dot"], ["Landlord proposed", css("--ink-2"), "ring"],
    ["Tribunal decided", css("--s1"), "dot"], ["Your rent", css("--s2")]]);
  if (pts.length) {
    const W = Math.max(300, box.clientWidth), narrow = W < 600, rowH = narrow ? 34 : 26;
    const m = {l: narrow ? 8 : 230, r: 20, t: 26, b: 26}, H = m.t + m.b + pts.length * rowH;
    const vals = pts.flatMap(t => [t.current_rent, t.proposed_rent, t.determined_rent]).filter(Boolean).concat([currentRent]);
    const lo = Math.floor((Math.min(...vals) - 100) / 250) * 250, hi = Math.ceil((Math.max(...vals) + 100) / 250) * 250;
    const x = v => m.l + (v - lo) / (hi - lo) * (W - m.l - m.r);
    const svg = el("svg", {viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": "Tribunal decisions: rent proposed versus rent decided"}, box);
    niceTicks(lo, hi, narrow ? 4 : 7).forEach(t => { el("line", {x1: x(t), x2: x(t), y1: m.t, y2: H - m.b, class: "gridline"}, svg);
      text(svg, x(t), H - m.b + 18, gbp(t), null, "middle"); });
    el("line", {x1: x(currentRent), x2: x(currentRent), y1: m.t - 8, y2: H - m.b, stroke: "var(--s2)", "stroke-width": 2}, svg);
    text(svg, x(currentRent), m.t - 12, "Your rent " + gbp(currentRent), "lbl-strong", "middle");
    pts.forEach((t, i) => {
      const yy = m.t + i * rowH + rowH / 2 + (narrow ? 6 : 0);
      const label = `${fmtDate(t.decision_date)} · ${t.postcode} · ${bedLabel(t)}`;
      if (narrow) text(svg, m.l, yy - 11, label, t.comparable ? "lbl-strong" : "lbl");
      else text(svg, m.l - 12, yy + 4, label, t.comparable ? "lbl-strong" : "lbl", "end");
      const g = el("g", {}, svg);
      el("line", {x1: x(t.proposed_rent), x2: x(t.determined_rent), y1: yy, y2: yy, stroke: "var(--axis)", "stroke-width": 2}, g);
      if (t.current_rent) el("circle", {cx: x(t.current_rent), cy: yy, r: 4, fill: "var(--muted)", stroke: "var(--surface)", "stroke-width": 2}, g);
      el("circle", {cx: x(t.proposed_rent), cy: yy, r: 5, fill: "var(--surface)", stroke: "var(--ink-2)", "stroke-width": 2}, g);
      const dec = el("circle", {cx: x(t.determined_rent), cy: yy, r: 5, fill: "var(--s1)", stroke: "var(--surface)", "stroke-width": 2, class: "mark"}, g);
      const hit = el("rect", {x: 0, y: yy - rowH / 2 - (narrow ? 6 : 0), width: W, height: rowH, class: "hit", tabindex: 0}, g);
      const show = e => { dec.classList.add("on"); showTip(e, [[gbp(t.determined_rent) + " decided", label],
        [gbp(t.proposed_rent), "landlord proposed", css("--ink-2")], [t.current_rent ? gbp(t.current_rent) : "–", "rent before", css("--muted")],
        [t.distance_km + " km", "from your flat"], [t.post_act ? "New rules" : "Old rules", "(notice from 1 May 2026 = new)"],
        ...(t.good_condition_rent ? [[gbp(t.good_condition_rent), "in good condition, before deductions" + (t.deduction_pct ? ` (−${t.deduction_pct}%)` : "")]] : []),
        [t.verified === 1 ? "✓ Checked" : t.verified === 0 ? "⚠ Differs" : "Not yet checked", "against the GOV.UK decision text"],
        ...(t.lesson ? [["Lesson:", t.lesson]] : [])]); };
      hit.addEventListener("pointermove", show); hit.addEventListener("focus", show);
      hit.addEventListener("pointerleave", () => { dec.classList.remove("on"); hideTip(); });
      hit.addEventListener("blur", () => { dec.classList.remove("on"); hideTip(); });
      hit.addEventListener("click", () => window.open(t.url, "_blank", "noopener"));
    });
  } else box.textContent = "No private-landlord decisions with full figures nearby.";
  const t = $("#tribTbl"); t.innerHTML = "";
  const head = ["Decided", "Distance", "Postcode", "Property", "Landlord", "Before", "Proposed", "Decided", "Rules", "Checked", "Good-condition rent"];
  const tr = t.createTHead().insertRow(); head.forEach((h, i) => { const th = document.createElement("th"); th.textContent = h;
    if ([1, 5, 6, 7].includes(i)) th.className = "num"; tr.appendChild(th); });
  const tb = t.createTBody();
  T.forEach(d => { const r = tb.insertRow(); if (d.comparable) r.style.fontWeight = 600;
    const cell = (v, cls) => { const td = r.insertCell(); if (cls) td.className = cls; if (v instanceof Node) td.appendChild(v); else td.textContent = v ?? "–"; };
    const a = document.createElement("a"); a.href = d.url; a.target = "_blank"; a.rel = "noopener"; a.textContent = fmtDate(d.decision_date); cell(a);
    cell(d.distance_km + " km", "num"); cell(d.postcode); cell(bedLabel(d));
    cell(d.private ? "Private" : "Social"); cell(d.current_rent ? gbp(d.current_rent) : "–", "num");
    cell(d.proposed_rent ? gbp(d.proposed_rent) : "–", "num"); cell(d.determined_rent ? gbp(d.determined_rent) : "–", "num");
    cell(d.post_act ? "New" : "Old"); cell(d.verified === 1 ? "✓ GOV.UK" : d.verified === 0 ? "⚠ differs" : "–");
    cell(d.good_condition_rent ? gbp(d.good_condition_rent) + (d.deduction_pct ? ` −${d.deduction_pct}%` : "") : "–"); });
}

function renderMarket() {
  const M = D.market; if (!M) { $("#mktCard").style.display = "none"; return; }
  $("#mktSub").textContent = `Median days advertised on OpenRent, split at your rent (${gbp(M.threshold, 2)}). Listings that sit longer are ones the market resists.`;
  const t = $("#mktTbl"); t.innerHTML = "";
  const tr = t.createTHead().insertRow(); ["", "Below your rent", "Your rent or more"].forEach(h => { const th = document.createElement("th"); th.textContent = h; tr.appendChild(th); });
  const tb = t.createTBody();
  [["Advertised now", M.advertised], ["Let agreed, last 24 months", M.let]].forEach(([label, g]) => {
    const r = tb.insertRow(); const th = document.createElement("th"); th.textContent = label; r.appendChild(th);
    [[g.median_low, g.n_low], [g.median_high, g.n_high]].forEach(([m, n]) => { const td = r.insertCell();
      const big = document.createElement("div"); big.className = "m"; big.textContent = m == null ? "–" : Math.round(m) + " days";
      const sm = document.createElement("div"); sm.className = "small"; sm.textContent = `${n} listing${n === 1 ? "" : "s"}`; td.append(big, sm); }); });
  $("#mktCuts").textContent = M.cuts.length ? `Price cuts seen since tracking began: ` + M.cuts.map(c => `${c.title.split(",")[1] || c.title} ${gbp(c.from)} → ${gbp(c.to)}`).join("; ") + "."
    : "No price cuts seen yet (tracking began November 2026).";
}

function renderDefects() {
  const X = D.defects || [], t = $("#defTbl"), P = D.pattern; t.innerHTML = "";
  if (P && P.issues) $("#defSub").textContent = `${P.issues} issues logged, ${P.open} still open` +
    (P.median_days_to_resolve != null ? `; resolved ones took a median of ${Math.round(P.median_days_to_resolve)} days` : "") +
    (P.longest_open ? `; longest open: ${P.longest_open.issue.toLowerCase()}, ${P.longest_open.days} days` : "") + ". " +
    Object.entries(P.themes).map(([k, v]) => `${k}: ${v.length} issue${v.length === 1 ? "" : "s"}`).join(" · ") +
    ". Dated photos and reports are what turn a defect into a deduction.";
  if (!X.length) { t.innerHTML = ""; const p = document.createElement("p"); p.className = "small";
    p.textContent = "No evidence yet. Add photos, saved emails and dated notes to evidence/defects/<issue>/ (one folder per problem)."; t.replaceWith(p); return; }
  const tr = t.createTHead().insertRow(); ["Issue", "First reported", "Days open", "Reports", "Agent replies", "Photos"].forEach((h, i) => {
    const th = document.createElement("th"); th.textContent = h; if (i > 1) th.className = "num"; tr.appendChild(th); });
  const tb = t.createTBody();
  X.forEach(x => { const r = tb.insertRow();
    const c = (v, cls) => { const td = r.insertCell(); td.textContent = v ?? "–"; if (cls) td.className = cls; return td; };
    const td = c(x.issue); if (x.latest_description) { const d = document.createElement("div"); d.className = "small"; d.textContent = x.latest_description; td.appendChild(d); }
    c(fmtDate(x.first_report)); c(x.days != null ? x.days + (x.status === "unresolved" ? " (open)" : "") : "–", "num");
    c(x.reports, "num"); c(x.acknowledged, "num"); c(x.photos, "num"); });
}

function renderCollection() {
  const c = D.collection;
  $("#collectionSummary").textContent = `${c.total} listings in the database · Latest completed scheduled crawl ${c.latest ? fmtDate(c.latest) : "unknown"}`;
  $("#collectionCoverage").textContent = `Of ${c.nearby} flats marked let agreed within 2 km in the past year, ${c.area} have a recorded or estimated floor area and ${c.layout} have bedroom layout evidence from a listing, review or photos.`;
  const body = $("#collectionTable tbody");
  c.days.forEach(day => {
    const row = body.insertRow();
    [fmtDate(day.date), day.status, day.warnings, day.failures, day.transitions].forEach(value => {
      row.insertCell().textContent = value;
    });
  });
}

function renderPublic() {
  if (D.title) { $("#title").textContent = D.title; document.title = D.title; }
  if (!D.public) return;
  $("#publicBanner").hidden = false;
  $("#publicText").textContent = `A live worked example for ${D.publicDescription}. The tenant's rent history and the evidence about comparable lets are real; private evidence (repairs, correspondence, photos, names and the exact address) is kept off this public copy.` +
    (D.roundedTo ? ` The tenant's own rents are rounded to the nearest £${D.roundedTo}.` : "") +
    (D.repoUrl ? " The code and a guide to running it for your own flat are on GitHub." : "");
  if (D.repoUrl) { const a = document.createElement("a"); a.href = D.repoUrl; a.textContent = " View the code"; a.rel = "noopener"; $("#publicText").appendChild(a); }
  ["#defCard", "#datesCard"].forEach(id => { const e = $(id); if (e) e.style.display = "none"; });
  const src = document.createElement("p"); src.className = "small";
  src.textContent = "Sources: OpenRent; ONS Price Index of Private Rents and House Price Statistics for Small Areas (Open Government Licence); GOV.UK EPC register; OpenStreetMap stations (© OpenStreetMap contributors); Rent Rise Check and GOV.UK tribunal decisions.";
  $("#publicBanner").appendChild(src);
}

function renderNotes() {
  const n = $("#notes"); n.innerHTML = "";
  [
    `Comparables: 2-bed homes within ${D.radius} km, from OpenRent area searches and the past listings of each landlord found. Collected ${D.generated}.`,
    "\"Let agreed\" rent is the amount recorded on OpenRent after the landlord agrees the rent, ahead of both parties confirming the contract. Live listings show the asking rent.",
    "Excluded: lets with bills included, room or flat shares, short lets, and duplicate listings of the same flat.",
    "Similarity score uses distance, how recent the let is, flat vs house, bathrooms, garden, parking, ground floor and layout. It never uses price.",
    "ONS figures: Price Index of Private Rents" + (D.onsRelease ? ` (release ${D.onsRelease})` : "") + ". Official statistics in development, covering new and existing tenancies, so averages run lower than new-let prices.",
    "Floor areas, where shown, come from the listing or are estimated from EPC certificates on the same street.",
    "\"In today's money\" scales each rent by the ONS Lewisham 2-bed index from the month it was let to the latest ONS month. Lets newer than the ONS data are unchanged.",
    "Tribunal decisions: Section 13 cases published on GOV.UK, figures extracted by Rent Rise Check (rentrisecheck.uk). Social-landlord cases decide an open-market rent the landlord may choose not to charge, so they are shown only in the full list.",
    "Adjustment percentages for layout and features are judgement calls, set in openrent_comps.py (ADJUSTMENTS) and shown line by line so each can be checked.",
  ].forEach(s => { const li = document.createElement("li"); li.textContent = s; n.appendChild(li); });
}

// ---------- wiring ----------
function render() {
  document.querySelectorAll(".seg").forEach(seg => seg.querySelectorAll("button").forEach(b =>
    b.setAttribute("aria-pressed", String(state[seg.dataset.f] === b.dataset.v))));
  const rows = inView().map(c => ({...c, raw: c.price, price: state.adj === "loc" && c.loc ? c.loc : state.adj === "today" && c.today ? c.today : c.price}));
  renderTiles(rows); renderDist(rows); renderTime(rows);
  const ranked = [...rows].sort((a, b) => b.score - a.score || a.price - b.price);
  tableInto("#tbl", ranked.slice(0, 15)); tableInto("#tblAll", ranked);
}
document.querySelectorAll(".seg").forEach(seg => seg.addEventListener("click", e => {
  const b = e.target.closest("button"); if (!b) return; state[seg.dataset.f] = b.dataset.v;
  try { localStorage.setItem("rc-filters", JSON.stringify(state)); } catch (err) {}
  render(); }));
$("#gen").textContent = "updated " + D.generated;
renderPublic(); renderCollection(); renderDates(); renderGrid(); renderSens(); renderTribunal(); renderMarket(); renderDefects(); renderGrowth(); renderBoroughs(); renderNotes(); render();
const redraw = () => { renderTribunal(); renderGrowth(); renderBoroughs(); render(); };
matchMedia("(prefers-color-scheme: dark)").addEventListener("change", redraw);
let lastW = innerWidth; addEventListener("resize", () => { if (Math.abs(innerWidth - lastW) > 40) { lastW = innerWidth; redraw(); } });
</script>
</body>
</html>
"""

if __name__ == "__main__":
    build()
