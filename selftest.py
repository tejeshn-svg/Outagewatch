#!/usr/bin/env python3
"""
OutageWatch -- offline self-test
================================

Builds fake-but-realistically-shaped status feeds in fixtures/, runs the
collector against them with no network at all, and checks that:

  * every adapter parses
  * each vendor vocabulary maps to the right normalised level
  * a second pass with changed data produces exactly the right transitions

Run:  python selftest.py
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import collector  # noqa: E402

FIXTURES = collector.FIXTURES_DIR


# ---------------------------------------------------------------------------
# Fixture builders -- these mirror the real response shapes.
# ---------------------------------------------------------------------------

def statuspage(name, indicator, description, components=(), incidents=(), maint=()):
    return json.dumps({
        "page": {"id": "x", "name": name, "url": f"https://status.{name.lower()}.com"},
        "status": {"indicator": indicator, "description": description},
        "components": [
            {"id": f"c{i}", "name": n, "status": s, "group": False}
            for i, (n, s) in enumerate(components)
        ],
        "incidents": [
            {"id": f"i{i}", "name": n, "status": st, "impact": imp,
             "created_at": "2026-11-12T08:15:00Z", "shortlink": "https://stspg.io/x"}
            for i, (n, st, imp) in enumerate(incidents)
        ],
        "scheduled_maintenances": [
            {"name": n, "status": st, "scheduled_for": "2026-11-12T02:00:00Z"}
            for n, st in maint
        ],
    })


def instatus(status="UP", incidents=()):
    return json.dumps({
        "page": {"name": "Acme", "url": "https://acme.instatus.com", "status": status},
        "activeIncidents": [
            {"name": n, "status": "INVESTIGATING", "impact": imp,
             "started": "2026-11-12T08:00:00Z", "url": "https://acme.instatus.com/x"}
            for n, imp in incidents
        ],
        "activeMaintenances": [],
    })


def status_io(code=100, components=()):
    names = {100: "Operational", 300: "Degraded Performance",
             400: "Partial Service Disruption", 500: "Service Disruption"}
    return json.dumps({"result": {
        "status_overall": {"status": names.get(code, "?"), "status_code": code},
        "status": [{"name": n, "status": names.get(c, "?"), "status_code": c}
                   for n, c in components],
        "incidents": [],
    }})


def fastly(status_text="Operational", unresolved=(), upcoming=()):
    return json.dumps({
        "PageName": "Fastly | Service Status",
        "Domain": "fastly",
        "StatusInEffectSince": "2026-09-14T17:56:00",
        "StatusText": status_text,
        "Status": status_text,
        "UnresolvedIncidents": [
            {"Id": i, "IncidentType": t, "Status": "InProgress", "Title": n,
             "StartDate": "2026-10-23T02:20:00", "ShortUrl": "https://sta.ac/u/x"}
            for i, (n, t) in enumerate(unresolved)
        ],
        "UpcomingIncidents": [
            {"Id": 900 + i, "IncidentType": "ScheduledMaintenance", "Status": "Future",
             "Title": n, "StartDate": "2026-11-12T21:00:00", "ShortUrl": "https://sta.ac/u/y"}
            for i, n in enumerate(upcoming)
        ],
    })


def gcp(open_incident=True):
    items = [{
        "id": "abc", "external_desc": "Elevated error rates in Cloud Storage",
        "begin": "2026-11-12T09:00:00Z", "status_impact": "SERVICE_DISRUPTION",
        "affected_products": [{"title": "Cloud Storage"}, {"title": "BigQuery"}],
        "uri": "/incidents/abc",
    }]
    if not open_incident:
        items[0]["end"] = "2026-11-12T10:00:00Z"
    return json.dumps(items)


def azure_rss(outage=True):
    body = (
        '<?xml version="1.0" encoding="utf-8"?><rss version="2.0"><channel>'
        "<title>Azure Status</title>"
    )
    if outage:
        body += (
            "<item><title>Virtual Machines - West Europe - Service Outage</title>"
            "<description>Customers are unable to create VMs.</description>"
            "<pubDate>Thu, 12 Nov 2026 09:10:00 GMT</pubDate>"
            "<link>https://azure.status.microsoft/status</link></item>"
        )
    body += (
        "<item><title>Storage - East US - Resolved</title>"
        "<description>This issue has been mitigated.</description>"
        "<pubDate>Wed, 11 Nov 2026 22:00:00 GMT</pubDate></item>"
        "</channel></rss>"
    )
    return body


def aws(events=True):
    if not events:
        return json.dumps([])
    return json.dumps([{
        "service_name": "EC2", "region": "us-east-1",
        "event_log_summary": "Increased API error rates",
        "start_time": "2026-11-12T07:40:00Z",
    }])


# ---------------------------------------------------------------------------

PASS_ONE = {
    "cloudflare": statuspage("Cloudflare", "none", "All Systems Operational"),
    "github":     statuspage("GitHub", "minor", "Minor Service Outage",
                             components=[("Actions", "degraded_performance")],
                             incidents=[("Delays in Actions queue", "investigating", "minor")]),
    "openai":     statuspage("OpenAI", "critical", "Major Service Outage",
                             components=[("API", "major_outage"), ("ChatGPT", "partial_outage")],
                             incidents=[("API unavailable", "identified", "critical")]),
    "notion":     statuspage("Notion", "maintenance", "Scheduled Maintenance",
                             maint=[("Database upgrade", "in_progress")]),
    "anthropic":  statuspage("Anthropic", "none", "All Systems Operational"),
    "gcp":        gcp(open_incident=True),
    "azure":      azure_rss(outage=True),
    "aws":        aws(events=True),
    "demo-instatus": instatus("HASISSUES", [("Search latency in EU", "PARTIALOUTAGE")]),
    "gitlab":     status_io(500, [("Git Operations", 500), ("Website", 100)]),
    # Fastly's real-world trap: headline says "Maintenance" purely because
    # long-running informational notices are open. That must NOT read as broken.
    "fastly":     fastly("Maintenance",
                         unresolved=[("Domainr TLD status quirk", "Informational")],
                         upcoming=["Nairobi (NBO) New Data Center"]),
}

# Second pass: OpenAI recovers, Cloudflare breaks, everything else unchanged.
PASS_TWO = dict(PASS_ONE)
PASS_TWO["openai"] = statuspage("OpenAI", "none", "All Systems Operational")
PASS_TWO["cloudflare"] = statuspage("Cloudflare", "major", "Partial System Outage",
                                    components=[("CDN", "partial_outage")],
                                    incidents=[("Elevated 5xx in EU", "monitoring", "major")])

EXPECT_ONE = {
    "cloudflare": collector.OPERATIONAL,
    "github":     collector.DEGRADED,
    "openai":     collector.MAJOR,
    "notion":     collector.MAINTENANCE,
    "anthropic":  collector.OPERATIONAL,
    "gcp":        collector.PARTIAL,
    "azure":      collector.MAJOR,
    "aws":        collector.DEGRADED,
    "demo-instatus": collector.PARTIAL,
    "gitlab":     collector.MAJOR,
    "fastly":     collector.MAINTENANCE,
}

EXPECT_TWO = dict(EXPECT_ONE)
EXPECT_TWO["openai"] = collector.OPERATIONAL
EXPECT_TWO["cloudflare"] = collector.PARTIAL


def write_fixtures(data: dict) -> None:
    os.makedirs(FIXTURES, exist_ok=True)
    for slug, body in data.items():
        with open(os.path.join(FIXTURES, f"{slug}.json"), "w", encoding="utf-8") as fh:
            fh.write(body)


def main() -> int:
    # Fresh database so the test is repeatable.
    for suffix in ("", "-wal", "-shm"):
        p = collector.DB_PATH + suffix
        if os.path.exists(p):
            os.remove(p)

    all_vendors = collector.load_vendors()
    vendors = [dict(v) for v in all_vendors if v["slug"] in PASS_ONE]

    # A synthetic vendor so the Instatus adapter is covered even though no
    # vendor in the shipped list currently uses that platform.
    vendors.append({
        "slug": "demo-instatus", "name": "Demo Instatus", "kind": "instatus",
        "url": "https://demo.instatus.com", "tags": [],
    })
    missing = set(PASS_ONE) - {v["slug"] for v in vendors}
    if missing:
        print(f"FAIL: fixtures reference slugs not in vendors.json: {sorted(missing)}")
        return 1

    conn = collector.db_connect()
    failures = []

    for label, data, expect in (("pass 1", PASS_ONE, EXPECT_ONE),
                                ("pass 2", PASS_TWO, EXPECT_TWO)):
        write_fixtures(data)
        results = collector.run_once(vendors, conn, {}, offline=True)
        got = {r["slug"]: r["level"] for r in results}
        print(f"\n--- {label} ---")
        for slug in sorted(expect):
            ok = got.get(slug) == expect[slug]
            print(f"  {'ok ' if ok else 'FAIL'}  {slug:<12} {got.get(slug):<15} "
                  f"{'' if ok else '(expected ' + expect[slug] + ')'}")
            if not ok:
                failures.append(f"{label}:{slug}")
            headline = next(r["headline"] for r in results if r["slug"] == slug)
            print(f"           -> {headline}")

    # Transitions: 8 first-sighting rows in pass 1, exactly 2 changes in pass 2.
    rows = conn.execute("SELECT slug, from_level, to_level FROM events ORDER BY id").fetchall()
    first = [r for r in rows if r["from_level"] is None]
    changes = [r for r in rows if r["from_level"] is not None]
    print(f"\n--- transitions ---")
    print(f"  first sightings: {len(first)} (expect {len(PASS_ONE)})")
    for r in changes:
        print(f"  change: {r['slug']} {r['from_level']} -> {r['to_level']}")
    if len(first) != len(PASS_ONE):
        failures.append("first-sighting count")
    if len(changes) != 2:
        failures.append(f"expected 2 changes, got {len(changes)}")

    conn.close()
    shutil.rmtree(FIXTURES, ignore_errors=True)

    print()
    if failures:
        print(f"SELF-TEST FAILED: {failures}")
        return 1
    print("SELF-TEST PASSED -- all adapters parse and normalise correctly.")
    print("Database left in place so you can start the web app:  python app.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
