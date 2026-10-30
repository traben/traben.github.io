#!/usr/bin/env python3
"""Build courts/oral-arguments/data/circuits_upcoming.json: upcoming federal appellate arguments.

Each circuit publishes its calendar differently. This handles the circuits that post
calendar PDFs on a page we can read (1st, 4th, 8th, 10th, 11th, Federal). One generic
line parser does the work; COURTS below holds each circuit's index page, link pattern,
docket format, time zone and live-stream link.

Good-citizen rules: identify the bot honestly, obey robots.txt, fetch each index page
and PDF at most once per run, pause between requests.

Debugging: the text extracted from every PDF is saved to _oral_arguments/debug/<court>/
(not published by Jekyll) so a parser can be fixed against the real layout. Per-court
counts and problems are written to the Actions job summary.

Usage:  python _oral_arguments/scripts/build_circuit_calendars.py [--only ca4,ca8]
"""
from __future__ import annotations

import argparse
import datetime as dt
import io
import json
import os
import re
import sys
import time
import urllib.robotparser
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from pypdf import PdfReader

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "courts" / "oral-arguments" / "data"
DEBUG = ROOT / "_oral_arguments" / "debug"
UA = "oral-argument-calendar-bot/1.0 (+https://traben.github.io/courts/oral-arguments/; personal, non-commercial)"

# --------------------------------------------------------------------------- per-circuit settings
COURTS: dict[str, dict] = {
    "ca1": {
        "pdfs": ["https://www.ca1.uscourts.gov/sites/ca1/files/calendar.pdf"],
        "tz": "America/New_York", "location": "Boston, MA",
        "live": "https://www.ca1.uscourts.gov/courtlivestream",
        "docket": r"\d{2}-\d{4}",
    },
    "ca4": {
        "index": ["https://www.ca4.uscourts.gov/oral-argument/oral-argument-calendar",
                  "https://www.ca4.uscourts.gov/oral-argument/law-school-sessions"],
        "link": r"internetcal.*\.pdf$",
        "tz": "America/New_York", "location": "Richmond, VA",
        "live": "https://www.ca4.uscourts.gov/oral-argument/livestream-oral-arguments",
        "docket": r"\d{2}-\d{4}",
        "issue_lines": True,          # the 4th Circuit prints a short issue statement under each case
    },
    "ca8": {
        "index": ["https://www.ca8.uscourts.gov/argument-calendars"],
        "link": r"webcal/.*\.pdf$",
        "tz": "America/Chicago", "location": None,
        "live": "https://www.ca8.uscourts.gov/news/",
        "live_note": "The 8th Circuit posts a public listen-in phone number before each session.",
        "docket": r"\d{2}-\d{4}",
        "origin_token": True,          # "25-3077 EA United States v. ..." (EA = district code)
        "lead_number": True,           # "1. 26-1064 MN ..."
    },
    "ca10": {
        "index": ["https://www.ca10.uscourts.gov/calendar"],
        "link": r"Public_Calendar.*\.pdf$",
        "tz": "America/Denver", "location": "Denver, CO",
        "live": "https://www.ca10.uscourts.gov/oral-argument-live-streaming",
        "docket": r"\d{2}-\d{4}",
        "origin_token": True,
    },
    "ca11": {
        "index": ["https://www.ca11.uscourts.gov/oral-argument-calendars"],
        "link": r"(?:oral_arguments|special_hearings)/.*\.pdf$",
        "per_pdf_live": r"https://video\.ibm\.com/channel/[\w-]+",   # each session has its own stream
        "tz": "America/New_York", "location": None,
        "live": "https://www.ca11.uscourts.gov/oral-argument-calendars",
        "docket": r"\d{2}-\d{5}",
    },
    "cafc": {
        "index": ["https://www.cafc.uscourts.gov/home/oral-argument/scheduled-cases/"],
        "link": r"PublicCalendar-.*\.pdf$",
        "tz": "America/New_York", "location": "Washington, DC",
        "live": "https://www.cafc.uscourts.gov/home/oral-argument/listen-to-oral-arguments/",
        "docket": r"\d{2}-\d{4}",
        "origin_token": True,          # PTO, CIT, DCT, MSPB, CFC, ITC ...
    },
}

CITY_TZ = {  # sessions held away from the usual courthouse
    "Wichita": "America/Chicago", "Omaha": "America/Chicago", "Kansas City": "America/Chicago",
    "St. Paul": "America/Chicago", "St. Louis": "America/Chicago", "Miami": "America/New_York",
    "Jacksonville": "America/New_York", "Atlanta": "America/New_York", "Greenville": "America/New_York",
    "Montgomery": "America/Chicago", "Tulsa": "America/Chicago", "Oklahoma City": "America/Chicago",
    "Salt Lake": "America/Denver", "Albuquerque": "America/Denver",
}

MONTHS = "january february march april may june july august september october november december".split()
DAY_RE = re.compile(r"\b(?:mon|tues|wednes|thurs|fri|satur|sun)day,?\s+([a-z]+)\.?\s+(\d{1,2}),?\s+(\d{4})", re.I)
TIME_RE = re.compile(r"\b(\d{1,2}):(\d{2})\s*([ap])\.?\s*m\.?", re.I)
NO_ARG_RE = re.compile(r"\bNO\s+ARG(?:UMENT)?\b|\bon the briefs\b|submitted on (?:the )?briefs|\*\*", re.I)
STATUS_RE = re.compile(r"\[(?:argued|on the briefs)\]|\bNO\s+ARG(?:UMENT)?\b|\s\d{1,2}$|\*+", re.I)
SKIP_LINE_RE = re.compile(r"^(page \d+|\(\d+ minutes?.*\)|briefs?$|appellant|appellee|petitioner|respondent|counsel|"
                          r"argued by|for (the )?(appellant|appellee|petitioner|respondent))", re.I)


# --------------------------------------------------------------------------- polite fetching
session = requests.Session()
session.headers["User-Agent"] = UA
_robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}
PROBLEMS: dict[str, list[str]] = {}


def allowed(url: str) -> bool:
    host = urlparse(url).scheme + "://" + urlparse(url).netloc
    if host not in _robots:
        rp = urllib.robotparser.RobotFileParser()
        try:
            r = session.get(host + "/robots.txt", timeout=20)
            if r.status_code >= 400:
                rp = None                       # no robots.txt: everything allowed
            else:
                rp.parse(r.text.splitlines())
        except requests.RequestException:
            rp = None
        _robots[host] = rp
    rp = _robots[host]
    return True if rp is None else rp.can_fetch(UA, url)


def fetch(court: str, url: str, binary: bool = False):
    if not allowed(url):
        PROBLEMS.setdefault(court, []).append(f"robots.txt disallows {url}; skipped")
        return None
    time.sleep(1.0)
    try:
        r = session.get(url, timeout=40)
        r.raise_for_status()
        return r.content if binary else r.text
    except requests.RequestException as e:
        status = getattr(getattr(e, "response", None), "status_code", None)
        PROBLEMS.setdefault(court, []).append(f"{'HTTP ' + str(status) if status else type(e).__name__} {url}")
        return None


# --------------------------------------------------------------------------- parsing
def room_label(line: str) -> str | None:
    """Short courtroom / panel label from a header line, e.g. 'Panel 1 · Red Courtroom (Room 412)'."""
    if len(line) > 160:
        return None
    parts = []
    pm = re.search(r"\b(panel\s+[A-Z0-9]{1,3}|division\s+[IVX]+)\b", line, re.I)
    if pm:
        word, num = pm.group(1).split(None, 1)
        parts.append(f"{word.title()} {num.upper()}")
    cm = re.search(r"((?:[A-Z][a-z]+\s+)?courtroom(?:\s+(?:[IVX]+|\d+[A-Z]?))?(?:\s*\(Room \d+\))?|en banc courtroom)", line, re.I)
    if cm:
        label = cm.group(1).strip()
        if label.isupper():
            label = re.sub(r"\b([ivx]+)\b", lambda m: m.group(1).upper(), label.title(), flags=re.I)
        parts.append(label)
    return " · ".join(parts) or None


def to_date(month: str, day: str, year: str) -> str | None:
    m = month.lower().rstrip(".")
    for i, name in enumerate(MONTHS):
        if name.startswith(m[:3]):
            try:
                return dt.date(int(year), i + 1, int(day)).isoformat()
            except ValueError:
                return None
    return None


def to_time(h: str, m: str, ap: str) -> str:
    hh = int(h) % 12 + (12 if ap.lower() == "p" else 0)
    return f"{hh:02d}:{m}"


def clean_caption(s: str) -> str:
    s = STATUS_RE.sub("", s).strip(" ;,*")
    s = re.sub(r"\s{2,}", " ", s)
    return s


def parse_pdf_text(pages: list[str], cfg: dict) -> list[dict]:
    """Generic calendar parser.

    Walks lines; a line that starts with a docket number starts a case. The case's date and
    time come from the nearest date/time line above it on the same page, or, if none, the
    first one below it on that page (the 4th Circuit prints them in the page footer), or
    else the last one seen on earlier pages."""
    dk = cfg["docket"]
    lead = r"^\s*(?:\d{1,2}\.\s+)?" if cfg.get("lead_number") else r"^\s*(?:No\.\s*)?"
    case_re = re.compile(lead + rf"((?:{dk})(?:\s*(?:,|&|and)?\s*{dk})*)\s*(.*)$")
    cases: list[dict] = []
    carry_date = carry_time = None
    carry_room = None
    head = "\n".join(pages[:1])
    dm = re.search(r"(?:convene|begin|commence|start)[^.\n]{0,40}?" + TIME_RE.pattern, head, re.I)
    default_time = to_time(*dm.groups()[-3:]) if dm else None

    for page in pages:
        lines = [l.rstrip() for l in page.splitlines()]
        dates = [(i, to_date(*m.groups())) for i, l in enumerate(lines) for m in [DAY_RE.search(l)] if m]
        times = [(i, to_time(*m.groups())) for i, l in enumerate(lines) for m in [TIME_RE.search(l)] if m]
        rooms = [(i, r) for i, l in enumerate(lines) for r in [room_label(l)] if r]

        def near(items, i, carry, lo=-1, hi=10**9):
            before = [v for j, v in items if lo <= j <= i and v]
            if before:
                return before[-1]
            after = [v for j, v in items if i < j <= hi and v]
            return after[0] if after else carry

        page_cases = []
        i = 0
        while i < len(lines):
            m = case_re.match(lines[i])
            if not m or DAY_RE.search(lines[i]):
                i += 1
                continue
            dockets = re.findall(dk, m.group(1))
            rest = m.group(2).strip()
            raw = lines[i]
            origin = None
            if cfg.get("origin_token"):
                om = re.match(r"^([A-Z]{2,5})\s+(.+)$", rest)
                if om and om.group(1) not in {"USA"}:
                    origin, rest = om.group(1), om.group(2)
            # continuation lines: caption wraps, amici, notes, issue statements
            extra, j = [], i + 1
            while j < len(lines) and lines[j].strip() and not case_re.match(lines[j]) \
                    and not DAY_RE.search(lines[j]) and len(extra) < 6:
                extra.append(lines[j].strip())
                j += 1
            caption, notes, issue = rest, [], []
            cont = re.compile(r"(and\b|et al|Inc\b|LLC|Corp|Co\.|&|of\b|the\b|[a-z])")
            for x in extra:
                if issue:                                   # once the issue statement starts, it continues
                    if not SKIP_LINE_RE.match(x):
                        issue.append(x)
                    continue
                if SKIP_LINE_RE.match(x) or TIME_RE.search(x) and len(x) < 60:
                    if re.search(r"minutes", x, re.I):
                        notes.append(x.strip("()"))
                    continue
                if re.search(r"\bamic(?:i|us)\b", x, re.I) or x.startswith("("):
                    notes.append(x.strip(" ;"))
                elif len(caption) < 200 and (cont.match(x) or (len(x.split()) <= 3 and not x.endswith("."))
                                             or (" v. " not in caption and not cfg.get("issue_lines"))):
                    caption += " " + x                      # caption wrapped onto the next line
                elif cfg.get("issue_lines"):
                    issue.append(x)
                elif x.startswith("("):
                    notes.append(x.strip("()"))
            no_arg = bool(NO_ARG_RE.search(raw) or any(NO_ARG_RE.search(x) for x in extra))
            page_cases.append({
                "line": i, "dockets": dockets, "caption": clean_caption(caption), "origin": origin,
                "no_argument": no_arg, "notes": notes,
                "issue": re.sub(r"\s{2,}", " ", re.sub(r"\bBriefs?\b|\(Judge [^)]*\)", "", " ".join(issue))).strip() or None,
            })
            i = j

        for c in page_cases:
            i = c["line"]
            d_before = [j for j, v in dates if j <= i and v]
            d_after = [j for j, v in dates if j > i and v]
            c["date"] = near(dates, i, carry_date)
            if d_before:       # date heading above the case: the time must sit between it and the case
                c["time"] = near(times, i, None, lo=d_before[-1], hi=i)
            elif d_after:      # date in the page footer (4th Cir.): take the footer's time
                c["time"] = next((v for j, v in times if j >= d_after[0] - 1), None)
            else:              # no date on this page: same day as the previous page
                c["time"] = near(times, i, carry_time)
            c["time"] = c["time"] or default_time
            if d_before:
                c["room"] = near(rooms, i, None, lo=d_before[-1] - 1, hi=i)
            elif d_after:
                c["room"] = next((v for j, v in rooms if j >= d_after[0] - 1), None)
            else:
                c["room"] = near(rooms, i, carry_room, hi=i)
            del c["line"]
            cases.append(c)
        if dates:
            carry_date = dates[-1][1]
        if times:
            carry_time = times[-1][1]
        if rooms:
            carry_room = rooms[-1][1]
    return [c for c in cases if c["date"] and c["caption"]]


# --------------------------------------------------------------------------- per-court driver
def pdf_links(court: str, cfg: dict) -> list[tuple[str, str | None]]:
    """(pdf_url, live_url_or_None) pairs for a court."""
    out: list[tuple[str, str | None]] = [(u, None) for u in cfg.get("pdfs", [])]
    for page in cfg.get("index", []):
        html = fetch(court, page) or ""
        for m in re.finditer(r"href\s*=\s*[\"']([^\"']+)[\"']", html, re.I):
            if not re.search(cfg["link"], m.group(1), re.I):
                continue
            url = urljoin(page, m.group(1).strip())
            live = None
            if cfg.get("per_pdf_live"):
                lm = re.search(cfg["per_pdf_live"], html[m.end(): m.end() + 2000])
                live = lm.group(0) if lm else None
            if url not in [u for u, _ in out]:
                out.append((url, live))
    return out


def build_court(court: str, cfg: dict, keep_from: str) -> list[dict]:
    rows = []
    for url, live in pdf_links(court, cfg):
        data = fetch(court, url, binary=True)
        if not data:
            continue
        try:
            pages = [(p.extract_text() or "") for p in PdfReader(io.BytesIO(data)).pages]
        except Exception as e:  # noqa: BLE001  (a malformed PDF shouldn't stop the other courts)
            PROBLEMS.setdefault(court, []).append(f"could not read PDF {url}: {e}")
            continue
        text = "\n\f\n".join(pages)
        dbg = DEBUG / court / (Path(urlparse(url).path).stem + ".txt")
        dbg.parent.mkdir(parents=True, exist_ok=True)
        dbg.write_text(text)

        tz = cfg["tz"]
        for city, ctz in CITY_TZ.items():
            if city.lower() in text[:3000].lower():
                tz, location = ctz, city
                break
        else:
            location = cfg.get("location")

        parsed = parse_pdf_text(pages, cfg)
        if not parsed:
            PROBLEMS.setdefault(court, []).append(f"parsed 0 cases from {url} ({len(text)} chars of text)")
        for c in parsed:
            if c["date"] < keep_from:
                continue
            rows.append({
                "id": f"{court}-{c['dockets'][0]}-{c['date']}",
                "court": court,
                "docket": ", ".join(c["dockets"]),
                "title": c["caption"][:300],
                "date": c["date"],
                "time_local": c["time"],
                "tz": tz,
                "location": location,
                "room": c["room"],
                "origin": c["origin"],
                "issue": c["issue"],
                "notes": c["notes"] or None,
                "no_argument": c["no_argument"],
                "links": {"calendar": url, "live": live or cfg["live"]},
                "live_note": cfg.get("live_note"),
            })
    return rows


def link_recordings(rows: list[dict]) -> None:
    """Attach CourtListener recordings (from circuits_recent.json) to cases already argued."""
    p = DATA / "circuits_recent.json"
    if not p.exists():
        return
    rec = {}
    for r in json.loads(p.read_text()).get("cases", []):
        for d in re.findall(r"\d{2}-\d{1,5}", r.get("docket", "")):
            rec[(r["court"], d)] = r
    for c in rows:
        for d in re.findall(r"\d{2}-\d{1,5}", c["docket"]):
            r = rec.get((c["court"], d))
            if r:
                c["links"]["recording"] = r.get("audio_mp3") or r.get("page")
                c["links"]["recording_page"] = r.get("page")
                break


def write_summary(counts: dict[str, int]) -> None:
    lines = ["### Circuit calendars", "", "| Court | Cases | Problems |", "|---|---|---|"]
    for court in COURTS:
        probs = "; ".join(PROBLEMS.get(court, [])[:4]) or "—"
        lines.append(f"| {court} | {counts.get(court, 0)} | {probs} |")
    text = "\n".join(lines) + "\n"
    print(text)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as f:
            f.write(text)
    for court, probs in PROBLEMS.items():
        for p in probs[:3]:
            print(f"::warning title={court} calendar::{p}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="")
    ap.add_argument("--out", default=str(DATA / "circuits_upcoming.json"))
    a = ap.parse_args()
    only = [c for c in a.only.split(",") if c] or list(COURTS)
    out_path = Path(a.out)
    old = json.loads(out_path.read_text()) if out_path.exists() else {"cases": []}

    keep_from = (dt.date.today() - dt.timedelta(days=14)).isoformat()   # keep two weeks back for "recent"
    rows, counts = [], {}
    for court in only:
        got = build_court(court, COURTS[court], keep_from)
        if not got:   # nothing new: keep this court's previous rows so a bad day doesn't blank it
            got = [c for c in old["cases"] if c["court"] == court and c["date"] >= keep_from]
        counts[court] = len(got)
        rows += got
    # courts not run this time keep their old rows
    rows += [c for c in old["cases"] if c["court"] not in only and c["date"] >= keep_from]

    seen, uniq = set(), []
    for c in sorted(rows, key=lambda c: (c["date"], c["court"], c.get("time_local") or "", c["docket"])):
        if c["id"] not in seen:
            seen.add(c["id"])
            uniq.append(c)
    link_recordings(uniq)
    out_path.write_text(json.dumps({
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "courts": list(COURTS),
        "cases": uniq,
    }, indent=1, ensure_ascii=False))
    write_summary(counts)


if __name__ == "__main__":
    main()
