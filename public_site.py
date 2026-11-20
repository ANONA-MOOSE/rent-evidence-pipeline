#!/usr/bin/env python3
"""
Build (and optionally publish) the PUBLIC showcase copy of the dashboard.

  python3 public_site.py            # build public_site/index.html and run the privacy check
  python3 public_site.py --deploy   # ...then upload it to Cloudflare Pages if the check passes

The public copy keeps the method and the comparables evidence and drops everything private:
the defects log, key dates and correspondence, the exact address, names, reference numbers,
OpenRent listing text, and full postcodes in tribunal decisions. Before anything is uploaded,
privacy_check() scans the finished page for PUBLIC_BLOCKLIST words (config_local.py), email
addresses and full UK postcodes; if it finds any, nothing is published.

Cloudflare: put an API token with "Cloudflare Pages: Edit" in cloudflare_token.txt, your account ID
in cloudflare_account.txt, and (optionally) a project name in cloudflare_project.txt. All three files
are git-ignored. Uploads use Wrangler (installed in node_modules).
"""
import glob
import json
import os
import re
import subprocess
import sys

import openrent_comps as oc

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(HERE, "public_site")
EMAIL_RX = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
POSTCODE_RX = re.compile(r"\b[A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2}\b")   # full UK postcode (districts like "SE13" are fine)


def _read(name):
    p = os.path.join(HERE, name)
    return open(p).read().strip() if os.path.exists(p) else ""


ROUND_TO = 50   # the subject's own rents are shown rounded to the nearest £50 on the public page


def _r(v):
    return round(v / ROUND_TO) * ROUND_TO if v else v


def public_data():
    import dashboard
    import analysis
    d = dashboard.gather()
    # The example flat's own rents, rounded; figures derived from them are recomputed from the rounded rent.
    d["rentHistory"] = [{"from": h["from"], "rent": _r(h["rent"])} for h in d["rentHistory"]]
    rounded_rent = d["rentHistory"][-1]["rent"] if d["rentHistory"] else None
    if rounded_rent:
        rows = oc.load_rows(oc.db())
        d["sens"] = analysis.sensitivity(rows, rent=rounded_rent)
        d["market"] = analysis.time_on_market(rows, threshold=rounded_rent)
    d["roundedTo"] = ROUND_TO
    d["public"] = True
    d["title"] = "Two-bed rent evidence: a live worked example"
    d["publicDescription"] = oc.PUBLIC_DESCRIPTION
    d["repoUrl"] = oc.PUBLIC_REPO_URL or _read("public_repo_url.txt")
    # Private panels: none of this leaves the computer.
    d["timeline"] = []
    d["defects"] = []
    d["pattern"] = None
    # Adjustment grid: keep the method, drop the subject flat's own defect deductions.
    g = d.get("grid") or {}
    c = g.get("conclusion")
    if c:
        cond = c.get("condition_pct") or 0
        undo = 1 / (1 + cond / 100) if cond else 1
        c["defects"], c["condition_pct"] = [], 0
        c["indicated"] = c["weighted_mean"]
        c["indicated_low"], c["indicated_high"] = c["low"], c["high"]
        if c.get("size_note"):
            c["size_note"]["indicated"] = c["size_note"]["weighted"]
        if c.get("loc_alt") and c["loc_alt"].get("indicated"):
            c["loc_alt"]["indicated"] = round(c["loc_alt"]["indicated"] * undo)
    for line in g.get("lines") or []:
        line["ai"] = None                      # AI descriptions quote listing text
    # Comparables: figures and links only, no listing text.
    for x in d.get("comps") or []:
        x.pop("why", None)
    # Tribunal decisions are public records, but show postcode sectors only and no AI-written notes.
    for t in d.get("tribunal") or []:
        pc = (t.get("postcode") or "").strip()
        t["postcode"] = pc[:-2].strip() if len(pc) > 4 else pc     # "SE6 2HB" -> "SE6 2"
        t["url"] = "https://rentrisecheck.uk"
        t["lesson"] = t["reasoning"] = None
    return d


def privacy_check(html):
    """Return a list of problems; empty means safe to publish."""
    problems = []
    low = html.lower()
    for word in oc.PUBLIC_BLOCKLIST:
        if word and word.lower() in low:
            problems.append(f"blocked word present: {word!r}")
    for m in set(EMAIL_RX.findall(html)):
        problems.append(f"email address present: {m}")
    for m in set(POSTCODE_RX.findall(html)):
        problems.append(f"full postcode present: {m}")
    return problems


def build():
    import dashboard
    d = public_data()
    html = dashboard.TEMPLATE.replace("/*__DATA__*/null", json.dumps(d, default=str).replace("</", "<\\/"))
    html = html.replace("<title>Rent comparables</title>", "<title>Rent evidence demo</title>")
    problems = privacy_check(html)
    os.makedirs(OUT_DIR, exist_ok=True)
    path = os.path.join(OUT_DIR, "index.html")
    if problems:
        if os.path.exists(path):
            os.remove(path)                    # never leave an unsafe page lying around to be uploaded
        return None, problems
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)
    with open(os.path.join(OUT_DIR, "_headers"), "w") as f:
        f.write("/*\n  Cache-Control: public, max-age=300\n  Referrer-Policy: no-referrer\n  X-Content-Type-Options: nosniff\n")
    return path, []


def _npx():
    for p in sorted(glob.glob(os.path.expanduser("~/.nvm/versions/node/*/bin/npx")), reverse=True) + ["/opt/homebrew/bin/npx", "/usr/local/bin/npx"]:
        if os.path.exists(p):
            return p
    return "npx"


def deploy():
    token, account = _read("cloudflare_token.txt"), _read("cloudflare_account.txt")
    project = _read("cloudflare_project.txt") or "rent-evidence-demo"
    if not token or not account:
        print("Public site built but not uploaded: add cloudflare_token.txt and cloudflare_account.txt (see public_site.py).")
        return False
    env = dict(os.environ, CLOUDFLARE_API_TOKEN=token, CLOUDFLARE_ACCOUNT_ID=account,
               PATH=os.path.dirname(_npx()) + ":" + os.environ.get("PATH", "/usr/bin:/bin"))
    npx = _npx()
    # Create the project on first use (harmless "already exists" error afterwards).
    subprocess.run([npx, "wrangler", "pages", "project", "create", project, "--production-branch", "main"],
                   cwd=HERE, env=env, capture_output=True, text=True, timeout=120)
    r = subprocess.run([npx, "wrangler", "pages", "deploy", OUT_DIR, "--project-name", project, "--branch", "main",
                        "--commit-dirty=true"], cwd=HERE, env=env, capture_output=True, text=True, timeout=300)
    out = (r.stdout + r.stderr).strip()
    print(out[-600:])
    return r.returncode == 0


def run(do_deploy=True):
    path, problems = build()
    if problems:
        print("PUBLIC SITE NOT BUILT: privacy check failed:\n  " + "\n  ".join(problems))
        oc.notify("Rent comparables", "Public site NOT published: privacy check failed. See logs/daily.log.")
        return False
    print(f"Public site built: {path} (privacy check passed)")
    if do_deploy:
        ok = deploy()
        if ok is False and os.path.exists(os.path.join(HERE, "cloudflare_token.txt")):
            oc.notify("Rent comparables", "Public site upload to Cloudflare failed. See logs/daily.log.")
        return ok
    return True


if __name__ == "__main__":
    run(do_deploy="--deploy" in sys.argv)
