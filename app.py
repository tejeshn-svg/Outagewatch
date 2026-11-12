#!/usr/bin/env python3
"""
OutageWatch -- web app
======================

Reads the SQLite database the collector writes and serves:

    /                   the dashboard
    /history            transition log + 7-day reliability per vendor
    /api/status.json    your own public JSON API (this is the part other
                        people would pay for -- see README)

The web app never fetches a vendor itself. Collector writes, app reads. Keeping
those two jobs apart means a slow vendor can never make your page slow.

Run:  python app.py      then open http://127.0.0.1:5000
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from flask import Flask, jsonify, render_template, request

from collector import (
    DB_PATH, LABELS, MAINTENANCE, OPERATIONAL, SEVERITY, db_connect,
)

app = Flask(__name__)

# Status palette. Each level ALWAYS travels with an icon and a text label --
# never colour alone, which matters for colour-blind users and for anyone
# glancing at this on a phone in sunlight.
STATUS_STYLE = {
    "operational":    {"color": "#0ca30c", "icon": "●", "label": "Operational"},
    "maintenance":    {"color": "#6b7280", "icon": "■", "label": "Maintenance"},
    "degraded":       {"color": "#fab219", "icon": "▲", "label": "Degraded Performance"},
    "partial_outage": {"color": "#ec835a", "icon": "◆", "label": "Partial Outage"},
    "major_outage":   {"color": "#d03b3b", "icon": "✖", "label": "Service Outage"},
    "unknown":        {"color": "#898781", "icon": "?",      "label": "Unknown"},
}


def fetch_current() -> list:
    if not os.path.exists(DB_PATH):
        return []
    conn = db_connect()
    rows = conn.execute("SELECT * FROM current").fetchall()
    conn.close()

    vendors = []
    for r in rows:
        try:
            incidents = json.loads(r["incidents"] or "[]")
        except ValueError:
            incidents = []
        style = STATUS_STYLE.get(r["level"], STATUS_STYLE["unknown"])
        vendors.append({
            "slug": r["slug"],
            "name": r["name"],
            "url": r["url"],
            "tags": [t for t in (r["tags"] or "").split(",") if t],
            "level": r["level"],
            "label": style["label"],
            "color": style["color"],
            "icon": style["icon"],
            "severity": SEVERITY.get(r["level"], 2),
            "headline": r["headline"] or "",
            "incidents": incidents,
            "checked_at": r["checked_at"],
            "changed_at": r["changed_at"],
            "initial": (r["name"] or "?")[0].upper(),
        })

    # Worst first, then alphabetical -- a status page's whole job is to put the
    # bad news at the top.
    vendors.sort(key=lambda v: (-v["severity"], v["name"].lower()))
    return vendors


def fetch_correlations() -> list:
    """Third parties that several independent vendors are blaming right now."""
    if not os.path.exists(DB_PATH):
        return []
    conn = db_connect()
    rows = conn.execute(
        "SELECT * FROM correlations ORDER BY groups DESC, entity"
    ).fetchall()
    conn.close()
    out = []
    for r in rows:
        try:
            vendors = json.loads(r["vendors"])
        except ValueError:
            vendors = []
        out.append({
            "entity": r["entity"],
            "groups": r["groups"],
            "vendors": vendors,
            "since": r["first_seen"],
        })
    return out


def summarise(vendors: list) -> dict:
    issues = [v for v in vendors if v["level"] not in (OPERATIONAL, MAINTENANCE, "unknown")]
    by_level = {}
    for v in vendors:
        by_level.setdefault(v["level"], []).append(v["name"])
    latest = max((v["checked_at"] or "" for v in vendors), default="")
    return {
        "monitored": len(vendors),
        "issues": len(issues),
        "down": [v["name"] for v in vendors if v["level"] == "major_outage"],
        "degraded": [v["name"] for v in vendors if v["level"] in ("degraded", "partial_outage")],
        "maintenance": [v["name"] for v in vendors if v["level"] == MAINTENANCE],
        "unknown": [v["name"] for v in vendors if v["level"] == "unknown"],
        "by_level": by_level,
        "last_checked": latest,
        "all_clear": len(issues) == 0,
    }


def reliability(days: int = 7) -> list:
    """Minutes spent not-operational per vendor, derived from the events table.

    We only store transitions, so uptime is reconstructed from the intervals
    between them. That is why the collector does not log every poll.
    """
    if not os.path.exists(DB_PATH):
        return []
    conn = db_connect()
    now = datetime.now(timezone.utc)
    window_start = now - timedelta(days=days)
    out = []

    for row in conn.execute("SELECT slug, name, level FROM current").fetchall():
        events = conn.execute(
            "SELECT to_level, at FROM events WHERE slug=? ORDER BY at", (row["slug"],)
        ).fetchall()

        # Level in force when the window opened.
        level_at_start = row["level"]
        for e in events:
            if _parse(e["at"]) <= window_start:
                level_at_start = e["to_level"]

        timeline = [(window_start, level_at_start)]
        for e in events:
            at = _parse(e["at"])
            if at > window_start:
                timeline.append((at, e["to_level"]))
        timeline.append((now, None))

        bad_seconds = 0.0
        for i in range(len(timeline) - 1):
            start, level = timeline[i]
            end = timeline[i + 1][0]
            if level not in (OPERATIONAL, MAINTENANCE, None):
                bad_seconds += (end - start).total_seconds()

        total = (now - window_start).total_seconds()
        out.append({
            "slug": row["slug"],
            "name": row["name"],
            "bad_minutes": round(bad_seconds / 60),
            "uptime_pct": round(100 * (1 - bad_seconds / total), 3) if total else 100.0,
            "incidents": sum(
                1 for e in events
                if _parse(e["at"]) > window_start
                and e["to_level"] not in (OPERATIONAL, MAINTENANCE)
            ),
        })
    conn.close()
    out.sort(key=lambda v: (-v["bad_minutes"], v["name"].lower()))
    return out


def _parse(value: str) -> datetime:
    try:
        dt = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return datetime.now(timezone.utc)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    vendors = fetch_current()
    return render_template(
        "index.html",
        vendors=vendors,
        summary=summarise(vendors),
        correlations=fetch_correlations(),
        tags=sorted({t for v in vendors for t in v["tags"]}),
        styles=STATUS_STYLE,
        page="dashboard",
    )


@app.route("/history")
def history():
    conn = db_connect() if os.path.exists(DB_PATH) else None
    events = []
    if conn:
        rows = conn.execute(
            "SELECT * FROM events ORDER BY at DESC LIMIT 300"
        ).fetchall()
        conn.close()
        for r in rows:
            events.append({
                "name": r["name"] or r["slug"],
                "from": LABELS.get(r["from_level"] or "", "new"),
                "to": LABELS.get(r["to_level"], r["to_level"]),
                "to_level": r["to_level"],
                "color": STATUS_STYLE.get(r["to_level"], STATUS_STYLE["unknown"])["color"],
                "icon": STATUS_STYLE.get(r["to_level"], STATUS_STYLE["unknown"])["icon"],
                "headline": r["headline"] or "",
                "at": r["at"],
                "recovered": SEVERITY.get(r["to_level"], 2) < SEVERITY.get(r["from_level"] or "operational", 0),
            })
    return render_template(
        "history.html",
        events=events,
        reliability=reliability(7),
        page="history",
    )


@app.route("/api/status.json")
def api_status():
    """Public API. Filter with ?tag=Cloud or ?issues=1."""
    vendors = fetch_current()
    if request.args.get("tag"):
        want = request.args["tag"].lower()
        vendors = [v for v in vendors if any(t.lower() == want for t in v["tags"])]
    if request.args.get("issues"):
        vendors = [v for v in vendors if v["level"] not in (OPERATIONAL, MAINTENANCE)]
    return jsonify({
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "summary": summarise(vendors),
        "correlations": fetch_correlations(),
        "vendors": [
            {k: v[k] for k in
             ("slug", "name", "level", "label", "headline", "tags", "url",
              "incidents", "checked_at", "changed_at")}
            for v in vendors
        ],
    })


@app.route("/healthz")
def healthz():
    return jsonify({"ok": os.path.exists(DB_PATH), "db": DB_PATH})


if __name__ == "__main__":
    if not os.path.exists(DB_PATH):
        print("No database yet -- run 'python collector.py --once' first.")
    app.run(host="127.0.0.1", port=5000, debug=False)
