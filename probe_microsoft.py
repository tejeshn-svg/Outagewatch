#!/usr/bin/env python3
"""
OutageWatch -- hunt for a usable Microsoft 365 status feed
==========================================================

Azure publishes a public RSS feed, which is why Azure works. Microsoft 365
(Outlook, Exchange, Teams, Intune) is the gap: its official health data sits
behind the Graph API and needs a tenant login.

But the public status page has to get its data from somewhere, and that
somewhere is reachable from a browser without signing in. This looks for it:
fetches the page, reads its JavaScript, and tries every endpoint it mentions.

    python probe_microsoft.py

Writes everything to microsoft.txt.
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

OUT = os.path.join(HERE, "microsoft.txt")
H = {"User-Agent": USER_AGENT, "Accept": "*/*"}
lines = []


def log(*parts):
    text = " ".join(str(p) for p in parts)
    print(text)
    lines.append(text)


def get(url, **kw):
    try:
        return requests.get(url, timeout=TIMEOUT, headers=H, allow_redirects=True, **kw)
    except requests.exceptions.RequestException as exc:
        return exc


def describe(url: str, r) -> None:
    if isinstance(r, Exception):
        log(f"    {url[:80]:<80} ERROR {type(r).__name__}")
        return
    ctype = (r.headers.get("content-type") or "?").split(";")[0]
    log(f"    {url[:80]:<80} {r.status_code} {ctype} {len(r.content)}b")
    if r.url.rstrip("/") != url.rstrip("/"):
        log(f"      -> redirected to {r.url[:100]}")
    body = r.text.strip()
    if r.status_code == 200 and (body[:1] in "{[" or "json" in ctype):
        try:
            data = r.json()
        except ValueError:
            log("      (claims JSON, will not parse)")
            return
        if isinstance(data, dict):
            log(f"      JSON keys: {list(data.keys())[:14]}")
            log(f"      sample: {json.dumps(data)[:500]}")
        elif isinstance(data, list):
            log(f"      JSON array of {len(data)}")
            if data:
                log(f"      first: {json.dumps(data[0])[:500]}")
    elif r.status_code == 200 and body[:5].lower() in ("<?xml", "<rss "):
        log("      XML/RSS feed")
        log(f"      sample: {' '.join(body[:500].split())}")


# ---------------------------------------------------------- known candidates
log("=" * 74)
log("1. DIRECT CANDIDATES")
log("=" * 74)

CANDIDATES = [
    "https://status.cloud.microsoft/",
    "https://status.cloud.microsoft/api/v2/status.json",
    "https://status.cloud.microsoft/api/status",
    "https://status.cloud.microsoft/api/services",
    "https://status.cloud.microsoft/rss",
    "https://status.cloud.microsoft/feed",
    "https://status.office365.com/",
    "https://status.office365.com/api/status",
    "https://portal.office.com/api/servicestatus",
    "https://portal.office.com/servicestatus",
    "https://admin.microsoft.com/servicestatus",
    "https://azure.status.microsoft/en-us/status/feed/",
    "https://support.xbox.com/en-US/xbox-live-status",
]
for url in CANDIDATES:
    describe(url, get(url))

# ------------------------------------------------------ scan the JS bundles
log("")
log("=" * 74)
log("2. WHAT THE STATUS PAGE'S OWN JAVASCRIPT CALLS")
log("=" * 74)

ENDPOINT = re.compile(
    r"""["'`]((?:https?://[A-Za-z0-9.\-]+)?/(?:api|data|v1|v2|status|service|health)"""
    r"""[A-Za-z0-9/_\-.]*)["'`]""",
    re.I,
)

for site in ("https://status.cloud.microsoft/", "https://status.office365.com/"):
    log(f"\n  --- {site}")
    r = get(site)
    if isinstance(r, Exception) or r.status_code != 200:
        log(f"    could not load ({r if isinstance(r, Exception) else r.status_code})")
        continue

    scripts = re.findall(r'<script[^>]+src=["\']([^"\']+)["\']', r.text)
    log(f"    {len(scripts)} script file(s)")

    found = set()
    for m in ENDPOINT.findall(r.text):
        found.add(m)

    for src in scripts[:8]:
        js = urljoin(site, src)
        jr = get(js)
        if isinstance(jr, Exception) or jr.status_code != 200:
            continue
        log(f"    scanned {js[:95]} ({len(jr.content)}b)")
        for m in ENDPOINT.findall(jr.text):
            if len(m) < 140 and not m.endswith((".js", ".css", ".png", ".svg", ".woff", ".ico")):
                found.add(m)

    if found:
        log(f"    {len(found)} candidate endpoint(s):")
        for f in sorted(found)[:50]:
            log(f"      {f}")
        log("")
        log("    trying the most promising ones:")
        for f in sorted(found)[:25]:
            url = f if f.startswith("http") else urljoin(site, f)
            describe(url, get(url))
    else:
        log("    no endpoints found in the bundles")

with open(OUT, "w", encoding="utf-8") as fh:
    fh.write("\n".join(lines) + "\n")
print(f"\nWritten to {OUT}")
print("Done. Review the output file above.")
