#!/usr/bin/env python3
"""Write AI case briefs for the appeals cases you picked, and only those.

The list lives in _oral_arguments/selected_cases.json:

    {"cases": [
      {"court": "ca4", "docket": "25-2342"},
      {"court": "ca9", "docket": "25-6874", "title": "California Chamber of Commerce v. Bonta",
       "note": "not on a calendar this site reads; title given by hand"}
    ]}

For each entry without a brief, this looks the case up in the upcoming calendars
(circuits_upcoming.json) or recent recordings (circuits_recent.json) for its name, date and
any issue statement, then asks Claude (with web search) for an overview, a plain question,
and sourced "why it matters" points. Briefs go to courts/oral-arguments/data/circuit_briefs.json.
A brief is refreshed once after the argument so it can say how the argument went.

Usage:
  python _oral_arguments/scripts/summarize_selected.py                 # everything pending
  python _oral_arguments/scripts/summarize_selected.py --add ca4 25-2342   # add to the list, then run
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from summarize import write_brief  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
TOOLS = ROOT / "_oral_arguments"
DATA = ROOT / "courts" / "oral-arguments" / "data"
LIST = TOOLS / "selected_cases.json"
OUT = DATA / "circuit_briefs.json"

COURT_NAMES = {
    "ca1": "First", "ca2": "Second", "ca3": "Third", "ca4": "Fourth", "ca5": "Fifth", "ca6": "Sixth",
    "ca7": "Seventh", "ca8": "Eighth", "ca9": "Ninth", "ca10": "Tenth", "ca11": "Eleventh",
    "cadc": "District of Columbia", "cafc": "Federal",
}
DOCKET_RE = re.compile(r"^\d{2}-\d{1,5}$")


def court_name(c: str) -> str:
    return f"U.S. Court of Appeals for the {COURT_NAMES[c]} Circuit"


def key(court: str, docket: str) -> str:
    return f"{court}:{docket}"


def load(p: Path, default):
    return json.loads(p.read_text()) if p.exists() else default


def find_case(court: str, docket: str) -> dict:
    """Best available facts about the case from our own data files."""
    for c in load(DATA / "circuits_upcoming.json", {"cases": []})["cases"]:
        if c["court"] == court and docket in re.findall(r"\d{2}-\d{1,5}", c["docket"]):
            return {"title": c["title"], "date": c["date"], "issue": c.get("issue"), "origin": c.get("origin"),
                    "notes": c.get("notes"), "calendar": c["links"].get("calendar")}
    for c in load(DATA / "circuits_recent.json", {"cases": []})["cases"]:
        if c["court"] == court and docket in re.findall(r"\d{2}-\d{1,5}", c.get("docket", "")):
            return {"title": c["title"], "date": c["date"], "recording": c.get("page")}
    return {}


def add(court: str, docket: str, title: str | None = None) -> bool:
    court, docket = court.lower().strip(), docket.strip()
    if court not in COURT_NAMES or not DOCKET_RE.match(docket):
        raise SystemExit(f"Not a valid court/docket: {court} {docket} (expected e.g. ca4 25-2342)")
    lst = load(LIST, {"cases": []})
    if any(e["court"] == court and e["docket"] == docket for e in lst["cases"]):
        return False
    entry = {"court": court, "docket": docket, "added": dt.date.today().isoformat()}
    if title:
        entry["title"] = title
    lst["cases"].append(entry)
    LIST.write_text(json.dumps(lst, indent=2, ensure_ascii=False) + "\n")
    return True


def run(only: str | None = None) -> int:
    lst = load(LIST, {"cases": []})["cases"]
    briefs = load(OUT, {"briefs": {}})["briefs"]
    today = dt.date.today().isoformat()
    selected = {key(e["court"], e["docket"]) for e in lst}
    # drop briefs for cases you removed from the list
    for k in [k for k in briefs if k not in selected]:
        del briefs[k]
    if not os.environ.get("ANTHROPIC_API_KEY") and lst:
        print("::warning title=AI summaries::ANTHROPIC_API_KEY is not set, so selected cases can't be summarized.")
    made = 0
    for e in lst:
        k = key(e["court"], e["docket"])
        if only and k != only:
            continue
        info = find_case(e["court"], e["docket"])
        title = e.get("title") or info.get("title")
        if not title:
            print(f"::warning title=AI summaries::{k} isn't on any calendar this site reads yet and has no "
                  f"\"title\" in selected_cases.json; it will be summarized once it appears.")
            continue
        argued = bool(info.get("date") and info["date"] < today)
        have = briefs.get(k)
        if have and not (argued and not have.get("brief_after_argument")):
            continue
        official = "\n".join(filter(None, [
            f"Case name on the court's calendar: {title}",
            f"Argument date: {info['date']}" if info.get("date") else None,
            f"Appeal from (court code on the calendar): {info['origin']}" if info.get("origin") else None,
            f"Issue statement printed on the court's calendar: {info['issue']}" if info.get("issue") else None,
            f"Notes on the calendar: {'; '.join(info['notes'])}" if info.get("notes") else None,
            f"Note from the site owner: {e['note']}" if e.get("note") else None,
        ]))
        print(f"summarizing {k}: {title[:80]}")
        brief = write_brief(title=title, court=court_name(e["court"]), docket=e["docket"],
                            official_text=official, argued=argued)
        if brief:
            brief.update({"court": e["court"], "docket": e["docket"], "title": title,
                          "written": today, "brief_after_argument": argued})
            briefs[k] = brief
            made += 1
    pending = sorted(selected - set(briefs))        # requested but not written yet (shown as "requested")
    OUT.write_text(json.dumps({"generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                               "pending": pending, "briefs": briefs}, indent=1, ensure_ascii=False))
    print(f"wrote {made} new brief(s); {len(briefs)} total")
    return made


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--add", nargs=2, metavar=("COURT", "DOCKET"))
    ap.add_argument("--title", help="case name, for a case not on a calendar this site reads")
    a = ap.parse_args()
    only = None
    if a.add:
        added = add(*a.add, title=a.title)
        print("added to selected_cases.json" if added else "already on the list")
        only = key(a.add[0].lower(), a.add[1])
    run(only)
