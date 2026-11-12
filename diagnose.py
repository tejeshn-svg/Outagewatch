#!/usr/bin/env python3
"""
OutageWatch -- diagnose stubborn vendors
========================================

For vendors that neither fixvendors.py nor guesswork can place, this asks each
candidate address what it ACTUALLY returns -- status code, content type, where
it redirected to, and which status-page platform (if any) its HTML mentions --
and writes the lot to diagnose.txt.

    python diagnose.py

The answers land in diagnose.txt. No guessing required after that.
"""

from __future__ import annotations

import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from collector import TIMEOUT, USER_AGENT, VENDORS_PATH  # noqa: E402

OUT = os.path.join(HERE, "diagnose.txt")

# Fingerprints that reveal which platform is behind a page.
MARKERS = [
    "statuspage.io", "status.io", "instatus", "statusgator", "sorryapp",
    "better stack", "betteruptime", "atlassian", "incident.io", "statuspal",
    "freshstatus", "hund.io", "cachet", "oh dear", "checkly", "rootly",
    "cloudflare", "akamai", "just a moment", "captcha", "access denied",
]

PATHS = [
    "/",
    "/api/v2/status.json",
    "/summary.json",
    "/api/v2/summary.json",
    "/index.json",
    "/status.json",
    "/history.rss",
    "/feed",
]


def hosts_for(vendor: dict) -> list:
    brand = vendor["slug"].replace("-", "")
    current = vendor["url"].replace("https://", "").replace("http://", "").strip("/").split("/")[0]
    out = [
        current,
        f"status.{brand}.com",
        f"{brand}status.com",
        f"www.{brand}status.com",
        f"{brand}.statuspage.io",
        f"{brand}.instatus.com",
        f"status.{brand}.io",
    ]
    seen, uniq = set(), []
    for h in out:
        if h and h not in seen:
            seen.add(h)
            uniq.append(h)
    return uniq


def check(host: str, path: str) -> str:
    url = f"https://{host}{path}"
    try:
        r = requests.get(url, timeout=TIMEOUT,
                         headers={"User-Agent": USER_AGENT},
                         allow_redirects=True)
    except requests.exceptions.RequestException as exc:
        return f"    {path:<22} ERROR {type(exc).__name__}: {str(exc)[:70]}"

    ctype = (r.headers.get("content-type") or "?").split(";")[0]
    line = f"    {path:<22} {r.status_code} {ctype:<26} {len(r.content)}b"

    if r.url.rstrip("/") != url.rstrip("/"):
        line += f"\n      redirected -> {r.url[:110]}"

    body = r.text[:200000].lower()
    found = [m for m in MARKERS if m in body]
    if found:
        line += f"\n      mentions: {', '.join(found[:6])}"

    if "json" in ctype or r.text.strip()[:1] in ("{", "["):
        try:
            data = r.json()
            if isinstance(data, dict):
                line += f"\n      JSON keys: {list(data.keys())[:10]}"
            elif isinstance(data, list) and data:
                line += f"\n      JSON array[{len(data)}], first keys: "
                line += str(list(data[0].keys())[:8] if isinstance(data[0], dict) else type(data[0]))
        except ValueError:
            line += "\n      (claims JSON but will not parse)"
    elif path == "/":
        snippet = " ".join(r.text[:400].split())
        line += f"\n      starts: {snippet[:150]}"
    return line


def main() -> int:
    with open(VENDORS_PATH, "r", encoding="utf-8") as fh:
        vendors = json.load(fh)["vendors"]

    wanted = sys.argv[1:] or ["fastly", "gitlab", "pagerduty", "zendesk", "algolia"]
    targets = [v for v in vendors if v["slug"] in wanted]
    if not targets:
        print(f"No matching vendors for {wanted}")
        return 1

    jobs = []
    for v in targets:
        for h in hosts_for(v):
            for p in PATHS:
                jobs.append((v["slug"], h, p))

    print(f"Running {len(jobs)} checks across {len(targets)} vendors. "
          f"Takes a minute or two...")

    with ThreadPoolExecutor(max_workers=10) as pool:
        results = list(pool.map(lambda j: (j[0], j[1], check(j[1], j[2])), jobs))

    lines = ["OutageWatch diagnose", "=" * 70, ""]
    for v in targets:
        lines.append(f"### {v['name']}  (slug: {v['slug']}, configured: {v['url']})")
        for h in hosts_for(v):
            lines.append(f"  https://{h}")
            for slug, host, text in results:
                if slug == v["slug"] and host == h:
                    lines.append(text)
            lines.append("")
        lines.append("")

    with open(OUT, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")

    print(f"\nWritten to {OUT}")
    print("Done. Review the output file above.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
