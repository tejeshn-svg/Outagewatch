#!/usr/bin/env python3
"""
OutageWatch -- vendor discovery
===============================

Before you add a vendor to vendors.json, check whether it actually speaks the
Atlassian Statuspage protocol. If it does, you get it for free. This is how you
grow a list of 30 vendors into 300 without writing any new code.

    python discover.py status.vendor.com
    python discover.py vendor.com status.other.com www.thirdstatus.com

It tries the obvious hostnames and prints a ready-to-paste vendors.json line.
"""

from __future__ import annotations

import json
import sys

import requests

from collector import TIMEOUT, USER_AGENT

CANDIDATE_PREFIXES = ["", "status.", "www.", "status."]


def candidates(domain: str) -> list:
    domain = domain.strip().replace("https://", "").replace("http://", "").strip("/")
    out = [domain]
    if not domain.startswith("status."):
        out.append("status." + domain)
    if not domain.startswith("www."):
        out.append("www." + domain)
    # many vendors use <name>status.com, e.g. githubstatus.com
    bare = domain.split(".")[0]
    out.append(f"{bare}status.com")
    out.append(f"www.{bare}status.com")
    seen, uniq = set(), []
    for d in out:
        if d not in seen:
            seen.add(d)
            uniq.append(d)
    return uniq


def probe(host: str) -> dict | None:
    url = f"https://{host}/api/v2/summary.json"
    try:
        r = requests.get(url, timeout=TIMEOUT, headers={"User-Agent": USER_AGENT})
    except requests.exceptions.RequestException:
        return None
    if r.status_code != 200:
        return None
    try:
        data = r.json()
    except ValueError:
        return None
    if "status" not in data or "page" not in data:
        return None
    return {
        "host": host,
        "page_name": (data.get("page") or {}).get("name", host),
        "components": len(data.get("components") or []),
        "indicator": (data.get("status") or {}).get("indicator", "?"),
        "description": (data.get("status") or {}).get("description", ""),
    }


def slugify(name: str) -> str:
    keep = [c.lower() if c.isalnum() else "-" for c in name]
    slug = "".join(keep)
    while "--" in slug:
        slug = slug.replace("--", "-")
    return slug.strip("-")[:28]


def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        raise SystemExit(1)

    for domain in sys.argv[1:]:
        print(f"\n== {domain}")
        hit = None
        for host in candidates(domain):
            print(f"   trying https://{host}/api/v2/summary.json ... ", end="", flush=True)
            hit = probe(host)
            print("YES" if hit else "no")
            if hit:
                break

        if not hit:
            print("   Not an Atlassian Statuspage (or unreachable).")
            print('   Fall back to a plain probe entry:')
            print("   " + json.dumps({
                "slug": slugify(domain.split(".")[0]),
                "name": domain.split(".")[0].title(),
                "kind": "probe",
                "url": f"https://{domain}",
                "tags": ["Uncategorised"],
            }))
            continue

        print(f"   page name : {hit['page_name']}")
        print(f"   components: {hit['components']}")
        print(f"   right now : {hit['indicator']} -- {hit['description']}")
        print("   Add this to vendors.json:")
        print("   " + json.dumps({
            "slug": slugify(hit["page_name"]),
            "name": hit["page_name"],
            "kind": "statuspage",
            "url": f"https://{hit['host']}",
            "tags": ["Uncategorised"],
        }))


if __name__ == "__main__":
    main()
