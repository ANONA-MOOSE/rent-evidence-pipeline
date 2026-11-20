#!/usr/bin/env python3
"""
The public showcase page: a small, high-impact summary built from the same pipeline.

summary() returns only aggregate, rounded or already-public figures:
  - the example flat's rent (rounded) and the expected proposal
  - a market estimate from the adjustment grid (no defect deductions)
  - counts of listings tracked and lets analysed
  - each let's location- and time-adjusted rent, let month and distance band (no titles, links or text)
  - a rolling median of agreed rents, the ONS borough line, and the sensitivity grid
The detail (evidence, adjustments line by line, tribunal cases) stays in the local dashboard.
"""
import statistics
from datetime import date, datetime

import analysis
import openrent_comps as oc

ROUND_TO = 50


def _r(v):
    return round(v / ROUND_TO) * ROUND_TO if v else v


def _q(vals, p):
    v = sorted(vals)
    if not v:
        return None
    i = (len(v) - 1) * p
    lo = int(i)
    return v[lo] + (v[min(lo + 1, len(v) - 1)] - v[lo]) * (i - lo)


def summary():
    con = oc.db()
    rows = oc.load_rows(con)
    hist = [{"from": f, "rent": _r(r)} for f, r in oc.RENT_HISTORY]
    rent = hist[-1]["rent"]

    usable = [r for r in rows if not r["exclude_reason"]]
    lets_all = [r for r in usable if r["status"] == "let_agreed"]
    lets = analysis.lets_in_window((r for r in lets_all if r.get("price_loc")), oc.RADIUS_KM, 24)
    live = [r for r in usable if r["status"] == "available"]

    # Market estimate: similarity-weighted average of the adjustment grid, without the subject's defect
    # deductions, and on the cautious size assumption when comparables' floor areas are unknown.
    g = analysis.grid(rows)
    c = g.get("conclusion") or {}
    est, low, high = c.get("weighted_mean"), c.get("low"), c.get("high")
    sn = c.get("size_note")
    if sn and est:
        f = 1 + sn["pct"] / 100
        est, low, high = sn["weighted"], round(low * f), round(high * f)
    est = round(est / 10) * 10 if est else None

    sens = analysis.sensitivity(rows, rent=rent)
    cells = [{"km": x["km"], "months": x["months"], "n": x["n"], "median": x.get("median"), "gap": x.get("gap")}
             for x in sens["cells"]]

    # Rolling 6-month median and middle half of agreed rents (as let), by month, from 2023.
    by_month = []
    today = date.today()
    y, m = 2023, 1
    while (y, m) <= (today.year, today.month):
        key = f"{y:04d}-{m:02d}"
        lo_y, lo_m = (y, m - 5) if m > 5 else (y - 1, m + 7)
        lo_key = f"{lo_y:04d}-{lo_m:02d}"
        vals = [r["price_pcm"] for r in lets_all if r["let_agreed_date"] and lo_key <= r["let_agreed_date"][:7] <= key
                and r["price_pcm"]]
        if len(vals) >= 8:
            by_month.append({"m": key, "med": round(statistics.median(vals)), "q1": round(_q(vals, .25)),
                             "q3": round(_q(vals, .75)), "n": len(vals)})
        y, m = (y, m + 1) if m < 12 else (y + 1, 1)

    ons = [{"m": p, "v": v} for p, v in con.execute(
        "SELECT period, price_2bed FROM ons_rents WHERE area='Lewisham' AND period >= '2023-01' ORDER BY period")]

    points = [{"m": r["let_agreed_date"][:7], "v": round(r["price_loc"]), "km": round(r["distance_km"] or 0, 1)}
              for r in lets]
    points.sort(key=lambda p: p["v"])
    cheaper = sum(p["v"] < rent for p in points)
    # Headline market rate: median of the comparable agreed lets shown (same figure the page headline uses).
    market_rate = round(statistics.median(r["price_loc"] for r in lets) / 10) * 10 if points else None

    return {
        "generated": datetime.now().strftime("%-d %b %Y"),
        "description": oc.PUBLIC_DESCRIPTION,
        "repo": oc.PUBLIC_REPO_URL,
        "rent": rent, "history": hist,
        "proposed": list(oc.PROPOSED_RENT_RANGE) if oc.PROPOSED_RENT_RANGE else None,
        "market": {"est": est, "low": low, "high": high},
        "gap": rent - market_rate if market_rate else None, "marketRate": market_rate,
        "counts": {"tracked": len(rows), "live": len(live), "lets": len(lets_all), "recent": len(lets),
                   "cheaper": cheaper, "radius": oc.RADIUS_KM},
        "points": points, "trend": by_month, "ons": ons,
        "sens": {"cells": cells, "above": sens["above_in"], "of": sens["of"]},
        "roundedTo": ROUND_TO,
    }


if __name__ == "__main__":
    import json
    s = summary()
    print(json.dumps({k: v for k, v in s.items() if k not in ("points", "trend", "ons")}, indent=1))
    print(len(s["points"]), "points;", len(s["trend"]), "trend months;", len(s["ons"]), "ONS months")


def render():
    import json
    d = summary()
    import html as _h
    url = (oc.PUBLIC_SITE_URL or "").rstrip("/") + "/"
    title = "Rent Control JUDO: using the force of the market, against itself"
    gap = d["gap"]
    desc = "A live, open-source rent evidence dashboard."
    if gap is not None:
        comparison = (f"£{abs(gap):,} a month {'above' if gap > 0 else 'below'}"
                      if gap else "in line with")
        desc = f"This example rent is {comparison} the local median of {len(d['points'])} comparable agreed lets."
    page = TEMPLATE.replace("__URL__", _h.escape(url)).replace("__TITLE__", _h.escape(title)).replace("__DESC__", _h.escape(desc))
    return page.replace("/*__DATA__*/null", json.dumps(d).replace("</", "<\\/"))


TEMPLATE = r"""<!doctype html>
<html lang="en-GB">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Rent Control JUDO</title>
<meta name="description" content="__DESC__">
<meta name="theme-color" content="#0b0c0e">
<link rel="icon" type="image/png" sizes="32x32" href="favicon-32.png">
<link rel="icon" type="image/png" sizes="192x192" href="icon-192.png">
<link rel="apple-touch-icon" href="apple-touch-icon.png">
<meta property="og:type" content="website">
<meta property="og:site_name" content="Rent Control JUDO">
<meta property="og:title" content="__TITLE__">
<meta property="og:description" content="__DESC__">
<meta property="og:url" content="__URL__">
<meta property="og:image" content="__URL__og.png">
<meta property="og:image:width" content="1200">
<meta property="og:image:height" content="630">
<meta property="og:image:alt" content="Rent Control JUDO: a cat logo over a glowing rent chart, with the words 'Using the force of the market, against itself'">
<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:image" content="__URL__og.png">
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
<style>
:root{
  color-scheme: light;
  --page:#f6f6f3; --surface:#ffffff; --raised:#fbfbf9; --ink:#0b0b0b; --ink2:#52514e; --muted:#676965;
  --line:#e6e5df; --grid:#ecebe5; --axis:#c3c2b7;
  --mkt:#2a78d6; --mkt-wash:rgba(42,120,214,.14); --you:#eb6834; --you-wash:rgba(235,104,52,.14); --tile:#c4552a; --prop:#d03b3b; --ok:#1baf7a;
  --glow: 0 1px 2px rgba(0,0,0,.04), 0 8px 24px rgba(0,0,0,.05);
}
@media (prefers-color-scheme: dark){ :root:not([data-theme="light"]){
  color-scheme: dark;
  --page:#0b0c0e; --surface:#131417; --raised:#17191c; --ink:#f2f1ee; --ink2:#bdbcb6; --muted:#85847e;
  --line:#24262a; --grid:#1f2124; --axis:#34363a;
  --mkt:#3987e5; --mkt-wash:rgba(57,135,229,.18); --you:#e2652f; --you-wash:rgba(217,89,38,.2); --tile:#cf5a26; --prop:#e66767; --ok:#199e70;
  --glow: 0 1px 0 rgba(255,255,255,.03) inset, 0 12px 32px rgba(0,0,0,.35);
}}
:root[data-theme="dark"]{
  color-scheme: dark;
  --page:#0b0c0e; --surface:#131417; --raised:#17191c; --ink:#f2f1ee; --ink2:#bdbcb6; --muted:#85847e;
  --line:#24262a; --grid:#1f2124; --axis:#34363a;
  --mkt:#3987e5; --mkt-wash:rgba(57,135,229,.18); --you:#e2652f; --you-wash:rgba(217,89,38,.2); --tile:#cf5a26; --prop:#e66767; --ok:#199e70;
  --glow: 0 1px 0 rgba(255,255,255,.03) inset, 0 12px 32px rgba(0,0,0,.35);
}
*{box-sizing:border-box}
body{margin:0;background:var(--page);color:var(--ink);font:15px/1.45 Inter,system-ui,-apple-system,"Segoe UI",sans-serif;-webkit-font-smoothing:antialiased;font-feature-settings:"tnum" 1,"cv11" 1}
main{max-width:1180px;margin:0 auto;padding:20px 16px 56px}
nav{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:28px}
.brand{display:flex;align-items:center;gap:10px;font-weight:700;letter-spacing:-.01em}
.mark{width:30px;height:30px;border-radius:8px;display:block;box-shadow:0 0 0 1px var(--line)}
.live{display:inline-flex;align-items:center;gap:8px;font-size:12.5px;color:var(--ink2);border:1px solid var(--line);background:var(--surface);padding:6px 10px;border-radius:999px}
.dot{width:7px;height:7px;border-radius:50%;background:var(--ok);box-shadow:0 0 0 0 var(--ok);animation:pulse 2.4s infinite}
@keyframes pulse{0%{box-shadow:0 0 0 0 rgba(27,175,122,.55)}70%{box-shadow:0 0 0 8px rgba(27,175,122,0)}100%{box-shadow:0 0 0 0 rgba(27,175,122,0)}}
@media (prefers-reduced-motion: reduce){.dot{animation:none}}
.grid{display:grid;gap:14px;grid-template-columns:repeat(12,1fr)}
.card{background:var(--surface);border:1px solid var(--line);border-radius:18px;padding:20px;box-shadow:var(--glow);min-width:0}
.s12{grid-column:span 12}.s7{grid-column:span 7}.s5{grid-column:span 5}
@media (max-width:900px){.s7,.s5{grid-column:span 12}}
.hero{display:grid;grid-template-columns:minmax(0,5fr) minmax(0,6fr);gap:32px;align-items:center;padding:32px}
.eyebrow{font-size:12px;letter-spacing:.08em;text-transform:uppercase;color:var(--muted);font-weight:650}
h1{margin:14px 0 16px;font-size:clamp(22px,2.1vw,30px);line-height:1.15;letter-spacing:-.035em}
#gap{display:block;font-size:clamp(56px,7vw,88px);color:var(--you);line-height:1;letter-spacing:-.06em}
.lede{color:var(--ink2);font-size:15px;margin:0}.lede b{color:var(--ink)}
.sample::before{content:" · "}
@media (max-width:620px){.sample{display:block}.sample::before{content:none}}
.proposal{font-size:13px;color:var(--ink2);margin:12px 0 0}
.proposal::before{content:"";display:inline-block;width:10px;height:10px;background:var(--prop);border-radius:50%;margin-right:7px;vertical-align:-1px}
.s4{grid-column:span 4}
@media (max-width:620px){.kpi{flex-direction:column;align-items:flex-start;gap:6px;padding:14px 12px}.kpi .v{font-size:28px}.kpi .l{font-size:12px}}
.kpi{display:flex;align-items:baseline;gap:12px;padding:18px 20px}
.kpi .v{font-size:clamp(30px,3.4vw,40px);font-weight:750;letter-spacing:-.04em;line-height:1}
.kpi .l{font-size:13.5px;color:var(--ink2);line-height:1.3}.kpi .l b{display:block;color:var(--ink);font-weight:600}
h2{font-size:clamp(18px,2vw,23px);font-weight:650;line-height:1.25;letter-spacing:-.028em;margin:0}
.cap{font-size:13px;color:var(--muted);margin:6px 0 14px}
#share{color:var(--mkt);font-size:1.6em;letter-spacing:-.05em}
@media (max-width:900px){.hero{grid-template-columns:1fr;gap:12px;padding:22px}}
@media (max-width:520px){main{padding:16px 12px 36px}nav{margin-bottom:16px}.card{padding:16px;border-radius:14px}.hero{gap:8px}.grid{gap:12px}}
.legend{display:flex;flex-wrap:wrap;gap:4px 14px;font-size:12px;color:var(--ink2);margin:0 0 6px}
.key{display:inline-flex;align-items:center;gap:6px}.key i{display:inline-block;width:14px;height:3px;border-radius:2px}
.key i.d{width:8px;height:8px;border-radius:50%}.key i.b{width:14px;height:10px;border-radius:3px}
svg{display:block;width:100%;height:auto;overflow:visible}
svg text{fill:var(--muted);font:11.5px Inter,system-ui,sans-serif;font-variant-numeric:tabular-nums}
svg .lbl{fill:var(--ink2);font-weight:500}svg .strong{fill:var(--ink);font-weight:650}
svg .lbl,svg .strong{paint-order:stroke;stroke:var(--surface);stroke-width:4px;stroke-linejoin:round}
.gl{stroke:var(--grid);stroke-width:1}.ax{stroke:var(--axis);stroke-width:1}
.hit{fill:transparent;cursor:default}
#tip{position:fixed;pointer-events:none;z-index:9;background:var(--raised);color:var(--ink);border:1px solid var(--line);border-radius:10px;padding:8px 10px;font-size:12.5px;box-shadow:var(--glow);opacity:0;transition:opacity .08s;max-width:240px}
#tip b{font-size:14px;display:block}
.cells{width:100%;table-layout:fixed;border-spacing:4px;font-variant-numeric:tabular-nums}
.cells th{font-size:12px;font-weight:550;color:var(--ink2);text-align:center}
.cells th:first-child{width:48px;text-align:left;white-space:nowrap}
.cells td{border-radius:10px;padding:10px 2px;text-align:center;font-weight:700;font-size:16px;color:#fff;letter-spacing:-.01em}
.cells td span{display:block;font-size:11px;font-weight:500;color:rgba(255,255,255,.82);margin-top:1px}
.cells td.empty{background:var(--raised);color:var(--muted)}
.cells .reference{box-shadow:0 0 0 2px var(--surface),0 0 0 4px var(--mkt)}
.reference-note{font-size:12px;color:var(--ink2);margin:8px 4px 0}
.steps{display:flex;flex-wrap:wrap;gap:6px;margin:6px 0 14px}
.step{font-size:12.5px;border:1px solid var(--line);background:var(--raised);border-radius:999px;padding:6px 10px;color:var(--ink2)}
.step b{color:var(--ink);font-weight:650;margin-right:4px}
.btn{display:inline-flex;align-items:center;gap:8px;text-decoration:none;font-weight:600;font-size:13.5px;color:var(--surface);background:var(--ink);padding:10px 14px;border-radius:10px}
.btn:focus-visible{outline:2px solid var(--mkt);outline-offset:2px}
.brand,.live{white-space:nowrap}
@media (max-width:520px){.live .long{display:none}}
footer{margin-top:22px;font-size:12px;color:var(--muted);line-height:1.6}
footer a{color:var(--ink2)}
/* ---------- Hero: Rent Control JUDO ---------- */
html{scroll-behavior:smooth}
@media (prefers-reduced-motion: reduce){html{scroll-behavior:auto}}
.jd{position:relative;min-height:100svh;overflow:hidden;overflow:clip;color:#f4f3ef;background:#141813;isolation:isolate;display:flex;flex-direction:column}
.jd-bg{position:absolute;inset:-12%;z-index:-3;filter:blur(46px) saturate(1.15);
  background:
    radial-gradient(40% 48% at 18% 32%, rgba(132,170,56,1), transparent 70%),
    radial-gradient(30% 34% at 34% 16%, rgba(190,214,96,.7), transparent 70%),
    radial-gradient(36% 42% at 70% 58%, rgba(236,134,42,.95), transparent 70%),
    radial-gradient(28% 32% at 88% 84%, rgba(224,110,38,.7), transparent 70%),
    radial-gradient(52% 48% at 52% 96%, rgba(62,76,90,.95), transparent 72%),
    radial-gradient(34% 34% at 92% 12%, rgba(57,135,229,.30), transparent 70%),
    linear-gradient(165deg,#1e2717 0%,#2a2f27 46%,#222a33 100%);
  animation:jdDrift 28s ease-in-out infinite alternate}
@keyframes jdDrift{0%{transform:translate3d(0,0,0) scale(1)}50%{transform:translate3d(-2%,1.5%,0) scale(1.04)}100%{transform:translate3d(1.5%,-1%,0) scale(1.02)}}
.jd-mesh{position:absolute;inset:0;z-index:-2;opacity:.55;pointer-events:none;
  background-image:linear-gradient(rgba(255,255,255,.035) 1px,transparent 1px),linear-gradient(90deg,rgba(255,255,255,.035) 1px,transparent 1px);
  background-size:3px 3px}
.jd-shade{position:absolute;inset:0;z-index:-1;pointer-events:none;
  background:linear-gradient(90deg,rgba(10,12,10,.5) 0%,rgba(10,12,10,.18) 55%,rgba(10,12,10,.05) 100%),
             linear-gradient(180deg,rgba(0,0,0,.15) 0%,transparent 30%,transparent 70%,var(--page) 100%)}
.jd-nav{display:flex;align-items:center;justify-content:flex-end;gap:16px;padding:22px clamp(16px,3vw,40px)}
.jd-gh{display:inline-flex;align-items:center;gap:9px;color:#fff;text-decoration:none;font-weight:600;font-size:15px;padding:10px 18px 10px 14px;border-radius:999px;border:1px solid rgba(255,255,255,.28);background:rgba(255,255,255,.08);backdrop-filter:blur(8px);transition:background .2s}
.jd-gh:hover{background:rgba(255,255,255,.18)}
.jd-pill{display:inline-flex;align-items:center;gap:8px;background:#fff;color:#0b0b0b;text-decoration:none;font-weight:650;font-size:16px;padding:15px 28px;border-radius:999px;white-space:nowrap;box-shadow:0 10px 30px rgba(0,0,0,.25);transition:transform .2s}
.jd-pill:hover{transform:translateY(-2px)}
.jd-pill:focus-visible,.jd-gh:focus-visible,.jd-down:focus-visible{outline:2px solid #fff;outline-offset:3px}
.jd-body{flex:1;display:grid;grid-template-columns:minmax(0,1.15fr) minmax(0,.85fr);align-items:center;gap:clamp(24px,4vw,64px);
  padding:clamp(24px,6vh,72px) clamp(16px,3vw,40px) clamp(56px,10vh,110px);max-width:1360px;width:100%;margin:0 auto;box-sizing:border-box}
.jd-title{font-weight:800;font-size:clamp(52px,8.4vw,128px);line-height:.92;letter-spacing:-.045em;margin:0;text-wrap:balance}
.jd-title .o{color:#ff7d3b;display:block}
.jd-tag{font-size:clamp(20px,2.2vw,30px);line-height:1.25;letter-spacing:-.02em;font-weight:500;color:rgba(244,243,239,.9);margin:22px 0 0;max-width:22ch}
.jd-ctas{display:flex;margin-top:40px}
.jd-art{position:relative;width:100%;max-width:520px;justify-self:end;margin-top:14%}
.jd-card{position:relative;aspect-ratio:1/1;width:100%;border-radius:28px;
  background:rgba(12,14,16,.58);border:1px solid rgba(255,255,255,.12);backdrop-filter:blur(14px);
  box-shadow:0 30px 80px rgba(0,0,0,.45);overflow:hidden}
.jd-card svg{position:absolute;inset:0;width:100%;height:100%}
.jd-cat{position:absolute;left:1%;top:-27%;width:50%;transform:rotate(-8deg);pointer-events:none;
  filter:drop-shadow(3px 0 0 #fff) drop-shadow(-3px 0 0 #fff) drop-shadow(0 3px 0 #fff) drop-shadow(0 -3px 0 #fff) drop-shadow(0 18px 28px rgba(0,0,0,.55))}
.jd-down{position:absolute;left:50%;bottom:20px;transform:translateX(-50%);display:flex;flex-direction:column;align-items:center;gap:4px;color:rgba(244,243,239,.7);text-decoration:none;font-size:11px;font-weight:600;letter-spacing:.16em;text-transform:uppercase}
.jd-down svg{animation:jdBob 2.2s ease-in-out infinite}
@keyframes jdBob{0%,100%{transform:translateY(0)}50%{transform:translateY(5px)}}
.jd-draw{stroke-dasharray:1;stroke-dashoffset:1;animation:jdDraw 1.6s .2s cubic-bezier(.4,0,.2,1) forwards}
@keyframes jdDraw{to{stroke-dashoffset:0}}
.jd-fade,.jd-late,.jd-lab{opacity:0;animation:jdIn .8s forwards}.jd-fade{animation-delay:.5s}.jd-late,.jd-lab{animation-delay:1.6s}
@keyframes jdIn{to{opacity:1}}
.jd-pulse{transform-box:fill-box;transform-origin:center;opacity:0;animation:jdPulse 2.4s 2.2s ease-out infinite}
@keyframes jdPulse{0%{opacity:.8;transform:scale(1)}100%{opacity:0;transform:scale(2.6)}}
@media (max-width:900px){.jd-body{grid-template-columns:1fr}.jd-art{justify-self:center;max-width:440px;margin-top:20%}}
@media (max-width:520px){.jd-art{max-width:none;margin-top:26%}.jd-cat{left:2%;top:-24%;width:46%}.jd-gh span{display:none}.jd-gh{padding:10px}.jd-down{display:none}}
@media (prefers-reduced-motion: reduce){.jd-bg,.jd-down svg,.jd-pulse{animation:none}.jd-draw,.jd-fade,.jd-late,.jd-lab{animation:none;opacity:1;stroke-dashoffset:0}}
.dash-head{display:flex;align-items:center;justify-content:space-between;gap:12px;margin:8px 0 18px;scroll-margin-top:16px}
.dash-head h2{font-size:clamp(22px,2.6vw,30px);letter-spacing:-.035em}
.reveal{opacity:0;transform:translateY(18px);transition:opacity .7s ease,transform .7s cubic-bezier(.2,.7,.2,1)}
.reveal.in{opacity:1;transform:none}
@media (prefers-reduced-motion: reduce){.reveal{opacity:1;transform:none;transition:none}}
@media print{.jd{display:none}.reveal{opacity:1;transform:none}}
</style>
</head>
<body>
<header class="jd" aria-label="Rent Control JUDO">
  <div class="jd-bg" aria-hidden="true"></div><div class="jd-mesh" aria-hidden="true"></div><div class="jd-shade" aria-hidden="true"></div>
  <nav class="jd-nav">
    <a class="jd-gh" id="jd-gh" href="#" aria-label="View the code on GitHub"><svg viewBox="0 0 16 16" width="20" height="20" fill="currentColor" aria-hidden="true"><path d="M8 0c4.42 0 8 3.58 8 8a8.013 8.013 0 0 1-5.45 7.59c-.4.08-.55-.17-.55-.38 0-.27.01-1.13.01-2.2 0-.75-.25-1.23-.54-1.48 1.78-.2 3.65-.88 3.65-3.95 0-.88-.31-1.59-.82-2.15.08-.2.36-1.02-.08-2.12 0 0-.67-.22-2.2.82-.64-.18-1.32-.27-2-.27-.68 0-1.36.09-2 .27-1.53-1.03-2.2-.82-2.2-.82-.44 1.1-.16 1.92-.08 2.12-.51.56-.82 1.28-.82 2.15 0 3.06 1.86 3.75 3.64 3.95-.23.2-.44.55-.51 1.07-.46.21-1.61.55-2.33-.66-.15-.24-.6-.83-1.23-.82-.67.01-.27.38.01.53.34.19.73.9.82 1.13.16.45.68 1.31 2.69.94 0 .67.01 1.3.01 1.49 0 .21-.15.45-.55.38A7.995 7.995 0 0 1 0 8c0-4.42 3.58-8 8-8Z"/></svg><span>GitHub</span></a>
  </nav>
  <div class="jd-body">
    <div>
      <h1 class="jd-title">Rent Control <span class="o">JUDO</span></h1>
      <p class="jd-tag">Using the force of the market, against itself.</p>
      <div class="jd-ctas"><a class="jd-pill" href="#evidence">See the evidence ↓</a></div>
    </div>
    <div class="jd-art" aria-hidden="true">
      <div class="jd-card"><svg id="jd-chart" viewBox="0 0 400 400"></svg></div>
      <img class="jd-cat" src="logo-512.png" alt="">
    </div>
  </div>
  <a class="jd-down" href="#evidence" aria-label="Scroll to the evidence"><span>Scroll</span>
    <svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M6 9l6 6 6-6"/></svg></a>
</header>
<main>
  <div class="dash-head" id="evidence">
    <h2>The evidence</h2>
    <div class="live"><span class="dot" aria-hidden="true"></span>Live<span class="long"> · updated</span>&nbsp;<span id="gen"></span></div>
  </div>

  <div class="grid">
    <section class="card s12 hero" aria-label="Headline">
      <div>
        <div class="eyebrow">Example tenancy · <span id="area"></span></div>
        <h1><span id="gap"></span><span id="comparison"></span></h1>
        <p class="lede" id="lede"></p>
        <p class="proposal" id="proposal" hidden></p>
      </div>
      <div id="c-hero"></div>
    </section>

    <section class="card s4 kpi"><div class="v" id="k1"></div><div class="l"><b>live listings</b>tracked daily within <span id="k1r"></span> km</div></section>
    <section class="card s4 kpi"><div class="v" id="k2"></div><div class="l"><b>agreed lets</b>analysed in total</div></section>
    <section class="card s4 kpi"><div class="v" id="k3"></div><div class="l"><b>recent comparables</b>let in the last 24 months</div></section>

    <section class="card s7" aria-labelledby="h-dist">
      <h2 id="h-dist"><span id="share"></span><span id="share-label"></span></h2>
      <p class="cap">Comparable two-bedroom lets from the last 24 months, adjusted for location and date.</p>
      <div id="c-dist"></div>
    </section>

    <section class="card s5" aria-labelledby="h-sens">
      <h2 id="h-sens">Above the median in every view</h2>
      <p class="cap">£ a month above the median, by distance and period.</p>
      <table class="cells" id="cells" aria-labelledby="h-sens"></table>
      <p class="reference-note" id="reference-note" hidden></p>
    </section>

    <section class="card s12" aria-labelledby="h-trend">
      <h2 id="h-trend">Agreed rents over time</h2>
      <p class="cap">6-month rolling median with the middle half shaded; official borough average; this tenancy's rent.</p>
      <div class="legend" id="lg-trend"></div><div id="c-trend"></div>
    </section>

    <section class="card s7" aria-labelledby="h-km">
      <h2 id="h-km">Near or far, cheaper</h2>
      <p class="cap">Each dot is one agreed let (last 24 months), by distance from the example flat.</p>
      <div class="legend" id="lg-km"></div><div id="c-km"></div>
    </section>

    <section class="card s5" aria-labelledby="h-how">
      <h2 id="h-how">How it works</h2>
      <p class="cap">An open-source pipeline runs every morning. Private evidence never leaves the tenant's computer.</p>
      <div class="steps" id="steps"></div>
      <a class="btn" id="repo" href="#" rel="noopener">View the code on GitHub →</a>
    </section>
  </div>

  <footer id="foot"></footer>
</main>
<div id="tip" role="status" aria-live="polite"></div>
<script>
const D = /*__DATA__*/null;
(() => { const t = new URLSearchParams(location.search).get("theme"); if (t === "dark" || t === "light") document.documentElement.dataset.theme = t; })();
const $ = s => document.querySelector(s);
const gbp = v => v == null ? "–" : "£" + Math.round(v).toLocaleString("en-GB");
const NS = "http://www.w3.org/2000/svg";
const css = v => getComputedStyle(document.documentElement).getPropertyValue(v).trim();
function el(t, a = {}, p) { const e = document.createElementNS(NS, t); for (const k in a) e.setAttribute(k, a[k]); if (p) p.appendChild(e); return e; }
function tx(p, x, y, s, cls, anchor = "start") { const t = el("text", {x, y, "text-anchor": anchor}, p); if (cls) t.setAttribute("class", cls); t.textContent = s; return t; }
function ticks(lo, hi, n) { const span = hi - lo, s0 = span / n, mag = Math.pow(10, Math.floor(Math.log10(s0)));
  const st = [1, 2, 2.5, 5, 10].map(m => m * mag).find(s => span / s <= n) || mag * 10; const o = [];
  for (let v = Math.ceil(lo / st) * st; v <= hi + 1e-9; v += st) o.push(v); return o; }
const median = a => { const v = [...a].sort((x, y) => x - y), m = v.length >> 1; return v.length % 2 ? v[m] : (v[m - 1] + v[m]) / 2; };
const quant = (a, p) => { const v = [...a].sort((x, y) => x - y), i = (v.length - 1) * p, l = Math.floor(i); return v[l] + (v[Math.min(l + 1, v.length - 1)] - v[l]) * (i - l); };
function legend(id, items) { const L = $(id); L.innerHTML = ""; items.forEach(([t, c, k]) => { const s = document.createElement("span"); s.className = "key";
  const i = document.createElement("i"); if (k) i.className = k; i.style.background = c; s.append(i, t); L.appendChild(s); }); }
const tip = $("#tip");
function show(e, lines) { tip.innerHTML = ""; lines.forEach((l, i) => { const d = document.createElement(i ? "div" : "b"); d.textContent = l; tip.appendChild(d); });
  tip.style.opacity = 1; const w = tip.offsetWidth, h = tip.offsetHeight;
  tip.style.left = Math.min(innerWidth - w - 8, Math.max(8, e.clientX + 14)) + "px"; tip.style.top = Math.max(8, e.clientY - h - 12) + "px"; }
const hide = () => tip.style.opacity = 0;
const fmtM = m => new Date(m + "-01").toLocaleDateString("en-GB", {month: "short", year: "numeric"});

const P = D.points.map(p => p.v), MKT = D.marketRate, RENT = D.rent;
const GAP = D.gap;
const signedGap = gap => (gap > 0 ? "+" : gap < 0 ? "−" : "") + gbp(Math.abs(gap));
const letCount = n => n + (n === 1 ? " let" : " lets");

function header() {
  $("#gen").textContent = D.generated;
  $("#area").textContent = "SE13, south-east London";
  $("#gap").textContent = GAP == null ? "" : gbp(Math.abs(GAP));
  $("#comparison").textContent = GAP == null ? "Local rent comparison" : GAP === 0 ? "difference from the local median"
    : ` a month ${GAP > 0 ? "above" : "below"} the local median`;
  const current = document.createElement("b");
  current.textContent = gbp(RENT);
  $("#lede").replaceChildren("Current rent ", current);
  if (P.length) {
    const med = document.createElement("b");
    med.textContent = gbp(MKT);
    const sample = document.createElement("span");
    sample.className = "sample";
    sample.append(`Median of ${P.length} comparable agreed ${P.length === 1 ? "let" : "lets"} `, med);
    $("#lede").append(" ", sample);
  }
  if (D.proposed) {
    $("#proposal").hidden = false;
    $("#proposal").textContent = `Expected proposal ${D.proposed.map(gbp).join("–")} a month`;
  }
  $("#k1").textContent = D.counts.live; $("#k1r").textContent = D.counts.radius;
  $("#k2").textContent = D.counts.lets; $("#k3").textContent = P.length;
  $("#share").textContent = P.length ? Math.round(100 * D.counts.cheaper / P.length) + "%" : "";
  $("#share-label").textContent = P.length ? " of recent agreed lets were cheaper" : "No recent comparable agreed lets";
  const steps = [["Collect", "listings & agreed lets"], ["Enrich", "ONS, EPC, stations"], ["Clean", "no cherry-picking"], ["Adjust", "time, place, building"], ["Test", "12 ways"], ["Publish", "privacy-checked"]];
  const st = $("#steps"); st.innerHTML = ""; steps.forEach(([a, b], i) => { const d = document.createElement("span"); d.className = "step";
    const x = document.createElement("b"); x.textContent = `${i + 1}. ${a}`; d.append(x, b); st.appendChild(d); });
  if (D.repo) $("#repo").href = D.repo; else $("#repo").style.display = "none";
  $("#foot").innerHTML = "";
  $("#foot").append(`Example: ${D.description}. Tenant's rents rounded to £${D.roundedTo}. Market rate = median of ${D.points.length} comparable agreed lets, adjusted to this neighbourhood (ONS small-area prices) and to today (ONS rent index); the cautious similar-flat estimate is ${gbp(D.market.est)}. Sources: OpenRent; ONS (Open Government Licence); GOV.UK EPC register; © OpenStreetMap contributors. Not legal advice. `);
}

function heroBar() {
  const box = $("#c-hero"); box.innerHTML = "";
  if (!P.length) return;
  const Q1 = quant(P, .25), Q3 = quant(P, .75);
  const W = Math.max(260, box.clientWidth), H = 158, m = {l: 22, r: 22};
  const vals = [Q1, Q3, RENT, MKT, ...(D.proposed || [])];
  const lo = Math.floor((Math.min(...vals) - 100) / 100) * 100, hi = Math.ceil((Math.max(...vals) + 100) / 100) * 100;
  const x = v => m.l + (v - lo) / (hi - lo) * (W - m.l - m.r);
  const svg = el("svg", {viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label":
    `Current rent ${gbp(RENT)}, local median ${gbp(MKT)}. Middle 50% of agreed rents ${gbp(Q1)} to ${gbp(Q3)}.`}, box);
  const y = 48, h = 24;
  el("rect", {x: m.l, y, width: W - m.l - m.r, height: h, rx: 8, fill: "var(--grid)"}, svg);
  el("rect", {x: x(Q1), y, width: x(Q3) - x(Q1), height: h, rx: 8, fill: "var(--mkt-wash)"}, svg);
  // gap bracket
  el("rect", {x: Math.min(x(MKT), x(RENT)), y: y + h / 2 - 3, width: Math.abs(x(RENT) - x(MKT)), height: 6, rx: 3, fill: "var(--you)", opacity: .7}, svg);
  const pin = (v, c, label, top) => { el("line", {x1: x(v), x2: x(v), y1: top ? y - 16 : y, y2: top ? y + h : y + h + 16, stroke: c, "stroke-width": 3, "stroke-linecap": "round"}, svg);
    el("circle", {cx: x(v), cy: y + h / 2, r: 9, fill: c, stroke: "var(--surface)", "stroke-width": 3}, svg);
    tx(svg, Math.max(57, Math.min(W - 57, x(v))), top ? y - 24 : y + h + 29, label, "strong", "middle"); };
  pin(MKT, "var(--mkt)", `Median ${gbp(MKT)}`, false);
  pin(RENT, "var(--you)", `Current rent ${gbp(RENT)}`, true);
  if (D.proposed) {
    const pm = (D.proposed[0] + D.proposed[1]) / 2;
    if (D.proposed[1] > D.proposed[0]) el("rect", {x: x(D.proposed[0]), y: y + h / 2 - 3, width: x(D.proposed[1]) - x(D.proposed[0]), height: 6, rx: 3, fill: "var(--prop)", opacity: .35}, svg);
    pin(pm, "var(--prop)", W < 520 ? "Proposal" : `Proposal ${D.proposed.map(gbp).join("–")}`, false);
  }
  const bracketY = 116;
  el("path", {d: `M${x(Q1)} ${bracketY - 4}v4H${x(Q3)}v-4`, fill: "none", stroke: "var(--mkt)"}, svg);
  const centre = Math.max(78, Math.min(W - 78, (x(Q1) + x(Q3)) / 2));
  tx(svg, centre, bracketY + 15, "Middle 50% of agreed rents", "lbl", "middle");
  ticks(lo, hi, W < 520 ? 4 : 6).forEach(t => tx(svg, x(t), H - 2, gbp(t), null, "middle"));
}

function kde(vals, lo, hi, n = 120) {
  const sd = Math.sqrt(vals.reduce((s, v) => s + (v - median(vals)) ** 2, 0) / vals.length);
  const iqr = quant(vals, .75) - quant(vals, .25), bw = Math.max(1, 0.9 * Math.min(sd, iqr / 1.34) * Math.pow(vals.length, -0.2));
  const out = []; for (let i = 0; i <= n; i++) { const x = lo + (hi - lo) * i / n;
    out.push([x, vals.reduce((s, v) => s + Math.exp(-0.5 * ((x - v) / bw) ** 2), 0) / (vals.length * bw * Math.sqrt(2 * Math.PI))]); }
  return out; }

function dist() {
  const box = $("#c-dist"); box.innerHTML = "";
  if (!P.length) return;
  const W = Math.max(300, box.clientWidth), H = 250, m = {l: 8, r: 8, t: 30, b: 28};
  const lo = Math.floor((Math.min(...P, RENT) - 100) / 100) * 100, hi = Math.ceil((Math.max(...P, RENT) + 100) / 100) * 100;
  const x = v => m.l + (v - lo) / (hi - lo) * (W - m.l - m.r);
  const K = kde(P, lo, hi), ymax = Math.max(...K.map(k => k[1]));
  const base = H - m.b - 26, y = d => base - d / ymax * (base - m.t);
  const svg = el("svg", {viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": "Distribution of comparable agreed rents with this rent marked"}, box);
  const g = el("defs", {}, svg); const lg = el("linearGradient", {id: "kd", x1: 0, x2: 0, y1: 0, y2: 1}, g);
  el("stop", {offset: "0%", "stop-color": css("--mkt"), "stop-opacity": .45}, lg); el("stop", {offset: "100%", "stop-color": css("--mkt"), "stop-opacity": .04}, lg);
  el("path", {d: `M${x(K[0][0])},${base}` + K.map(k => `L${x(k[0])},${y(k[1])}`).join("") + `L${x(K[K.length - 1][0])},${base}Z`, fill: "url(#kd)"}, svg);
  el("path", {d: K.map((k, i) => (i ? "L" : "M") + x(k[0]) + "," + y(k[1])).join(""), fill: "none", stroke: "var(--mkt)", "stroke-width": 2}, svg);
  el("line", {x1: m.l, x2: W - m.r, y1: base, y2: base, class: "ax"}, svg);
  // rug of individual lets
  D.points.forEach(p => { el("line", {x1: x(p.v), x2: x(p.v), y1: base + 5, y2: base + 21, stroke: "var(--mkt)", "stroke-width": 1.5, "stroke-opacity": .55, "stroke-linecap": "round"}, svg);
    const h = el("rect", {x: x(p.v) - 4, y: base + 4, width: 8, height: 16, class: "hit"}, svg);
    h.addEventListener("pointermove", e => show(e, [gbp(p.v), `let ${fmtM(p.m)} · ${p.km} km away`, "adjusted to this location & today"]));
    h.addEventListener("pointerleave", hide); });
  const vline = (v, c, label, wgt, side) => { el("line", {x1: x(v), x2: x(v), y1: m.t - 6, y2: base, stroke: c, "stroke-width": wgt}, svg);
    tx(svg, x(v) + (side === "end" ? -6 : 6), m.t - 2, label, "strong", side); };
  vline(MKT, "var(--mkt)", `Median ${gbp(MKT)}`, 2, RENT >= MKT ? "end" : "start");
  vline(RENT, "var(--you)", `This rent ${gbp(RENT)}`, 3, RENT >= MKT ? "start" : "end");
  ticks(lo, hi, W < 520 ? 4 : 7).forEach(t => tx(svg, x(t), H - 4, gbp(t), null, "middle"));
}

function cells() {
  const C = D.sens.cells;
  const gaps = C.map(c => c.gap).filter(Number.isFinite);
  const host = $("#cells");
  host.replaceChildren();
  const heading = $("#h-sens");
  if (gaps.length) {
    const above = gaps.filter(g => g > 0).length;
    heading.textContent = `Above the median in ${above} of ${gaps.length} views`;
  }
  const gmax = Math.max(1, ...gaps.map(Math.abs));
  const months = [...new Set(C.map(c => c.months))], kms = [...new Set(C.map(c => c.km))];
  function addHeader(row, label, scope) {
    const th = document.createElement("th");
    th.scope = scope;
    th.textContent = label;
    row.appendChild(th);
  }
  const top = host.createTHead().insertRow();
  addHeader(top, "Within", "col");
  months.forEach(mo => addHeader(top, `${mo} mo`, "col"));
  const body = host.createTBody();
  kms.forEach(km => {
    const row = body.insertRow();
    addHeader(row, `${km} km`, "row");
    months.forEach(mo => {
      const c = C.find(cell => cell.km === km && cell.months === mo);
      const td = row.insertCell();
      if (!c || !c.n) { td.textContent = "–"; td.className = "empty"; return; }
      td.style.background = c.gap > 0 ? `color-mix(in srgb, var(--tile) ${Math.round(80 + 20 * c.gap / gmax)}%, var(--surface))` : "var(--mkt)";
      td.textContent = signedGap(c.gap);
      const count = document.createElement("span");
      count.textContent = letCount(c.n);
      td.appendChild(count);
      if (km === D.counts.radius && mo === 24) {
        td.className = "reference";
        const note = $("#reference-note");
        note.hidden = false;
        note.textContent = `Outlined: the ${letCount(c.n)} behind the headline figure.`;
      }
      td.addEventListener("pointermove", e => show(e, [`${gbp(c.median)} median`, `${letCount(c.n)} within ${km} km, last ${mo} months`]));
      td.addEventListener("pointerleave", hide);
    });
  });
}

function trend() {
  const box = $("#c-trend"); box.innerHTML = "";
  legend("#lg-trend", [["Agreed rents: median", css("--mkt")], ["middle half", css("--mkt-wash"), "b"], ["Borough average (ONS)", css("--ok")], ["This tenancy", css("--you")]]);
  const T = D.trend; if (!T.length) return; const O = D.ons.filter(o => o.m >= T[0].m);
  const W = Math.max(300, box.clientWidth), H = 300, m = {l: 52, r: W < 520 ? 12 : 112, t: 14, b: 28};
  const t0 = new Date(T[0].m + "-01"), t1 = new Date();
  const vals = T.flatMap(t => [t.q1, t.q3]).concat(O.map(o => o.v), D.history.map(h => h.rent), D.proposed || []);
  const lo = Math.floor((Math.min(...vals) - 50) / 250) * 250, hi = Math.ceil((Math.max(...vals) + 50) / 250) * 250;
  const x = d => m.l + (new Date(d) - t0) / (t1 - t0) * (W - m.l - m.r), y = v => H - m.b - (v - lo) / (hi - lo) * (H - m.t - m.b);
  const svg = el("svg", {viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": "Agreed rents over time against this tenancy's rent"}, box);
  ticks(lo, hi, 5).forEach(t => { el("line", {x1: m.l, x2: W - m.r, y1: y(t), y2: y(t), class: "gl"}, svg); tx(svg, m.l - 8, y(t) + 4, gbp(t), null, "end"); });
  // Year labels at each 1 January; if the series starts mid-year, label that first year at the start too.
  if (t0.getMonth() > 0 || t0.getDate() > 1) tx(svg, x(t0), H - 6, String(t0.getFullYear()), null, "start");
  for (let yr = t0.getFullYear(); yr <= t1.getFullYear(); yr++) { const d = new Date(yr, 0, 1);
    if (d >= t0) { el("line", {x1: x(d), x2: x(d), y1: H - m.b, y2: H - m.b + 4, class: "ax"}, svg); tx(svg, x(d), H - 6, String(yr), null, "middle"); } }
  const xm = s => x(s + "-15");
  el("path", {d: T.map((t, i) => (i ? "L" : "M") + xm(t.m) + "," + y(t.q3)).join("") + [...T].reverse().map(t => "L" + xm(t.m) + "," + y(t.q1)).join("") + "Z", fill: "var(--mkt-wash)"}, svg);
  el("path", {d: T.map((t, i) => (i ? "L" : "M") + xm(t.m) + "," + y(t.med)).join(""), fill: "none", stroke: "var(--mkt)", "stroke-width": 2.5, "stroke-linejoin": "round"}, svg);
  if (O.length) el("path", {d: O.map((o, i) => (i ? "L" : "M") + xm(o.m) + "," + y(o.v)).join(""), fill: "none", stroke: "var(--ok)", "stroke-width": 2, "stroke-linejoin": "round"}, svg);
  let p = ""; D.history.forEach((h, i) => { const nx = i + 1 < D.history.length ? D.history[i + 1].from + "-01" : t1; const xs = Math.max(m.l, x(h.from + "-01"));
    p += (i ? "L" : "M") + xs + "," + y(h.rent) + "L" + x(nx) + "," + y(h.rent); });
  el("path", {d: p, fill: "none", stroke: "var(--you)", "stroke-width": 3, "stroke-linejoin": "round"}, svg);
  if (D.proposed) el("circle", {cx: x(t1), cy: y((D.proposed[0] + D.proposed[1]) / 2), r: 6, fill: "var(--prop)", stroke: "var(--surface)", "stroke-width": 2}, svg);
  if (W >= 520) { const last = T[T.length - 1];
    tx(svg, W - m.r + 8, y(D.rent) + 4, `This ${gbp(D.rent)}`, "strong");
    tx(svg, W - m.r + 8, y(last.med) + 4, `Median ${gbp(last.med)}`, "lbl");
    if (O.length) tx(svg, W - m.r + 8, y(O[O.length - 1].v) + (Math.abs(y(O[O.length - 1].v) - y(last.med)) < 14 ? 16 : 4), `ONS ${gbp(O[O.length - 1].v)} (${new Date(O[O.length - 1].m + "-01").toLocaleDateString("en-GB", {month: "short"})})`, "lbl");
    if (D.proposed) tx(svg, W - m.r + 14, y((D.proposed[0] + D.proposed[1]) / 2) + 4, "Proposal", "lbl"); }
  const cross = el("line", {y1: m.t, y2: H - m.b, stroke: "var(--axis)", opacity: 0}, svg);
  const ov = el("rect", {x: m.l, y: m.t, width: W - m.l - m.r, height: H - m.t - m.b, class: "hit"}, svg);
  ov.addEventListener("pointermove", e => { const pt = svg.createSVGPoint(); pt.x = e.clientX; pt.y = e.clientY; const sx = pt.matrixTransform(svg.getScreenCTM().inverse()).x;
    const d = new Date(+t0 + (sx - m.l) / (W - m.l - m.r) * (t1 - t0)), key = d.toISOString().slice(0, 7);
    const t = T.reduce((b, v) => !b || Math.abs(new Date(v.m) - d) < Math.abs(new Date(b.m) - d) ? v : b, null);
    const o = O.reduce((b, v) => !b || Math.abs(new Date(v.m) - d) < Math.abs(new Date(b.m) - d) ? v : b, null);
    const h = [...D.history].reverse().find(h => h.from <= key);
    cross.setAttribute("x1", sx); cross.setAttribute("x2", sx); cross.setAttribute("opacity", 1);
    show(e, [fmtM(key), `Median agreed ${gbp(t.med)} (${t.n} lets)`, ...(o ? [`ONS average ${gbp(o.v)}`] : []), ...(h ? [`This tenancy ${gbp(h.rent)}`] : [])]); });
  ov.addEventListener("pointerleave", () => { cross.setAttribute("opacity", 0); hide(); });
}

function byKm() {
  const box = $("#c-km"); box.innerHTML = "";
  legend("#lg-km", [["Cheaper than this rent", css("--mkt"), "d"], ["Same or dearer", css("--muted"), "d"], ["This rent", css("--you")]]);
  const W = Math.max(300, box.clientWidth), H = 260, m = {l: 52, r: 12, t: 16, b: 30};
  const vals = P.concat(RENT); const lo = Math.floor((Math.min(...vals) - 100) / 250) * 250, hi = Math.ceil((Math.max(...vals) + 100) / 250) * 250;
  const kmax = Math.ceil(Math.max(...D.points.map(p => p.km), 1));
  const x = k => m.l + k / kmax * (W - m.l - m.r), y = v => H - m.b - (v - lo) / (hi - lo) * (H - m.t - m.b);
  const svg = el("svg", {viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": "Agreed rents by distance, with this rent marked"}, box);
  ticks(lo, hi, 5).forEach(t => { el("line", {x1: m.l, x2: W - m.r, y1: y(t), y2: y(t), class: "gl"}, svg); tx(svg, m.l - 8, y(t) + 4, gbp(t), null, "end"); });
  for (let k = 0; k <= kmax; k++) tx(svg, x(k), H - 8, `${k} km`, null, "middle");
  el("rect", {x: m.l, y: y(RENT), width: W - m.l - m.r, height: Math.max(0, y(lo) - y(RENT)), fill: "var(--mkt-wash)", opacity: .5}, svg);
  el("line", {x1: m.l, x2: W - m.r, y1: y(RENT), y2: y(RENT), stroke: "var(--you)", "stroke-width": 2.5}, svg);
  D.points.forEach(p => { const below = p.v < RENT;
    const c = el("circle", {cx: x(p.km), cy: y(p.v), r: 5, fill: below ? "var(--mkt)" : "var(--muted)", "fill-opacity": .85, stroke: "var(--surface)", "stroke-width": 1.5}, svg);
    const h = el("circle", {cx: x(p.km), cy: y(p.v), r: 10, class: "hit"}, svg);
    h.addEventListener("pointermove", e => { c.setAttribute("r", 7); show(e, [gbp(p.v), `${p.km} km away · let ${fmtM(p.m)}`, below ? `${gbp(RENT - p.v)} cheaper` : "same or dearer"]); });
    h.addEventListener("pointerleave", () => { c.setAttribute("r", 5); hide(); }); });
  tx(svg, W - m.r, y(RENT) - 8, `This rent ${gbp(RENT)}`, "strong", "end");
}

function judo() {
  const gh = $("#jd-gh"); if (D.repo) gh.href = D.repo; else gh.remove();
  // The card's chart, over time to the expected proposal: the market's middle half (blue band) and median (blue line),
  // this rent (orange steps) above it with the gap shaded, then a red dashed step up to the proposal (red dot).
  const svg = $("#jd-chart"); svg.innerHTML = "";
  const S = 400, T = D.trend; if (!T.length || !D.history.length) return;
  const smooth = key => T.map((_, i) => { let a = 0, b = 0; T.forEach((v, j) => { const k = Math.exp(-(((i - j) / 3.5) ** 2) / 2) * v.n; a += k * v[key]; b += k; }); return a / b; });
  const md = smooth("med"), q1 = smooth("q1"), q3 = smooth("q3");
  const pm = D.proposed ? (D.proposed[0] + D.proposed[1]) / 2 : null;
  const t0 = +new Date(T[0].m + "-01"), t1 = Date.now(), tp = t1 + 120 * 864e5;     // proposal shown ~4 months on
  const xL = S * .07, xP = S * .88, x = t => xL + (t - t0) / (tp - t0) * (xP - xL);
  const vals = q1.concat(q3, D.history.map(h => h.rent), pm ? [pm] : []);
  const lo = Math.min(...vals), hi = Math.max(...vals), y = v => S * .84 - (v - lo) / (hi - lo) * S * .64;
  const rentAt = t => { let r = D.history[0].rent; D.history.forEach(h => { if (+new Date(h.from + "-01") <= t) r = h.rent; }); return r; };
  const defs = el("defs", {}, svg), f = el("filter", {id: "jdglow", filterUnits: "userSpaceOnUse", x: 0, y: 0, width: S, height: S}, defs);
  el("feGaussianBlur", {stdDeviation: 5, result: "b"}, f); const mg = el("feMerge", {}, f); el("feMergeNode", {in: "b"}, mg); el("feMergeNode", {in: "SourceGraphic"}, mg);
  const vg = el("linearGradient", {id: "jdBand", x1: 0, x2: 0, y1: 0, y2: 1}, defs);
  el("stop", {offset: 0, "stop-color": "#3987e5", "stop-opacity": .32}, vg); el("stop", {offset: 1, "stop-color": "#3987e5", "stop-opacity": .08}, vg);
  // faint horizontal grid and year ticks
  for (let i = 1; i < 5; i++) el("line", {x1: 0, x2: S, y1: S * i / 5, y2: S * i / 5, stroke: "rgba(255,255,255,.06)"}, svg);
  const year = (t, s, a) => { const e = tx(svg, x(t), S - 14, s, null, a); e.style.fill = "rgba(244,243,239,.45)"; e.setAttribute("font-size", 13); };
  // a faint line at each 1 January; each year's label centred in its own span
  for (let yr = new Date(t0).getFullYear(); yr <= new Date(tp).getFullYear(); yr++) {
    const a = Math.max(t0, +new Date(yr, 0, 1)), b = Math.min(tp, +new Date(yr + 1, 0, 1));
    if (a > t0) el("line", {x1: x(a), x2: x(a), y1: S * .1, y2: S - 34, stroke: "rgba(255,255,255,.07)"}, svg);
    if (x(b) - x(a) > 34) year((a + b) / 2, String(yr), "middle"); }
  const ts = T.map(t => +new Date(t.m + "-15")), P = arr => ts.map((t, i) => [x(t), y(arr[i])]);
  const line = pts => pts.map((p, i) => (i ? "L" : "M") + p[0].toFixed(1) + "," + p[1].toFixed(1)).join("");
  el("path", {d: line(P(q3)) + line(P(q1).reverse()).replace("M", "L") + "Z", fill: "url(#jdBand)", class: "jd-fade"}, svg);
  el("path", {d: line(ts.map(t => [x(t), y(rentAt(t))])) + line(P(md).reverse()).replace("M", "L") + "Z", fill: "#ff7d3b", "fill-opacity": .14, class: "jd-fade"}, svg);
  el("path", {d: line(P(md)), fill: "none", stroke: "#3987e5", "stroke-width": 4.5, filter: "url(#jdglow)", "stroke-linecap": "round", "stroke-linejoin": "round", pathLength: 1, class: "jd-draw"}, svg);
  let r = `M${x(t0)},${y(rentAt(t0))}`;
  D.history.forEach(h => { const t = +new Date(h.from + "-01"); if (t > t0 && t < t1) r += `L${x(t)},${y(rentAt(t - 1))}L${x(t)},${y(h.rent)}`; });
  r += `L${x(t1)},${y(rentAt(t1))}`;
  el("path", {d: r, fill: "none", stroke: "#ff7d3b", "stroke-width": 4.5, filter: "url(#jdglow)", "stroke-linejoin": "round", "stroke-linecap": "round", pathLength: 1, class: "jd-draw"}, svg);
  const lab = (x0, y0, t, c, a = "end") => { const e = tx(svg, x0, y0, t, "jd-lab", a); e.style.fill = c; e.setAttribute("font-size", 16); e.setAttribute("font-weight", 650); };
  if (pm) {
    el("path", {d: `M${x(t1)},${y(rentAt(t1))}L${xP},${y(rentAt(t1))}L${xP},${y(pm)}`, fill: "none", stroke: "#e66767", "stroke-width": 2.5, "stroke-dasharray": "3 5", class: "jd-late"}, svg);
    el("circle", {cx: xP, cy: y(pm), r: 9, fill: "none", stroke: "#e66767", "stroke-width": 2, class: "jd-pulse"}, svg);
    el("circle", {cx: xP, cy: y(pm), r: 9, fill: "#e66767", filter: "url(#jdglow)", class: "jd-late"}, svg);
    lab(xP + 10, y(pm) - 20, "Proposal", "#f3a0a0");
  }
  lab(x(t1) - 4, y(rentAt(t1)) - 14, "This rent", "#ffa070");
  lab(x(ts[ts.length - 1]), y(q1[q1.length - 1]) + 26, "Market", "#8dbcf3");
}

function reveals() {
  // Fade cards up as they come into view. Robust by design: position checks on scroll (no reliance on
  // IntersectionObserver), and a safety net that shows everything after 2.5 s whatever happens.
  if (matchMedia("(prefers-reduced-motion: reduce)").matches) return;
  const els = [...document.querySelectorAll("main .card, main .dash-head")];
  els.forEach((e, i) => { if (e.getBoundingClientRect().top > innerHeight * 0.92) { e.classList.add("reveal"); e.style.transitionDelay = (i % 3) * 60 + "ms"; } });
  let ticking = false;
  const check = () => { ticking = false; const lim = innerHeight * 0.94;
    document.querySelectorAll(".reveal:not(.in)").forEach(e => { if (e.getBoundingClientRect().top < lim) e.classList.add("in"); }); };
  addEventListener("scroll", () => { if (!ticking) { ticking = true; requestAnimationFrame(check); } }, {passive: true});
  addEventListener("resize", check);
  setTimeout(() => document.querySelectorAll(".reveal").forEach(e => e.classList.add("in")), 2500);
  check();
}

function all() { header(); heroBar(); dist(); cells(); trend(); byKm(); judo(); }
all(); reveals();
let lw = innerWidth; addEventListener("resize", () => { if (Math.abs(innerWidth - lw) > 40) { lw = innerWidth; all(); } });
matchMedia("(prefers-color-scheme: dark)").addEventListener("change", all);
</script>
</body>
</html>
"""
