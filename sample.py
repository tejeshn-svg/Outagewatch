#!/usr/bin/env python3
"""
OutageWatch -- collect exact samples from the stubborn vendors
==============================================================

Three jobs:
  1. Fetch Fastly's homemade JSON so its format can be read exactly.
  2. Find GitLab's Status.io page id and prove the API works.
  3. Dig through the JavaScript of the three single-page status sites to see
     whether they fetch their data from an endpoint we could use directly.

Writes everything to sample.txt.

    python sample.py
"""

from __future__ import annotations

import json
import os
import re
import sys
from urllib.parse import urljoin

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from collector import TIMEOUT, USER_AGENT  # noqa: E402

OUT = os.path.join(HERE, "sample.txt")
H = {"User-Agent": USER_AGENT}
lines = []


def log(*parts):
    text = " ".join(str(p) for p in parts)
    print(text)
    lines.append(text)


def get(url, **kw):
    try:
        return requests.get(url, timeout=TIMEOUT, headers=H, **kw)
    except requests.exceptions.RequestException as exc:
        log(f"    ERROR {type(exc).__name__}: {str(exc)[:90]}")
        return None


# ---------------------------------------------------------------- 1. Fastly
log("=" * 72)
log("1. FASTLY -- exact shape of its custom JSON")
log("=" * 72)
for url in ("https://www.fastlystatus.com/summary.json",
            "https://www.fastlystatus.com/status.json"):
    log(f"\n  {url}")
    r = get(url)
    if r is None or r.status_code != 200:
        log(f"    HTTP {getattr(r, 'status_code', '?')}")
        continue
    try:
        data = r.json()
    except ValueError:
        log("    not JSON")
        continue
    log(f"    top-level keys: {list(data.keys())}")
    for key, value in data.items():
        if isinstance(value, list):
            log(f"    {key}: list of {len(value)}")
            if value:
                log(f"      first item: {json.dumps(value[0])[:600]}")
        elif isinstance(value, dict):
            log(f"    {key}: dict keys {list(value.keys())[:12]}")
        else:
            log(f"    {key}: {json.dumps(value)[:200]}")

# --------------------------------------------------------------- 2. GitLab
log("\n" + "=" * 72)
log("2. GITLAB -- find the Status.io page id")
log("=" * 72)
r = get("https://status.gitlab.com/")
if r is not None and r.status_code == 200:
    html = r.text
    ids = list(dict.fromkeys(re.findall(r"[0-9a-f]{24}", html)))
    log(f"  found {len(ids)} candidate id(s) in the page HTML: {ids[:10]}")

    for pattern in (r"statuspage_id['\"]?\s*[:=]\s*['\"]([0-9a-f]{24})",
                    r"api\.status\.io/1\.0/status/([0-9a-f]{24})",
                    r"status\.io/1\.0/[a-z]+/([0-9a-f]{24})"):
        hits = re.findall(pattern, html, re.I)
        if hits:
            log(f"  pattern {pattern[:38]}... -> {hits[:4]}")

    for page_id in ids[:8]:
        api = f"https://api.status.io/1.0/status/{page_id}"
        rr = get(api)
        if rr is None:
            continue
        if rr.status_code != 200:
            log(f"  {page_id}: HTTP {rr.status_code}")
            continue
        try:
            res = rr.json().get("result") or {}
        except ValueError:
            log(f"  {page_id}: HTTP 200 but not JSON")
            continue
        overall = res.get("status_overall") or {}
        if overall:
            log(f"  *** WORKS: {api}")
            log(f"      page name   : {res.get('name')}")
            log(f"      overall     : {overall.get('status')} (code {overall.get('status_code')})")
            comps = res.get("status") or []
            log(f"      components  : {len(comps)}")
            for c in comps[:5]:
                log(f"        - {c.get('name')}: {c.get('status')} ({c.get('status_code')})")
            break
else:
    log("  could not load status.gitlab.com")

# ------------------------------------------------- 3. The JavaScript apps
log("\n" + "=" * 72)
log("3. PAGERDUTY / ZENDESK / ALGOLIA -- hunt for hidden data endpoints")
log("=" * 72)

INTERESTING = re.compile(
    r"""["'`](/(?:api|v1|v2|data|status|services|components|incidents)[A-Za-z0-9/_\-.]*"""
    r"""|https?://[A-Za-z0-9.\-]+/[A-Za-z0-9/_\-.]*(?:api|status|incident|component)"""
    r"""[A-Za-z0-9/_\-.]*)["'`]""",
    re.I,
)

for name, site in (("PagerDuty", "https://status.pagerduty.com/"),
                   ("Zendesk", "https://status.zendesk.com/"),
                   ("Algolia", "https://status.algolia.com/")):
    log(f"\n  --- {name} ({site})")
    r = get(site)
    if r is None or r.status_code != 200:
        log(f"    HTTP {getattr(r, 'status_code', '?')}")
        continue

    scripts = re.findall(r'<script[^>]+src=["\']([^"\']+)["\']', r.text)
    log(f"    {len(scripts)} script file(s)")

    found = set()
    for src in scripts[:6]:
        js_url = urljoin(site, src)
        jr = get(js_url)
        if jr is None or jr.status_code != 200:
            continue
        log(f"    scanned {js_url[:95]} ({len(jr.content)}b)")
        for m in INTERESTING.findall(jr.text):
            if len(m) < 120 and not m.endswith((".js", ".css", ".png", ".svg", ".woff", ".ico")):
                found.add(m)

    # Also check any inline JSON the page ships with.
    for m in INTERESTING.findall(r.text):
        if len(m) < 120:
            found.add(m)

    if found:
        log(f"    candidate endpoints ({len(found)}):")
        for f in sorted(found)[:40]:
            log(f"      {f}")
    else:
        log("    no obvious endpoints found in the bundles")

with open(OUT, "w", encoding="utf-8") as fh:
    fh.write("\n".join(lines) + "\n")
print(f"\nWritten to {OUT}")
print("Done. Review the output file above.")
