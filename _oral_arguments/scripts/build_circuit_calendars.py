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
        "parser": "ca4",              # issue statement, docket, caption, "Briefs", judge; time/room at page foot
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
DATE_LINE_RE = re.compile(r"^\s*(january|february|march|april|may|june|july|august|september|october|november|december)\s+(\d{1,2}),?\s+(\d{4})\s*$", re.I)
NO_ARG_RE = re.compile(r"\bNO\s+ARG(?:UMENT)?\b|\bon the briefs\b|submitted on (?:the )?briefs|\*\*", re.I)
STATUS_RE = re.compile(r"\[(?:argued|on the briefs)\]|\bNO\s+ARG(?:UMENT)?\b|\s\d{1,2}$|\*+", re.I)
SKIP_LINE_RE = re.compile(r"^(page \d+|\(\d+ minutes?.*\)|briefs?$|appellant|appellee|petitioner|respondent|counsel|"
                          r"argued by|arguing counsel|panel [a-z0-9]{1,3}:?$|for (the )?(appellant|appellee|petitioner|respondent))", re.I)


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
        dates = [(i, to_date(*m.groups())) for i, l in enumerate(lines) for m in [DAY_RE.search(l) or DATE_LINE_RE.match(l)] if m]
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
            if cfg.get("origin_token") and not origin and not rest and extra and re.fullmatch(r"[A-Z]{2,5}", extra[0]):
                origin = extra.pop(0)
            if not rest and extra and not SKIP_LINE_RE.match(extra[0]):
                rest = extra.pop(0)
            rest = re.sub(r"^-[A-Z]\s*[–-]\s*", "", rest)       # "No.25-10137-P – HODGES ..." (11th Cir. special hearings)
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
                elif len(caption) < 240 and (cont.match(x) or (len(x.split()) <= 3 and not x.endswith("."))
                                             or re.search(r"(\bof|\bthe|\band|\bfor|\bto|&|,|\bv\.|ex rel\.)$", caption.strip())
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


ISSUE_START_RE = re.compile(r"^(?:Briefs\s+)?([A-Z][A-Z ,/&'\-]{2,40}):\s*(.*)$")


def parse_ca4(pages: list[str], cfg: dict) -> list[dict]:
    """4th Circuit calendar pages: each page is one panel. Each case has a docket number on its own
    line (sometimes followed by "Briefs" or "Rehearing En Banc"), the caption, "Briefs", the district
    judge in parentheses, and an issue statement ("CIVIL: Whether ...") either just before or just
    after it. The date is in the page header; the panel, courtroom and time are at the page foot."""
    out = []
    dk = re.compile(r"^\s*(\d{2}-\d{4})\b\s*(.*)$")
    for page in pages:
        raw = [re.sub(r"_{5,}", "", l) for l in page.splitlines()]
        lines = [re.sub(r"\s+", " ", l).strip() for l in raw]
        dm = next((m for l in lines for m in [DAY_RE.search(l)] if m), None)
        if not dm:
            continue
        date = to_date(*dm.groups())
        tm = [to_time(*m.groups()) for l in lines for m in [TIME_RE.search(l)] if m]
        room = " · ".join(dict.fromkeys(r for l in lines for r in [room_label(l)] if r)) or None
        # split the page into case blocks at docket lines (not "Associations:" lines)
        idx = [i for i, l in enumerate(lines) if dk.match(l)]
        issues, cases = [], []
        # issue statements, in page order
        i = 0
        while i < len(lines):
            m = ISSUE_START_RE.match(lines[i])
            if m and re.search(r"whether|rehearing|appeal", lines[i], re.I):
                txt, j = [f"{m.group(1).title()}: {m.group(2)}"], i + 1
                while j < len(lines) and lines[j] and not dk.match(lines[j]) and not ISSUE_START_RE.match(lines[j]) \
                        and " v. " not in lines[j] and not lines[j].startswith(("(", "Briefs", "Panel", "Judge", "Chief Judge")) \
                        and not DAY_RE.search(lines[j]) and not TIME_RE.search(lines[j]):
                    txt.append(lines[j]); j += 1
                issues.append((i, " ".join(txt)))
                i = j
            else:
                i += 1
        for n, i in enumerate(idx):
            end = idx[n + 1] if n + 1 < len(idx) else len(lines)
            block = lines[i + 1:end]
            m = dk.match(lines[i])
            tail = m.group(2)
            # some calendars print the issue in a second column on the docket line itself
            inline = ISSUE_START_RE.match(re.sub(r"^Briefs\s+", "", tail))
            inline_issue = [f"{inline.group(1).title()}: {inline.group(2)}"] if inline and re.search(r"whether", tail, re.I) else []
            cap = []
            for k, l in enumerate(block):
                cols = re.split(r"\s{2,}", raw[i + 1 + k].strip()) if inline_issue else [l]
                if inline_issue and len(cols) == 1 and " v. " in l:
                    # columns ran together: cut the caption at the first lowercase word a case name wouldn't have
                    words, keep = l.split(" "), 0
                    ok = {"v.", "of", "the", "and", "et", "al.", "al.,", "ex", "rel.", "de", "la", "del", "for", "in", "on", "by", "to", "&", "von", "van", "da", "dos", "y", "e"}
                    for w_i, w in enumerate(words):
                        if w and w[0].islower() and w.lower().strip(",.;") not in {x.strip(",.") for x in ok}:
                            break
                        keep = w_i + 1
                    if keep < len(words):
                        cols = [" ".join(words[:keep]), " ".join(words[keep:])]
                if inline_issue and len(cols) > 1 and not cols[0].startswith(("(", "Associations")):
                    l = cols[0]
                    inline_issue.append(" ".join(cols[1:]))
                elif inline_issue and cap and not l.startswith(("(", "Associations", "Judge", "Chief Judge")) and " v. " not in l \
                        and l[:1].islower():
                    inline_issue.append(l)              # issue wrapped onto its own line
                    continue
                if not l or l.startswith(("(", "Briefs", "Associations", "Panel", "Judge", "Chief Judge", "Senior Judge", "*")) \
                        or ISSUE_START_RE.match(l) or DAY_RE.search(l) or TIME_RE.search(l) or room_label(l):
                    if cap:
                        break
                    continue
                cap.append(l)
                if len(cap) >= 3:
                    break
            caption = re.sub(r"(\w)v\.(?=[A-Z])", r"\1 v. ", " ".join(cap))
            caption = re.sub(r"\s+", " ", caption).strip()
            note = "Rehearing en banc" if re.search(r"rehearing en banc", tail + " " + " ".join(block[:3]), re.I) else None
            cases.append({"line": i, "dockets": [m.group(1)], "caption": caption, "note": note,
                          "inline": " ".join(inline_issue).strip() or None})
        # pair issues with cases: an issue belongs to the nearest case below it, else the nearest above
        for c in cases:
            c["issue"] = c.get("inline")
        inline_lines = {c["line"] for c in cases if c.get("inline")}
        issues = [(li, t) for li, t in issues if li not in inline_lines]
        for li, txt in issues:
            below = [c for c in cases if c["line"] > li and c["issue"] is None]
            above = [c for c in reversed(cases) if c["line"] < li and c["issue"] is None]
            target = below[0] if below and (not above or below[0]["line"] - li <= li - above[0]["line"]) else (above[0] if above else None)
            if target:
                target["issue"] = txt
        for c in cases:
            if not re.search(r"\bv\.|\bIn re\b|\bIn the Matter\b|\bex parte\b", c["caption"], re.I):
                continue   # not a caption (e.g. a stray "LIVESTREAM" line)
            out.append({"dockets": c["dockets"], "caption": c["caption"][:300], "origin": None,
                        "no_argument": False, "notes": [c["note"]] if c["note"] else [],
                        "issue": c["issue"], "date": date, "time": tm[-1] if tm else None, "room": room})
    return out


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

        if len(text) < 800 and re.search(r"will be (published|posted)", text, re.I):
            continue                                  # placeholder: "calendar will be published ~30 days ahead"
        parsed = (parse_ca4 if cfg.get("parser") == "ca4" else parse_pdf_text)(pages, cfg)
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
