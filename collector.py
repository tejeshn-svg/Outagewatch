#!/usr/bin/env python3
"""
OutageWatch -- collector
========================

Polls the official status feeds of many vendors, normalises every vendor's
private vocabulary into ONE set of status levels, stores the result in SQLite,
and records a transition every time a vendor changes state.

Why normalise? Every vendor invents its own words. Atlassian Statuspage says
"degraded_performance", Google Cloud says "SERVICE_DISRUPTION", Azure says
"Advisory". A status aggregator is 10% fetching and 90% translating all of that
into the same five words so you can show them side by side.

Run it:
    python collector.py --once          # one pass, then exit
    python collector.py                 # poll forever (default every 120s)
    python collector.py --offline       # use fixtures/, no network (for testing)
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "outagewatch.db")
VENDORS_PATH = os.path.join(HERE, "vendors.json")
CONFIG_PATH = os.path.join(HERE, "config.json")
FIXTURES_DIR = os.path.join(HERE, "fixtures")
ALERT_LOG = os.path.join(HERE, "alerts.log")

# Some vendors sit behind bot protection that rejects unfamiliar User-Agents
# with HTTP 403. Presenting as an ordinary browser is what everyone does here;
# we still make one polite request every couple of minutes.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
TIMEOUT = 15
WORKERS = 12

# ---------------------------------------------------------------------------
# The five levels. Everything on earth gets mapped into one of these.
# ---------------------------------------------------------------------------
OPERATIONAL = "operational"
MAINTENANCE = "maintenance"
DEGRADED = "degraded"
PARTIAL = "partial_outage"
MAJOR = "major_outage"
UNKNOWN = "unknown"

# How bad is each one? Used for sorting worst-first and for "is this worse?".
SEVERITY = {
    OPERATIONAL: 0,
    MAINTENANCE: 1,
    UNKNOWN: 2,
    DEGRADED: 3,
    PARTIAL: 4,
    MAJOR: 5,
}

LABELS = {
    OPERATIONAL: "Operational",
    MAINTENANCE: "Maintenance",
    DEGRADED: "Degraded Performance",
    PARTIAL: "Partial Outage",
    MAJOR: "Service Outage",
    UNKNOWN: "Unknown",
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ===========================================================================
# CORRELATION
# ---------------------------------------------------------------------------
# The idea: a company is slow to admit its own outage, but has no reason to
# hide somebody else's. When several unrelated vendors all blame the same third
# party at the same time, that is strong evidence -- often arriving before the
# third party's own status page says a word, and sometimes for a service we
# cannot monitor directly at all.
#
# This is crowdsourcing without a crowd. The reporters are other companies'
# engineering teams, already writing honest incident notes for their own
# customers. Nobody reads them sideways.
# ===========================================================================

import re  # noqa: E402  (kept beside the code that uses it)

# Third parties worth watching for. Patterns are matched case-insensitively
# against each vendor's headline and open incident titles.
ENTITY_PATTERNS = {
    "Microsoft": [r"\bmicrosoft\b", r"\boutlook\b", r"\boffice ?365\b", r"\bm365\b",
                  r"\bintune\b", r"\bexchange online\b", r"\bazure\b", r"\bms teams\b",
                  r"\bsharepoint\b", r"\bentra\b"],
    "Amazon Web Services": [r"\baws\b", r"\bamazon web services\b", r"\bec2\b",
                            r"\bs3\b", r"\bus-east-1\b"],
    "Google Cloud": [r"\bgoogle cloud\b", r"\bgcp\b", r"\bgoogle workspace\b", r"\bgmail\b"],
    "Cloudflare": [r"\bcloudflare\b"],
    "Okta": [r"\bokta\b"],
    "Slack": [r"\bslack\b"],
    "Twilio": [r"\btwilio\b"],
    "GitHub": [r"\bgithub\b"],
    "Stripe": [r"\bstripe\b"],
    "Salesforce": [r"\bsalesforce\b"],
    "Zoom": [r"\bzoom\b"],
}

# Vendors that are really the same company, or that republish each other's
# incidents. They must count as ONE independent voice, not several.
# (SendGrid is Twilio; Azure is Microsoft.)
VENDOR_GROUP = {
    "sendgrid": "twilio",
    "twilio": "twilio",
    "azure": "microsoft",
    "microsoft-365": "microsoft",
    "power-platform": "microsoft",
    "aws": "amazon",
    "gcp": "google",
}

# Which vendor group each entity belongs to, so a company blaming itself
# doesn't count as corroboration.
ENTITY_OWNER = {
    "Microsoft": "microsoft",
    "Amazon Web Services": "amazon",
    "Google Cloud": "google",
    "Cloudflare": "cloudflare",
    "Twilio": "twilio",
    "Zoom": "zoom",
    "GitHub": "github",
}

# How many independent vendor groups must agree before we call it.
CORRELATION_THRESHOLD = 3


def group_of(slug: str) -> str:
    return VENDOR_GROUP.get(slug, slug)


def find_correlations(results: list, threshold: int = CORRELATION_THRESHOLD) -> list:
    """Which third parties are several independent vendors complaining about?"""
    hits = {}

    for r in results:
        if r["level"] in (OPERATIONAL, UNKNOWN):
            continue  # a healthy vendor is not reporting anything

        text = " ".join([
            r.get("headline", "") or "",
            " ".join(str(i.get("title", "")) for i in r.get("incidents", [])),
            " ".join(str(i.get("impact", "")) for i in r.get("incidents", [])),
        ]).lower()

        for entity, patterns in ENTITY_PATTERNS.items():
            if group_of(r["slug"]) == ENTITY_OWNER.get(entity):
                continue  # don't let a company corroborate itself
            if any(re.search(p, text) for p in patterns):
                hits.setdefault(entity, {}).setdefault(group_of(r["slug"]), []).append(r["name"])

    out = []
    for entity, groups in hits.items():
        if len(groups) >= threshold:
            # Affiliated vendors count as one voice but are both named, so the
            # reader can see exactly who is reporting.
            names = sorted("/".join(sorted(v)) for v in groups.values())
            out.append({
                "entity": entity,
                "groups": len(groups),
                "vendors": names,
            })
    out.sort(key=lambda c: -c["groups"])
    return out


def worse(a: str, b: str) -> str:
    """Return whichever of the two levels is more severe."""
    return a if SEVERITY.get(a, 2) >= SEVERITY.get(b, 2) else b


# ===========================================================================
# ADAPTERS
# ---------------------------------------------------------------------------
# One function per vendor family. Each takes the vendor dict and the raw
# response text, and returns (level, headline, incidents_list).
# To support a new vendor family, write one of these and register it in
# ADAPTERS at the bottom of this section. Nothing else changes.
# ===========================================================================

# --- Atlassian Statuspage --------------------------------------------------
# This is the big one. A large share of well-known services run on Atlassian
# Statuspage, and every one of those pages exposes the same public JSON at
# /api/v2/summary.json -- no API key, no scraping. Learn this one shape and you
# can monitor hundreds of vendors with a single adapter.

STATUSPAGE_INDICATOR = {
    "none": OPERATIONAL,
    "minor": DEGRADED,
    "major": PARTIAL,
    "critical": MAJOR,
    "maintenance": MAINTENANCE,
}

STATUSPAGE_COMPONENT = {
    "operational": OPERATIONAL,
    "degraded_performance": DEGRADED,
    "partial_outage": PARTIAL,
    "major_outage": MAJOR,
    "under_maintenance": MAINTENANCE,
}


def adapter_statuspage(vendor: dict, body: str):
    data = json.loads(body)

    # 1. The page's own overall verdict.
    indicator = (data.get("status") or {}).get("indicator", "none")
    level = STATUSPAGE_INDICATOR.get(indicator, UNKNOWN)
    description = (data.get("status") or {}).get("description") or ""

    # 2. Component-level detail. Sometimes the overall indicator lags behind
    #    the components, so take whichever is worse.
    broken = []
    for comp in data.get("components") or []:
        if comp.get("group"):
            continue  # group headers duplicate their children
        clevel = STATUSPAGE_COMPONENT.get(comp.get("status", ""), UNKNOWN)
        if clevel != OPERATIONAL and clevel != UNKNOWN:
            broken.append((comp.get("name", "?"), clevel))
            level = worse(level, clevel)

    # 3. Open incidents -- the human-readable "what is actually wrong".
    incidents = []
    for inc in data.get("incidents") or []:
        if inc.get("status") in ("resolved", "postmortem"):
            continue
        incidents.append({
            "title": inc.get("name", "Incident"),
            "impact": inc.get("impact", ""),
            "started": inc.get("created_at", ""),
            "url": inc.get("shortlink") or vendor.get("url", ""),
        })

    # 4. Scheduled maintenance that is happening right now.
    for m in data.get("scheduled_maintenances") or []:
        if m.get("status") == "in_progress":
            level = worse(level, MAINTENANCE)
            incidents.append({
                "title": m.get("name", "Scheduled maintenance"),
                "impact": "maintenance",
                "started": m.get("scheduled_for", ""),
                "url": vendor.get("url", ""),
            })

    headline = _headline(level, description, broken, incidents)
    return level, headline, incidents


def _headline(level, description, broken, incidents):
    """One short line describing the situation, like the reference site shows."""
    if incidents:
        n = len(incidents)
        first = incidents[0]["title"]
        return first if n == 1 else f"{first} (+{n - 1} more)"
    if broken:
        names = ", ".join(n for n, _ in broken[:3])
        extra = f" +{len(broken) - 3}" if len(broken) > 3 else ""
        return f"{len(broken)} component(s) affected: {names}{extra}"
    if description:
        return description
    return LABELS.get(level, "")


# --- Google Cloud ----------------------------------------------------------
# Publishes a plain JSON array of incidents. An incident with no "end" key is
# still open.

def adapter_gcp(vendor: dict, body: str):
    data = json.loads(body)
    level, incidents = OPERATIONAL, []
    for inc in data:
        if inc.get("end"):
            continue
        impact = (inc.get("status_impact") or "").upper()
        if impact == "SERVICE_OUTAGE":
            level = worse(level, MAJOR)
        elif impact == "SERVICE_DISRUPTION":
            level = worse(level, PARTIAL)
        else:
            level = worse(level, DEGRADED)
        products = ", ".join(
            p.get("title", "") for p in (inc.get("affected_products") or [])[:3]
        )
        incidents.append({
            "title": inc.get("external_desc", "Incident")[:160],
            "impact": products or impact,
            "started": inc.get("begin", ""),
            "url": "https://status.cloud.google.com" + (inc.get("uri") or ""),
        })
    return level, _headline(level, "All Systems Operational", [], incidents), incidents


# --- Microsoft (RSS) -------------------------------------------------------
# Microsoft doesn't publish Statuspage-style JSON, but it does publish RSS,
# in more than one place:
#
#   azure.status.microsoft/en-us/status/feed/   -- Azure, the cloud platform
#   status.cloud.microsoft/api/feed/mac         -- Microsoft 365 admin centre
#   status.cloud.microsoft/api/feed/ppac        -- Power Platform admin centre
#
# The M365 one isn't documented anywhere; it was found by reading the status
# page's own JavaScript to see what it fetches. It needs no login.
#
# Parsed with the standard library -- no extra package needed.

def adapter_azure_rss(vendor: dict, body: str):
    root = ET.fromstring(body)
    items = root.findall(".//item")
    level, incidents = OPERATIONAL, []
    for item in items[:12]:
        title = (item.findtext("title") or "").strip()
        desc = (item.findtext("description") or "").strip()
        pub = (item.findtext("pubDate") or "").strip()
        blob = f"{title} {desc}".lower()
        if not title:
            continue
        if any(k in blob for k in ("resolved", "mitigated", "post incident")):
            continue
        if "maintenance" in blob:
            lv = MAINTENANCE
        elif any(k in blob for k in ("outage", "unavailable", "unable to")):
            lv = MAJOR
        elif any(k in blob for k in ("degraded", "latency", "intermittent", "slow")):
            lv = DEGRADED
        elif "advisory" in blob:
            lv = DEGRADED
        else:
            continue
        level = worse(level, lv)
        incidents.append({
            "title": title[:160],
            "impact": "",
            "started": pub,
            "url": (item.findtext("link") or vendor.get("url", "")),
        })
    return level, _headline(level, "No active events", [], incidents), incidents


# --- AWS -------------------------------------------------------------------
def adapter_aws(vendor: dict, body: str):
    data = json.loads(body)
    events = data if isinstance(data, list) else data.get("events", []) or []
    level, incidents = OPERATIONAL, []
    for ev in events:
        if not isinstance(ev, dict):
            continue
        summary = str(ev.get("event_log_summary") or ev.get("summary") or "Event")
        svc = str(ev.get("service_name") or ev.get("service") or "")
        blob = f"{summary} {svc}".lower()
        if "resolved" in blob or "operating normally" in blob:
            continue
        lv = MAJOR if "outage" in blob else DEGRADED
        level = worse(level, lv)
        incidents.append({
            "title": (f"{svc}: " if svc else "") + summary[:140],
            "impact": str(ev.get("region") or ""),
            "started": str(ev.get("start_time") or ""),
            "url": vendor.get("url", ""),
        })
    return level, _headline(level, "No events reported", [], incidents), incidents


# --- Plain HTTP probe ------------------------------------------------------
# Fallback for a vendor with no machine-readable feed at all: just ask the
# service's own front door whether it answers. Crude, but it is exactly what
# "is it down for everyone or just me" needs, and it catches outages the
# vendor has not admitted to yet.

def adapter_probe(vendor: dict, body: str):
    # body is unused: status was decided by the HTTP code in fetch_one().
    return OPERATIONAL, "Responding normally", []


# --- Instatus --------------------------------------------------------------
# The second-most-common hosted status page. Same idea as Statuspage, different
# words: every page serves /summary.json.

INSTATUS_IMPACT = {
    "OPERATIONAL": OPERATIONAL,
    "DEGRADEDPERFORMANCE": DEGRADED,
    "PARTIALOUTAGE": PARTIAL,
    "MAJOROUTAGE": MAJOR,
    "UNDERMAINTENANCE": MAINTENANCE,
}


def adapter_instatus(vendor: dict, body: str):
    data = json.loads(body)
    page_status = ((data.get("page") or {}).get("status") or "").upper()
    level = {"UP": OPERATIONAL, "HASISSUES": DEGRADED,
             "UNDERMAINTENANCE": MAINTENANCE}.get(page_status, UNKNOWN)

    incidents = []
    for inc in data.get("activeIncidents") or []:
        level = worse(level, INSTATUS_IMPACT.get((inc.get("impact") or "").upper(), DEGRADED))
        incidents.append({
            "title": inc.get("name", "Incident"),
            "impact": inc.get("impact", ""),
            "started": inc.get("started", ""),
            "url": inc.get("url") or vendor.get("url", ""),
        })
    for m in data.get("activeMaintenances") or []:
        level = worse(level, MAINTENANCE)
        incidents.append({
            "title": m.get("name", "Scheduled maintenance"),
            "impact": "maintenance",
            "started": m.get("start", ""),
            "url": m.get("url") or vendor.get("url", ""),
        })

    desc = "All Systems Operational" if level == OPERATIONAL else ""
    return level, _headline(level, desc, [], incidents), incidents


# --- Status.io -------------------------------------------------------------
# Used by GitLab among others. Numeric status codes rather than words.
# The vendor url for this kind is the API endpoint itself:
#   https://api.status.io/1.0/status/<page id>

STATUS_IO_CODE = {
    100: OPERATIONAL,   # Operational
    200: MAINTENANCE,   # Planned maintenance
    300: DEGRADED,      # Degraded performance
    400: PARTIAL,       # Partial service disruption
    500: MAJOR,         # Service disruption
    600: PARTIAL,       # Security event
}


def adapter_status_io(vendor: dict, body: str):
    result = json.loads(body).get("result") or {}
    overall = result.get("status_overall") or {}
    level = STATUS_IO_CODE.get(overall.get("status_code"), UNKNOWN)

    broken = []
    for comp in result.get("status") or []:
        clevel = STATUS_IO_CODE.get(comp.get("status_code"), UNKNOWN)
        if clevel not in (OPERATIONAL, UNKNOWN):
            broken.append((comp.get("name", "?"), clevel))
            level = worse(level, clevel)

    incidents = []
    for inc in result.get("incidents") or []:
        incidents.append({
            "title": inc.get("name", "Incident"),
            "impact": "",
            "started": inc.get("datetime_open", ""),
            "url": vendor.get("url", ""),
        })

    return level, _headline(level, overall.get("status", ""), broken, incidents), incidents


# --- Fastly ----------------------------------------------------------------
# Fastly rolled their own. Their /summary.json uses capitalised field names and
# their own words, and -- importantly -- it splits incidents into unresolved
# (happening now) and upcoming (scheduled, not yet started).
#
# A wrinkle worth knowing: Fastly leaves long-running "Informational" notices
# open for months, which drags their headline status to "Maintenance". Those
# must not be reported as breakage, or Fastly looks permanently broken.

FASTLY_WORDS = [
    ("operational", OPERATIONAL),
    ("all systems", OPERATIONAL),
    ("normal", OPERATIONAL),
    ("maintenance", MAINTENANCE),
    ("degraded", DEGRADED),
    ("partial", PARTIAL),
    ("disrupt", PARTIAL),
    ("outage", MAJOR),
    ("major", MAJOR),
    ("unavailable", MAJOR),
]

FASTLY_QUIET_TYPES = {"informational", "scheduledmaintenance"}


def adapter_fastly(vendor: dict, body: str):
    data = json.loads(body)
    text = str(data.get("StatusText") or data.get("Status") or "")
    low = text.lower()

    level = UNKNOWN
    for word, lv in FASTLY_WORDS:
        if word in low:
            level = lv
            break

    incidents = []
    for inc in data.get("UnresolvedIncidents") or []:
        if str(inc.get("Status", "")).lower() in ("resolved", "completed"):
            continue
        itype = str(inc.get("IncidentType", "")).lower()
        incidents.append({
            "title": str(inc.get("Title", "Incident"))[:160],
            "impact": str(inc.get("IncidentType", "")),
            "started": str(inc.get("StartDate", "")),
            "url": inc.get("ShortUrl") or vendor.get("url", ""),
        })
        # Only genuine service problems raise the level; notices do not.
        if itype not in FASTLY_QUIET_TYPES:
            level = worse(level, DEGRADED)

    # Scheduled work that has not begun is listed but changes nothing today.
    for m in data.get("UpcomingIncidents") or []:
        incidents.append({
            "title": "Upcoming: " + str(m.get("Title", "Scheduled maintenance"))[:150],
            "impact": "scheduled",
            "started": str(m.get("StartDate", "")),
            "url": m.get("ShortUrl") or vendor.get("url", ""),
        })

    if level == UNKNOWN:
        level = DEGRADED if any(
            str(i.get("IncidentType", "")).lower() not in FASTLY_QUIET_TYPES
            for i in (data.get("UnresolvedIncidents") or [])
        ) else OPERATIONAL

    return level, _headline(level, text, [], incidents), incidents


# --- Microsoft 365 / Power Platform (status.cloud.microsoft RSS) -----------
# Unlike the Azure feed, this one carries a machine-readable <status> element
# per item, so there is no need to guess severity from prose. The <title> is
# only the service name ("Microsoft Admin Center"); the real description lives
# in <description> as escaped HTML with labelled sections:
#
#   User impact: ...   More info: ...   Current status: ...
#   Scope of impact: ...   Next update by: ...
#
# "User impact" is the one-line human summary, so that becomes the headline.

MS_STATUS = {
    "serviceoperational": OPERATIONAL,
    "servicerestored": OPERATIONAL,
    "postincidentreportpublished": OPERATIONAL,
    "pirpublished": OPERATIONAL,
    "falsepositive": OPERATIONAL,
    "resolved": OPERATIONAL,
    "investigating": DEGRADED,
    "servicedegradation": DEGRADED,
    "restoringservice": DEGRADED,
    "verifyingservice": DEGRADED,
    "extendedrecovery": DEGRADED,
    "advisory": DEGRADED,
    "information": DEGRADED,
    "serviceinterruption": MAJOR,
    "informationunavailable": UNKNOWN,
}

_TAGS = re.compile(r"<[^>]+>")
_SECTIONS = ("More info:", "Current status:", "Previous status:",
             "Scope of impact:", "Next update by:", "Final status:")


def _strip_html(raw: str) -> str:
    text = _TAGS.sub(" ", raw or "")
    for entity, char in (("&lt;", "<"), ("&gt;", ">"), ("&amp;", "&"),
                         ("&quot;", '"'), ("&#39;", "'"), ("&nbsp;", " ")):
        text = text.replace(entity, char)
    return " ".join(text.split())


def _user_impact(description: str) -> str:
    """The 'User impact:' sentence, which is the useful one-line summary."""
    text = _strip_html(description)
    if "User impact:" not in text:
        return text[:160]
    after = text.split("User impact:", 1)[1]
    cut = len(after)
    for marker in _SECTIONS:
        if marker in after:
            cut = min(cut, after.index(marker))
    return after[:cut].strip(" .") or text[:160]


def adapter_ms_rss(vendor: dict, body: str):
    root = ET.fromstring(body)
    level, incidents = OPERATIONAL, []

    for item in root.findall(".//item"):
        raw_status = (item.findtext("status") or "").strip().lower()
        item_level = MS_STATUS.get(raw_status.replace(" ", ""), UNKNOWN)
        if item_level in (OPERATIONAL, UNKNOWN):
            continue

        service = (item.findtext("title") or "Service").strip()
        impact = _user_impact(item.findtext("description") or "")
        level = worse(level, item_level)
        incidents.append({
            "title": impact[:200] or service,
            "impact": (item.findtext("status") or "").strip(),
            "started": (item.findtext("pubDate") or "").strip(),
            "url": (item.findtext("link") or vendor.get("url", "")),
        })

    desc = "No active events" if level == OPERATIONAL else ""
    return level, _headline(level, desc, [], incidents), incidents


ADAPTERS = {
    "statuspage": adapter_statuspage,
    "instatus": adapter_instatus,
    "status_io": adapter_status_io,
    "fastly": adapter_fastly,
    "gcp": adapter_gcp,
    "azure_rss": adapter_azure_rss,
    "ms_rss": adapter_ms_rss,
    "aws": adapter_aws,
    "probe": adapter_probe,
}


def feed_url(vendor: dict) -> str:
    """Where to actually fetch from, derived from the vendor's kind."""
    kind, url = vendor["kind"], vendor["url"].rstrip("/")
    if kind == "statuspage":
        return f"{url}/api/v2/summary.json"
    if kind in ("instatus", "fastly"):
        return f"{url}/summary.json"
    return url


# ===========================================================================
# FETCHING
# ===========================================================================

def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT, "Accept": "*/*"})
    return s


def fetch_one(session, vendor: dict, offline: bool = False) -> dict:
    """Fetch and normalise one vendor. Never raises -- failure is a result."""
    slug = vendor["slug"]
    result = {
        "slug": slug,
        "name": vendor["name"],
        "url": vendor["url"],
        "kind": vendor["kind"],
        "tags": ",".join(vendor.get("tags", [])),
        "level": UNKNOWN,
        "headline": "",
        "incidents": [],
        "checked_at": now_iso(),
    }
    try:
        if offline:
            path = os.path.join(FIXTURES_DIR, f"{slug}.json")
            if not os.path.exists(path):
                result["headline"] = "No fixture"
                return result
            with open(path, "r", encoding="utf-8") as fh:
                body = fh.read()
            code = 200
        else:
            resp = session.get(feed_url(vendor), timeout=TIMEOUT)
            code, body = resp.status_code, resp.text

        if vendor["kind"] == "probe":
            if code >= 500:
                result["level"], result["headline"] = MAJOR, f"HTTP {code} from origin"
            elif code >= 400:
                result["level"], result["headline"] = DEGRADED, f"HTTP {code} from origin"
            else:
                result["level"], result["headline"] = OPERATIONAL, "Responding normally"
            return result

        if code != 200:
            result["headline"] = f"Status feed returned HTTP {code}"
            return result

        level, headline, incidents = ADAPTERS[vendor["kind"]](vendor, body)
        result["level"], result["headline"], result["incidents"] = level, headline, incidents
    except requests.exceptions.RequestException as exc:
        result["headline"] = f"Could not reach status feed ({type(exc).__name__})"
    except (ValueError, ET.ParseError) as exc:
        result["headline"] = f"Could not parse status feed ({type(exc).__name__})"
    return result


def fetch_all(vendors: list, offline: bool = False) -> list:
    session = make_session()
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        return list(pool.map(lambda v: fetch_one(session, v, offline), vendors))


# ===========================================================================
# STORAGE
# ---------------------------------------------------------------------------
# Two tables only.
#   current -- one row per vendor, overwritten every poll (what the page shows)
#   events  -- one row per CHANGE of state (history, uptime, alerts)
# Recording only changes rather than every sample keeps the database tiny:
# 84 vendors polled every 2 minutes would be 60,000 rows a day if you logged
# every poll, but only a handful of rows a day if you log transitions.
# ===========================================================================

SCHEMA = """
CREATE TABLE IF NOT EXISTS current (
    slug       TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    url        TEXT,
    kind       TEXT,
    tags       TEXT,
    level      TEXT NOT NULL,
    headline   TEXT,
    incidents  TEXT,
    checked_at TEXT,
    changed_at TEXT
);
CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    slug       TEXT NOT NULL,
    name       TEXT,
    from_level TEXT,
    to_level   TEXT NOT NULL,
    headline   TEXT,
    at         TEXT NOT NULL,
    notified   INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_events_at ON events(at);
CREATE INDEX IF NOT EXISTS idx_events_slug ON events(slug);
CREATE TABLE IF NOT EXISTS correlations (
    entity     TEXT PRIMARY KEY,
    vendors    TEXT NOT NULL,
    groups     INTEGER NOT NULL,
    first_seen TEXT NOT NULL,
    last_seen  TEXT NOT NULL
);
"""


def db_connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    return conn


def save(conn: sqlite3.Connection, results: list) -> list:
    """Write results, return the list of state changes that just happened."""
    changes = []
    ts = now_iso()
    for r in results:
        row = conn.execute("SELECT level FROM current WHERE slug=?", (r["slug"],)).fetchone()
        previous = row["level"] if row else None

        if previous != r["level"]:
            changes.append({
                "slug": r["slug"], "name": r["name"],
                "from": previous, "to": r["level"], "headline": r["headline"],
            })
            conn.execute(
                "INSERT INTO events (slug,name,from_level,to_level,headline,at) VALUES (?,?,?,?,?,?)",
                (r["slug"], r["name"], previous, r["level"], r["headline"], ts),
            )
            changed_at = ts
        else:
            changed_at = None

        conn.execute(
            """INSERT INTO current (slug,name,url,kind,tags,level,headline,incidents,checked_at,changed_at)
               VALUES (?,?,?,?,?,?,?,?,?,COALESCE(?, (SELECT changed_at FROM current WHERE slug=?), ?))
               ON CONFLICT(slug) DO UPDATE SET
                 name=excluded.name, url=excluded.url, kind=excluded.kind, tags=excluded.tags,
                 level=excluded.level, headline=excluded.headline, incidents=excluded.incidents,
                 checked_at=excluded.checked_at, changed_at=excluded.changed_at""",
            (r["slug"], r["name"], r["url"], r["kind"], r["tags"], r["level"],
             r["headline"], json.dumps(r["incidents"]), r["checked_at"],
             changed_at, r["slug"], ts),
        )
    conn.commit()
    return changes


# ===========================================================================
# ALERTS
# ---------------------------------------------------------------------------
# Only fires on a CHANGE, which is the whole point -- nobody wants a message
# every two minutes saying GitHub is still fine. Telegram is used here because
# it is free and takes two minutes to set up; see README for how to get a token.
# ===========================================================================

def save_correlations(conn: sqlite3.Connection, found: list) -> list:
    """Store current correlations; return the ones that are newly appearing."""
    ts = now_iso()
    current = {c["entity"]: c for c in found}

    known = {r["entity"] for r in conn.execute("SELECT entity FROM correlations")}
    new = []

    for entity, c in current.items():
        if entity not in known:
            new.append(c)
            conn.execute(
                "INSERT INTO correlations (entity,vendors,groups,first_seen,last_seen)"
                " VALUES (?,?,?,?,?)",
                (entity, json.dumps(c["vendors"]), c["groups"], ts, ts),
            )
        else:
            conn.execute(
                "UPDATE correlations SET vendors=?, groups=?, last_seen=? WHERE entity=?",
                (json.dumps(c["vendors"]), c["groups"], ts, entity),
            )

    for entity in known - set(current):
        conn.execute("DELETE FROM correlations WHERE entity=?", (entity,))

    conn.commit()
    return new


def notify_correlations(new: list, config: dict) -> None:
    if not new:
        return
    lines = []
    for c in new:
        lines.append(
            f"[CORRELATED] {c['entity']} may be having problems -- "
            f"{c['groups']} independent services report it: {', '.join(c['vendors'])}"
        )
    message = "\n".join(lines)

    with open(ALERT_LOG, "a", encoding="utf-8") as fh:
        fh.write(f"{now_iso()}\n{message}\n\n")
    print(message, flush=True)

    send_telegram(config, message)


CHANNEL_PATH = os.path.join(HERE, "channel.json")


def alert_targets(config: dict) -> list:
    """Everywhere an alert should go.

    Two kinds of destination, and they are not the same thing:

      * a chat id  -- your own private conversation with the bot
      * a channel  -- a public Telegram channel the bot posts to, which other
                      people can join without you managing anything

    The channel name lives in channel.json, in the repository, because a public
    channel's @name is public by definition. The token stays a secret; it is
    the thing that must never be shared.
    """
    targets = []
    if config.get("telegram_chat_id"):
        targets.append(str(config["telegram_chat_id"]))
    channel = config.get("telegram_channel")
    if channel:
        channel = channel.strip()
        if channel and not channel.startswith(("@", "-")):
            channel = "@" + channel
        targets.append(channel)
    # Same place twice would mean the same message twice.
    seen, unique = set(), []
    for t in targets:
        if t and t not in seen:
            seen.add(t)
            unique.append(t)
    return unique


def send_telegram(config: dict, message: str) -> None:
    token = config.get("telegram_bot_token")
    targets = alert_targets(config)
    if not token or not targets:
        return
    for target in targets:
        try:
            resp = requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": target, "text": "OutageWatch\n" + message},
                timeout=TIMEOUT,
            )
            body = resp.json()
            if not body.get("ok"):
                print(f"  (telegram refused {target}: {body.get('description')})",
                      file=sys.stderr, flush=True)
        except (requests.exceptions.RequestException, ValueError) as exc:
            print(f"  (telegram send to {target} failed: {exc})",
                  file=sys.stderr, flush=True)


def load_config() -> dict:
    """config.json on your PC; environment variables when running on GitHub."""
    config = {}
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
            config = json.load(fh)

    # The public channel name is not a secret, so it is committed to the repo.
    if os.path.exists(CHANNEL_PATH):
        try:
            with open(CHANNEL_PATH, "r", encoding="utf-8") as fh:
                config.update(json.load(fh))
        except ValueError:
            pass

    # GitHub Actions has no config.json -- secrets arrive as env vars instead.
    for key, env in (("telegram_bot_token", "TELEGRAM_BOT_TOKEN"),
                     ("telegram_chat_id", "TELEGRAM_CHAT_ID")):
        if os.environ.get(env):
            config[key] = os.environ[env]
    if os.environ.get("ALERT_ON_UNKNOWN", "").lower() in ("1", "true", "yes"):
        config["alert_on_unknown"] = True
    return config


def notify(changes: list, config: dict) -> None:
    if not changes:
        return

    interesting = []
    for c in changes:
        # Ignore the very first observation of a vendor (from=None) and
        # ignore flapping in and out of "unknown" unless you want the noise.
        if c["from"] is None:
            continue
        if UNKNOWN in (c["from"], c["to"]) and not config.get("alert_on_unknown"):
            continue
        interesting.append(c)
    if not interesting:
        return

    lines = []
    for c in interesting:
        arrow = "RECOVERED" if SEVERITY[c["to"]] < SEVERITY[c["from"]] else "DOWN"
        lines.append(f"[{arrow}] {c['name']}: {LABELS[c['from']]} -> {LABELS[c['to']]} -- {c['headline']}")

    message = "\n".join(lines)

    with open(ALERT_LOG, "a", encoding="utf-8") as fh:
        fh.write(f"{now_iso()}\n{message}\n\n")
    print(message, flush=True)

    send_telegram(config, message)


# ===========================================================================
# MAIN
# ===========================================================================

def load_vendors() -> list:
    with open(VENDORS_PATH, "r", encoding="utf-8") as fh:
        vendors = json.load(fh)["vendors"]
    seen = set()
    for v in vendors:
        for key in ("slug", "name", "url", "kind"):
            if key not in v:
                raise SystemExit(f"vendors.json: entry missing '{key}': {v}")
        if v["kind"] not in ADAPTERS:
            raise SystemExit(f"vendors.json: unknown kind '{v['kind']}' for {v['slug']}")
        if v["slug"] in seen:
            raise SystemExit(f"vendors.json: duplicate slug '{v['slug']}'")
        seen.add(v["slug"])
    return vendors


def run_once(vendors, conn, config, offline=False) -> list:
    started = time.time()
    results = fetch_all(vendors, offline=offline)
    changes = save(conn, results)

    # Forget vendors that have been deleted from vendors.json. Without this,
    # a removed vendor keeps its last row forever and sits on the dashboard
    # as a permanent "Unknown" that nothing will ever update.
    slugs = [v["slug"] for v in vendors]
    placeholders = ",".join("?" * len(slugs))
    dropped = conn.execute(
        f"DELETE FROM current WHERE slug NOT IN ({placeholders})", slugs
    ).rowcount
    conn.commit()
    if dropped:
        print(f"  removed {dropped} vendor(s) no longer in vendors.json", flush=True)

    correlations = find_correlations(results)
    new_correlations = save_correlations(conn, correlations)

    counts = {}
    for r in results:
        counts[r["level"]] = counts.get(r["level"], 0) + 1
    issues = sum(n for lv, n in counts.items() if lv not in (OPERATIONAL, MAINTENANCE))
    corr_note = ""
    if correlations:
        corr_note = " | correlated: " + ", ".join(
            f"{c['entity']}({c['groups']})" for c in correlations
        )
    print(
        f"{now_iso()}  checked {len(results)} vendors in {time.time() - started:.1f}s  "
        f"| {issues} with issues | {len(changes)} change(s){corr_note}",
        flush=True,
    )
    notify(changes, config)
    notify_correlations(new_correlations, config)
    return results


def main() -> None:
    ap = argparse.ArgumentParser(description="OutageWatch collector")
    ap.add_argument("--once", action="store_true", help="single pass, then exit")
    ap.add_argument("--interval", type=int, default=120, help="seconds between polls")
    ap.add_argument("--offline", action="store_true", help="read fixtures/ instead of the network")
    args = ap.parse_args()

    vendors = load_vendors()
    config = load_config()
    conn = db_connect()
    print(f"OutageWatch: {len(vendors)} vendors loaded, database at {DB_PATH}")

    if args.once:
        run_once(vendors, conn, config, args.offline)
        return

    print(f"Polling every {args.interval}s. Press Ctrl+C to stop.")
    try:
        while True:
            try:
                run_once(vendors, conn, config, args.offline)
            except Exception as exc:  # never let one bad pass kill the loop
                print(f"poll failed: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
