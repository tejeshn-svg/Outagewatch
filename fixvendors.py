#!/usr/bin/env python3
"""
OutageWatch -- vendor repair tool
=================================

Any vendor showing as "Unknown" on your dashboard has a broken address in
vendors.json. This hunts for the right one.

For each broken vendor it tries the addresses companies actually use --
status.x.com, xstatus.com, x.statuspage.io, status.x.io and so on -- and when
one answers with real Statuspage data, it writes the fix into vendors.json.

    python fixvendors.py            find fixes, show them, ask before saving
    python fixvendors.py --yes      find fixes and save without asking
    python fixvendors.py --all      re-test every vendor, not just broken ones

Stop the collector window before running this, then start it again afterwards.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from collector import DB_PATH, TIMEOUT, USER_AGENT, VENDORS_PATH  # noqa: E402

# Extra hand-known addresses for vendors that moved somewhere unguessable.
KNOWN = {
    "gitlab":    ["status.gitlab.com", "gitlab.statuspage.io"],
    "pagerduty": ["status.pagerduty.com", "pagerduty.statuspage.io"],
    "zendesk":   ["status.zendesk.com", "zendesk.statuspage.io", "support.zendesk.com"],
    "notion":    ["status.notion.so", "notion.statuspage.io", "www.notion-status.com"],
    "algolia":   ["status.algolia.com", "algolia.statuspage.io"],
    "datadog":   ["status.datadoghq.com", "datadog.statuspage.io"],
    # Fastly redesigned and moved their status page off its old address.
    "fastly":    ["www.fastlystatus.com", "fastlystatus.com", "status.fastly.com"],
}


def host_of(url: str) -> str:
    return url.replace("https://", "").replace("http://", "").strip("/").split("/")[0]


def candidates(vendor: dict) -> list:
    """Every address worth trying for this vendor, best guesses first."""
    current = host_of(vendor["url"])
    brand = vendor["slug"].replace("-", "")
    out = list(KNOWN.get(vendor["slug"], []))
    out += [
        current,
        f"status.{brand}.com",
        f"{brand}status.com",
        f"www.{brand}status.com",
        f"{brand}.statuspage.io",
        f"status.{brand}.io",
        f"status.{brand}.net",
        f"status.{brand}.org",
    ]
    # Also try the bare registered domain of whatever is configured now.
    parts = current.split(".")
    if len(parts) > 2:
        out.append("status." + ".".join(parts[-2:]))
    seen, uniq = set(), []
    for h in out:
        if h and h not in seen:
            seen.add(h)
            uniq.append(h)
    return uniq


def _get(url: str):
    try:
        return requests.get(url, timeout=TIMEOUT,
                            headers={"User-Agent": USER_AGENT},
                            allow_redirects=True)
    except requests.exceptions.RequestException:
        return None


def try_host(host: str) -> dict | None:
    """Work out which status-page platform this address runs, if any.

    Statuspage is the most common, but it is not the only one. Instatus and
    Status.io between them cover most of the rest, and each publishes its own
    public JSON. Detecting the platform is what lets one vendor list cover
    companies that made different vendor choices.
    """

    # --- Atlassian Statuspage ---------------------------------------------
    r = _get(f"https://{host}/api/v2/status.json")
    if r is not None and r.status_code == 200:
        try:
            d = r.json()
            if isinstance(d, dict) and "status" in d and "page" in d:
                return {
                    "kind": "statuspage",
                    "url": f"https://{host_of(r.url) or host}",
                    "page_name": (d.get("page") or {}).get("name", host),
                    "description": (d.get("status") or {}).get("description", ""),
                }
        except ValueError:
            pass

    # --- Instatus ----------------------------------------------------------
    r = _get(f"https://{host}/summary.json")
    if r is not None and r.status_code == 200:
        try:
            d = r.json()
            page = d.get("page") if isinstance(d, dict) else None
            if isinstance(page, dict) and isinstance(page.get("status"), str):
                return {
                    "kind": "instatus",
                    "url": f"https://{host_of(r.url) or host}",
                    "page_name": page.get("name", host),
                    "description": page.get("status", ""),
                }
        except ValueError:
            pass

    # --- Status.io ---------------------------------------------------------
    # The page id is embedded in the status page's own HTML, so read it there
    # and then talk to the documented public API.
    r = _get(f"https://{host}/")
    if r is not None and r.status_code == 200 and "status.io" in r.text.lower():
        # A Status.io page is littered with 24-character hex ids -- one per
        # component. Only one is the PAGE id, and it is the one labelled
        # statuspage_id. Look for the labelled one first; scanning blindly
        # finds dozens of component ids and tests the wrong ones.
        labelled = re.findall(
            r"statuspage_id['\"]?\s*[:=]\s*['\"]([0-9a-f]{24})", r.text, re.I
        ) + re.findall(r"api\.status\.io/1\.0/status/([0-9a-f]{24})", r.text, re.I)
        ids = labelled + re.findall(r"[0-9a-f]{24}", r.text)
        for page_id in list(dict.fromkeys(ids))[:12]:
            api = f"https://api.status.io/1.0/status/{page_id}"
            rr = _get(api)
            if rr is None or rr.status_code != 200:
                continue
            try:
                res = rr.json().get("result") or {}
            except ValueError:
                continue
            overall = res.get("status_overall") or {}
            if overall.get("status_code"):
                return {
                    "kind": "status_io",
                    "url": api,
                    "page_name": res.get("name", host),
                    "description": overall.get("status", ""),
                }
    return None


def broken_slugs() -> set:
    if not os.path.exists(DB_PATH):
        print("No database yet. Run 'python collector.py --once' first.")
        raise SystemExit(1)
    # Copy-free read; the collector may hold a write lock, so be patient.
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT slug FROM current WHERE level='unknown'").fetchall()
    conn.close()
    return {r["slug"] for r in rows}


def repair(vendor: dict) -> tuple:
    for host in candidates(vendor):
        hit = try_host(host)
        if hit:
            return vendor, hit
    return vendor, None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--yes", action="store_true", help="save without asking")
    ap.add_argument("--all", action="store_true", help="re-test every vendor")
    args = ap.parse_args()

    with open(VENDORS_PATH, "r", encoding="utf-8") as fh:
        doc = json.load(fh)
    vendors = doc["vendors"]

    HOSTED = ("statuspage", "instatus", "status_io")
    if args.all:
        targets = [v for v in vendors if v["kind"] in HOSTED]
    else:
        bad = broken_slugs()
        targets = [v for v in vendors if v["slug"] in bad and v["kind"] in HOSTED]

    if not targets:
        print("Nothing to repair -- no vendors are showing as Unknown.")
        return 0

    print(f"Testing {len(targets)} vendor(s). This takes a moment...\n")
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(repair, targets))

    fixes, failures = [], []
    for vendor, hit in results:
        if not hit:
            failures.append(vendor)
            print(f"  NO LUCK   {vendor['name']:<20} -- tried {len(candidates(vendor))} addresses")
            continue
        if hit["url"] == vendor["url"] and hit["kind"] == vendor["kind"]:
            print(f"  OK NOW    {vendor['name']:<20} {hit['url']}  ({hit['description']})")
            continue
        fixes.append((vendor, hit))
        print(f"  FIXED     {vendor['name']:<20} {vendor['url']}  [{vendor['kind']}]")
        print(f"            {'':<20} -> {hit['url']}  [{hit['kind']}]  ({hit['description']})")

    if failures:
        print(f"\n{len(failures)} vendor(s) could not be found. They probably don't")
        print("publish a machine-readable status feed at all. Either delete them from")
        print("vendors.json, or change their \"kind\" to \"probe\" and point the url at")
        print("the service's real homepage so you at least learn when it stops answering.")

    if not fixes:
        print("\nNo address changes to save.")
        return 0

    if not args.yes:
        answer = input(f"\nSave {len(fixes)} fix(es) to vendors.json? [y/N] ").strip().lower()
        if answer not in ("y", "yes"):
            print("Nothing saved.")
            return 0

    for vendor, hit in fixes:
        for v in vendors:
            if v["slug"] == vendor["slug"]:
                v["url"] = hit["url"]
                v["kind"] = hit["kind"]

    backup = VENDORS_PATH + ".backup"
    if not os.path.exists(backup):
        with open(VENDORS_PATH, "r", encoding="utf-8") as src, \
             open(backup, "w", encoding="utf-8") as dst:
            dst.write(src.read())
        print(f"Original saved as {os.path.basename(backup)}")

    with open(VENDORS_PATH, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2)
        fh.write("\n")

    print(f"Saved {len(fixes)} fix(es) to vendors.json.")
    print("Now restart the collector window and the dashboard will clear up.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
