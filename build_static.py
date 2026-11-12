#!/usr/bin/env python3
"""
OutageWatch -- one scheduled run, no server
===========================================

This is what GitHub Actions executes every 10 minutes. It does the same work
your PC does, except there is no PC:

  1. load the saved state out of state/ into a fresh database
  2. poll every vendor once (alerts fire here, same as always)
  3. save the state back to state/
  4. render the dashboard and history as plain HTML files into docs/

GitHub then serves docs/ as a website and keeps state/ in the repository, so
the next run picks up exactly where this one stopped.

Why JSON files instead of committing the database: a database is a binary
blob, so every run would store a whole new copy and the repository would grow
without limit. JSON is text -- git stores only what actually changed, and the
events file is append-only, which is the friendliest shape there is.

    python build_static.py        run it yourself to see what Actions will do
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import collector  # noqa: E402

STATE_DIR = os.path.join(HERE, "state")
DOCS_DIR = os.path.join(HERE, "docs")
CURRENT_JSON = os.path.join(STATE_DIR, "current.json")
EVENTS_JSONL = os.path.join(STATE_DIR, "events.jsonl")
CORRELATIONS_JSON = os.path.join(STATE_DIR, "correlations.json")

CURRENT_COLUMNS = ["slug", "name", "url", "kind", "tags", "level",
                   "headline", "incidents", "checked_at", "changed_at"]


# ---------------------------------------------------------------- state I/O

def load_state(conn) -> None:
    if os.path.exists(CURRENT_JSON):
        with open(CURRENT_JSON, encoding="utf-8") as fh:
            for row in json.load(fh):
                conn.execute(
                    f"INSERT OR REPLACE INTO current ({','.join(CURRENT_COLUMNS)}) "
                    f"VALUES ({','.join('?' * len(CURRENT_COLUMNS))})",
                    [row.get(c) for c in CURRENT_COLUMNS],
                )

    if os.path.exists(EVENTS_JSONL):
        with open(EVENTS_JSONL, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                conn.execute(
                    "INSERT INTO events (slug,name,from_level,to_level,headline,at,notified)"
                    " VALUES (?,?,?,?,?,?,1)",
                    (e.get("slug"), e.get("name"), e.get("from_level"),
                     e.get("to_level"), e.get("headline"), e.get("at")),
                )

    if os.path.exists(CORRELATIONS_JSON):
        with open(CORRELATIONS_JSON, encoding="utf-8") as fh:
            for c in json.load(fh):
                conn.execute(
                    "INSERT OR REPLACE INTO correlations"
                    " (entity,vendors,groups,first_seen,last_seen) VALUES (?,?,?,?,?)",
                    (c["entity"], json.dumps(c["vendors"]), c["groups"],
                     c["first_seen"], c["last_seen"]),
                )
    conn.commit()


def save_state(conn) -> None:
    os.makedirs(STATE_DIR, exist_ok=True)

    rows = [dict(r) for r in conn.execute(
        f"SELECT {','.join(CURRENT_COLUMNS)} FROM current ORDER BY slug")]
    with open(CURRENT_JSON, "w", encoding="utf-8") as fh:
        json.dump(rows, fh, indent=1, sort_keys=True)
        fh.write("\n")

    # Append-only: git stores just the new lines.
    events = [dict(r) for r in conn.execute(
        "SELECT slug,name,from_level,to_level,headline,at FROM events ORDER BY id")]
    with open(EVENTS_JSONL, "w", encoding="utf-8") as fh:
        for e in events:
            fh.write(json.dumps(e, sort_keys=True) + "\n")

    corrs = []
    for r in conn.execute("SELECT * FROM correlations ORDER BY entity"):
        corrs.append({
            "entity": r["entity"], "vendors": json.loads(r["vendors"]),
            "groups": r["groups"], "first_seen": r["first_seen"],
            "last_seen": r["last_seen"],
        })
    with open(CORRELATIONS_JSON, "w", encoding="utf-8") as fh:
        json.dump(corrs, fh, indent=1, sort_keys=True)
        fh.write("\n")


# ------------------------------------------------------------ static render

def rewrite_links(html: str) -> str:
    """Flask writes absolute paths; a site in a subfolder needs relative ones."""
    replacements = [
        ('href="/static/style.css"', 'href="static/style.css"'),
        ("href=\"/history\"", 'href="history.html"'),
        ('href="/api/status.json"', 'href="status.json"'),
        ("fetch('/api/status.json'", "fetch('status.json'"),
        ('href="/"', 'href="index.html"'),
    ]
    for old, new in replacements:
        html = html.replace(old, new)
    return html


def render_site() -> None:
    import app as webapp

    os.makedirs(DOCS_DIR, exist_ok=True)
    os.makedirs(os.path.join(DOCS_DIR, "static"), exist_ok=True)

    client = webapp.app.test_client()

    for route, filename in (("/", "index.html"), ("/history", "history.html")):
        resp = client.get(route)
        if resp.status_code != 200:
            raise SystemExit(f"render failed for {route}: HTTP {resp.status_code}")
        html = rewrite_links(resp.get_data(as_text=True))
        with open(os.path.join(DOCS_DIR, filename), "w", encoding="utf-8") as fh:
            fh.write(html)

    resp = client.get("/api/status.json")
    with open(os.path.join(DOCS_DIR, "status.json"), "w", encoding="utf-8") as fh:
        fh.write(resp.get_data(as_text=True))

    shutil.copyfile(os.path.join(HERE, "static", "style.css"),
                    os.path.join(DOCS_DIR, "static", "style.css"))

    # Stops GitHub Pages running the files through its blog engine.
    open(os.path.join(DOCS_DIR, ".nojekyll"), "w").close()


# ------------------------------------------------------------------- main

def main() -> int:
    started = datetime.now(timezone.utc)
    print(f"OutageWatch scheduled run at {started.isoformat(timespec='seconds')}")

    # Always start from a clean database and rebuild it from the saved state,
    # so a run never depends on a leftover file.
    for suffix in ("", "-wal", "-shm"):
        path = collector.DB_PATH + suffix
        if os.path.exists(path):
            os.remove(path)

    conn = collector.db_connect()
    load_state(conn)

    before = conn.execute("SELECT COUNT(*) n FROM current").fetchone()["n"]
    print(f"  restored {before} vendor(s) from saved state")

    vendors = collector.load_vendors()
    config = collector.load_config()
    if config.get("telegram_bot_token"):
        print("  telegram alerts: on")
    else:
        print("  telegram alerts: off (no token set)")

    collector.run_once(vendors, conn, config)

    save_state(conn)
    render_site()
    conn.close()

    took = (datetime.now(timezone.utc) - started).total_seconds()
    print(f"  wrote docs/ and state/ in {took:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
