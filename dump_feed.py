#!/usr/bin/env python3
"""
OutageWatch -- dump a feed verbatim
===================================

Prints exactly what a vendor's feed returns, so an adapter can be written
against the real thing rather than a guess.

    python dump_feed.py                 the two Microsoft feeds
    python dump_feed.py <url> [<url>]   any feeds you like

Writes to feed.txt.
"""

from __future__ import annotations

import os
import sys
import xml.etree.ElementTree as ET

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from collector import TIMEOUT, USER_AGENT  # noqa: E402

OUT = os.path.join(HERE, "feed.txt")

DEFAULTS = [
    "https://status.cloud.microsoft/api/feed/mac",
    "https://status.cloud.microsoft/api/feed/ppac",
]

lines = []


def log(*parts):
    text = " ".join(str(p) for p in parts)
    print(text)
    lines.append(text)


def dump(url: str) -> None:
    log("=" * 74)
    log(url)
    log("=" * 74)
    try:
        r = requests.get(url, timeout=TIMEOUT,
                         headers={"User-Agent": USER_AGENT}, allow_redirects=True)
    except requests.exceptions.RequestException as exc:
        log(f"  ERROR {type(exc).__name__}: {exc}")
        return

    log(f"  HTTP {r.status_code}  {r.headers.get('content-type')}  {len(r.content)} bytes")
    log("")

    body = r.text
    try:
        root = ET.fromstring(body)
    except ET.ParseError as exc:
        log(f"  not parseable XML ({exc}); raw body follows:")
        log(body[:4000])
        return

    channel_title = root.findtext(".//channel/title")
    log(f"  channel title: {channel_title}")
    items = root.findall(".//item")
    log(f"  items: {len(items)}")
    log("")

    for n, item in enumerate(items, 1):
        log(f"  --- item {n} ---")
        for tag in ("title", "pubDate", "link", "category", "guid"):
            value = item.findtext(tag)
            if value:
                log(f"    {tag}: {' '.join(value.split())[:300]}")
        desc = item.findtext("description") or ""
        if desc:
            log("    description:")
            flat = " ".join(desc.split())
            for i in range(0, min(len(flat), 1800), 150):
                log(f"      {flat[i:i+150]}")
        # anything else the feed carries
        for child in item:
            name = child.tag.split("}")[-1]
            if name not in ("title", "pubDate", "link", "category", "guid", "description"):
                text = " ".join((child.text or "").split())[:200]
                if text:
                    log(f"    {name}: {text}")
        log("")


def main() -> int:
    urls = sys.argv[1:] or DEFAULTS
    for u in urls:
        dump(u)
        log("")
    with open(OUT, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    print(f"\nWritten to {OUT}")
    print("Done. Review the output file above.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
