#!/usr/bin/env python3
"""Build data/circuits_recent.json: recent federal courts of appeals arguments with audio.

Source: CourtListener's oral-argument search (Free Law Project), which collects the
recordings each circuit posts — usually the same day.  A free API token raises the
rate limit: set COURTLISTENER_TOKEN (https://www.courtlistener.com/help/api/rest/).

Upcoming circuit calendars are NOT built yet — each circuit publishes them in its own
format (see courts/oral-arguments/data/courts.json for where). That is the next phase of the project.

Usage:  python _oral_arguments/scripts/build_circuits.py [--days 10] [--out data/circuits_recent.json]
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "courts" / "oral-arguments" / "data"
CIRCUITS = ["ca1", "ca2", "ca3", "ca4", "ca5", "ca6", "ca7", "ca8", "ca9", "ca10", "ca11", "cadc", "cafc"]
API = "https://www.courtlistener.com/api/rest/v4/search/"
CL = "https://www.courtlistener.com"
STORAGE = "https://storage.courtlistener.com/"


def normalize(r: dict) -> dict:
    """Map one CourtListener oral-argument search hit to the site's schema."""
    local = r.get("local_path") or ""
    return {
        "id": f"cl-{r.get('id') or r['absolute_url'].strip('/').split('/')[1]}",
        "court": r["court_id"],
        "docket": r.get("docketNumber") or "",
        "title": (r.get("caseName") or "").strip(),
        "date": (r.get("dateArgued") or "")[:10],
        "duration_sec": r.get("duration"),
        "audio_mp3": STORAGE + local if local else None,
        "page": CL + r["absolute_url"],
    }


def dedupe(cases: list[dict]) -> list[dict]:
    """CourtListener sometimes holds two records for one argument; keep the newest id."""
    best: dict[tuple, dict] = {}
    for c in cases:
        key = (c["court"], c["docket"], c["date"])
        cid = int(c["id"].split("-")[1])
        if key not in best or cid > int(best[key]["id"].split("-")[1]):
            best[key] = c
    rows = sorted(best.values(), key=lambda c: (c["court"], c["title"]))
    return sorted(rows, key=lambda c: c["date"], reverse=True)  # newest day first, then by court


def fetch(days: int, max_pages: int = 10) -> list[dict]:
    headers = {"User-Agent": "oral-argument-calendar (personal, non-commercial)"}
    if os.environ.get("COURTLISTENER_TOKEN"):
        headers["Authorization"] = f"Token {os.environ['COURTLISTENER_TOKEN']}"
    since = (dt.date.today() - dt.timedelta(days=days)).isoformat()
    url, params = API, {
        "type": "oa", "court": " ".join(CIRCUITS), "argued_after": since,
        "order_by": "dateArgued desc",
    }
    out: list[dict] = []
    for _ in range(max_pages):
        r = requests.get(url, params=params, headers=headers, timeout=30)
        if r.status_code == 429:
            time.sleep(15)
            continue
        r.raise_for_status()
        body = r.json()
        out += [normalize(x) for x in body.get("results", []) if x.get("court_id") in CIRCUITS]
        url, params = body.get("next"), None
        if not url:
            break
        time.sleep(1)
    return out


def write(cases: list[dict], out_path: Path, days: int, source_note: str) -> None:
    out_path.write_text(json.dumps({
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "window_days": days,
        "source": source_note,
        "cases": dedupe(cases),
    }, indent=2, ensure_ascii=False))
    print(f"wrote {len(cases)} -> {out_path}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=10)
    ap.add_argument("--out", default=str(DATA / "circuits_recent.json"))
    a = ap.parse_args()
    try:
        cases = fetch(a.days)
    except requests.RequestException as e:
        print(f"CourtListener fetch failed: {e}; keeping existing file.", file=sys.stderr)
        sys.exit(0)
    if not cases:
        print("No results; keeping existing file.", file=sys.stderr)
        sys.exit(0)
    write(cases, Path(a.out), a.days, "CourtListener oral argument search (Free Law Project)")
