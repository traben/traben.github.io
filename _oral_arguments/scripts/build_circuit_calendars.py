#!/usr/bin/env python3
"""Build courts/oral-arguments/data/circuits_upcoming.json: upcoming federal appellate arguments.

Each circuit publishes its calendar differently. Calendar PDFs (1st, 4th, 8th, 10th, 11th,
Federal) go through one generic line parser; the 3rd, 5th, 6th, 7th and 9th have small
"readers" for their web calendars (see READERS). COURTS below holds each circuit's
sources, time zone and live-stream link. The 2nd and D.C. Circuits' calendar pages are
closed to automated readers by robots.txt, so they are not read.

Good-citizen rules: identify the bot honestly, obey robots.txt (including Crawl-delay),
fetch each page at most once per run, pause between requests.

Debugging: the text extracted from every PDF is saved to _oral_arguments/debug/<court>/
(not published by Jekyll) so a parser can be fixed against the real layout. Per-court
counts and problems are written to the Actions job summary.

Usage:  python _oral_arguments/scripts/build_circuit_calendars.py [--only ca4,ca8]
"""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import io
import json
import os
import re
import sys
import time
import urllib.robotparser
from html import unescape
from pathlib import Path
from urllib.parse import unquote, urljoin, urlparse

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
    # ---- courts whose calendars are web pages or data feeds rather than PDFs ("reader") ----
    "ca3": {
        "reader": "ca3",
        # Month grids on www.ca3 link each sitting day; the day's case list lives on the
        # court's Power Pages portal, which the page itself fills from two JSON grids.
        "index": ["https://www.ca3.uscourts.gov/calendar"],
        "portal": "https://ca03portal.powerappsportals.us",
        "tz": "America/New_York", "location": "Philadelphia, PA",
        "live": "https://www.youtube.com/channel/UCLSXp4JMYiFc7BHD_ln3d-w",
        "live_note": "The 3rd Circuit streams argument audio live on YouTube.",
    },
    "ca5": {
        "reader": "ca5",
        # The calendar page carries its data as a JSON block (<script data-cal-json>).
        "index": ["https://www.ca5.uscourts.gov/court-calendars"],
        "tz": "America/Chicago", "location": "New Orleans, LA",
        "live": "https://www.ca5.uscourts.gov/live-streams",
    },
    "ca6": {
        "reader": "ca6",
        # Weekly case lists are posted about two weeks ahead; the yearly sitting schedule
        # PDFs give the session weeks before that.
        "index": ["https://www.ca6.uscourts.gov/oral-argument-calendars"],
        "tz": "America/New_York", "location": "Cincinnati, OH",
        "live": "https://www.ca6.uscourts.gov/live-arguments",
        "docket": r"\d{2}-\d{4}",
    },
    "ca7": {
        "reader": "ca7",
        # The court's search form (POST) returns a month's arguments, up to 30 days ahead.
        "index": ["https://media.ca7.uscourts.gov/showOralArgCalendarOutside.jsp"],
        "search": "https://media.ca7.uscourts.gov/cgi-bin/oralArgCal/getOralArgCalendar.pl",
        "tz": "America/Chicago", "location": "Chicago, IL",
        "live": "https://www.youtube.com/channel/UCWvXsHlWdsIJHy3R_znCUsA/streams",
        "live_note": "The 7th Circuit streams arguments live on YouTube.",
    },
    "ca9": {
        "reader": "ca9",
        # The calendar page embeds every posted sitting as a JavaScript array.
        "index": ["https://www.ca9.uscourts.gov/cases/calendar/"],
        "tz": "America/Los_Angeles", "location": None,
        "live": "https://www.ca9.uscourts.gov/media/live-oral-arguments",
        "live_note": "Every 9th Circuit argument is streamed live on YouTube.",
    },
    # ---- refreshed only when the site owner asks (never on the schedule) ----
    # These courts' calendar pages are closed to automated readers by robots.txt. The daily
    # run never touches them. They are read only by `--manual`, which the "Refresh calendars"
    # workflow runs when the owner clicks the button on the page; at most once per
    # MANUAL_COOLDOWN_H hours, with a user agent that says a person asked for it. See README.
    "cadc": {
        "reader": "cadc", "manual": True,
        "index": ["https://media.cadc.uscourts.gov/calendar/calendar.php?cal=FutureDates"],
        "tz": "America/New_York", "location": "Washington, DC",
        "live": "https://media.cadc.uscourts.gov/calendar/calendar.php?cal=TodaysSitting",
        "live_note": "Live audio from the court's Today's Sitting page (also on its YouTube channel).",
    },
    "ca2": {
        "reader": "ca2", "manual": True,
        # The court's calendar page embeds this HuntCal calendar; each "Argument Calendar"
        # event lists the day's panel and cases.
        "index": ["https://www.huntcal.com/cal/view/USCA2/USCA2"],
        "tz": "America/New_York", "location": "New York, NY",
        "live": "https://ww2.ca2.uscourts.gov/court.html",
        "live_note": "Live audio on the court's YouTube channels (one per courtroom).",
    },
}

NINTH_TZ = {"PHX": "America/Phoenix", "HODC": "Pacific/Honolulu", "HI": "Pacific/Honolulu",
            "AK": "America/Anchorage", "ANC": "America/Anchorage"}
NINTH_TZ_BY_NAME = {"Phoenix": "America/Phoenix", "Honolulu": "Pacific/Honolulu", "Anchorage": "America/Anchorage",
                    "Missoula": "America/Denver", "Helena": "America/Denver", "Billings": "America/Denver",
                    "Boise": "America/Boise", "Pocatello": "America/Boise", "Guam": "Pacific/Guam",
                    "Saipan": "Pacific/Saipan", "Hagatna": "Pacific/Guam", "Hagåtña": "Pacific/Guam"}
FIFTH_TZ_BY_CITY = {"El Paso": "America/Denver"}
THIRD_TZ_BY_CITY = {"St. Thomas": "America/St_Thomas", "St. Croix": "America/St_Thomas", "Christiansted": "America/St_Thomas"}

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
MANUAL_UA = UA.replace("personal, non-commercial", "personal, non-commercial; one-time refresh requested by a person")
MANUAL_COOLDOWN_H = 12
_owner_requested_hosts: set[str] = set()   # hosts read only because the owner asked (robots.txt noted, not obeyed)
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


_last_hit: dict[str, float] = {}


def _wait_turn(url: str) -> None:
    """Pause between requests to one host: at least 1 s, or the site's Crawl-delay if it sets one."""
    host = urlparse(url).netloc
    rp = _robots.get(urlparse(url).scheme + "://" + host)
    delay = 1.0
    if rp is not None:
        try:
            delay = max(delay, float(rp.crawl_delay(UA) or 0))
        except (TypeError, ValueError):
            pass
    wait = _last_hit.get(host, 0) + delay - time.monotonic()
    if wait > 0:
        time.sleep(wait)
    _last_hit[host] = time.monotonic()


def request(court: str, method: str, url: str, binary: bool = False, quiet: bool = False, **kw):
    """One polite request. Returns text (or bytes), or None after logging the problem."""
    if urlparse(url).netloc in _owner_requested_hosts:
        kw.setdefault("headers", {})["User-Agent"] = MANUAL_UA
    elif not allowed(url):
        PROBLEMS.setdefault(court, []).append(f"robots.txt disallows {url}; skipped")
        return None
    _wait_turn(url)
    try:
        r = session.request(method, url, timeout=40, **kw)
        r.raise_for_status()
        return r.content if binary else r.text
    except requests.RequestException as e:
        if not quiet:
            status = getattr(getattr(e, "response", None), "status_code", None)
            PROBLEMS.setdefault(court, []).append(f"{'HTTP ' + str(status) if status else type(e).__name__} {method} {url}")
        return None


def fetch(court: str, url: str, binary: bool = False, quiet: bool = False):
    return request(court, "GET", url, binary=binary, quiet=quiet)


def save_debug(court: str, name: str, text: str) -> None:
    p = DEBUG / court / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


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


# --------------------------------------------------------------------------- web-page / data-feed readers
def make_row(court: str, cfg: dict, *, docket: str, title: str, date: str, time_local: str | None,
             tz: str | None = None, location: str | None = None, room: str | None = None,
             origin: str | None = None, issue: str | None = None, notes: list[str] | None = None,
             no_argument: bool = False, calendar: str, live: str | None = None, panel: str | None = None) -> dict:
    first = re.findall(r"\d{2}-\d{1,5}", docket)
    return {
        "id": f"{court}-{first[0] if first else docket}-{date}",
        "court": court,
        "docket": docket,
        "title": re.sub(r"\s{2,}", " ", title).strip()[:300],
        "date": date,
        "time_local": time_local,
        "tz": tz or cfg["tz"],
        "location": location if location is not None else cfg.get("location"),
        "room": room,
        "origin": origin,
        "issue": issue,
        "notes": notes or None,
        "panel": panel,
        "no_argument": no_argument,
        "links": {"calendar": calendar, "live": live or cfg["live"]},
        "live_note": cfg.get("live_note"),
    }


def ampm(s: str) -> str | None:
    """'9:30am', '9:30 am', '930 AM', '1:30 p.m.' -> 'HH:MM'."""
    m = re.search(r"(\d{1,2}):?(\d{2})\s*([ap])\.?\s*m", s or "", re.I)
    return to_time(*m.groups()) if m else None


def js_literal(src: str, start: int):
    """Parse the JavaScript array/object literal that starts at src[start] ('[' or '{').

    Handles the subset the 9th Circuit page uses: bare or quoted keys, double- or
    single-quoted strings, numbers, true/false/null. Returns (value, end_index)."""
    depth, q, j = 0, None, start
    while j < len(src):
        c = src[j]
        if q:
            if c == "\\":
                j += 2
                continue
            if c == q:
                q = None
        elif c in "\"'":
            q = c
        elif c in "[{":
            depth += 1
        elif c in "]}":
            depth -= 1
            if depth == 0:
                break
        j += 1
    s, out, k, q = src[start:j + 1], [], 0, None
    while k < len(s):
        c = s[k]
        if q:
            if c == "\\":
                nxt = s[k + 1]
                out.append("'" if nxt == "'" else "\\" + nxt)
                k += 2
                continue
            if c == q:
                out.append('"')
                q = None
            elif c == '"':
                out.append('\\"')
            else:
                out.append(c)
            k += 1
            continue
        if c in "\"'":
            q = c
            out.append('"')
            k += 1
            continue
        m = re.match(r"([A-Za-z_$][\w$]*)\s*:", s[k:])
        prev = next((x for x in reversed(out) if x.strip()), "")
        if m and prev[-1:] in "{,":
            out.append(f'"{m.group(1)}":')
            k += m.end()
            continue
        m = re.match(r"!0|!1", s[k:])
        if m:
            out.append("true" if m.group(0) == "!0" else "false")
            k += 2
            continue
        out.append(c)
        k += 1
    return json.loads("".join(out)), j + 1


def read_ca9(court: str, cfg: dict, keep_from: str) -> tuple[list[dict], list[dict]]:
    page = cfg["index"][0]
    html = fetch(court, page) or ""
    m = re.search(r"\bglobal_panel_sittings\s*=\s*\[", html)
    if not m:
        PROBLEMS.setdefault(court, []).append("calendar page no longer has global_panel_sittings; layout changed?")
        return [], []
    try:
        sittings, _ = js_literal(html, m.end() - 1)
    except (ValueError, IndexError) as e:
        save_debug(court, "calendar_page.html", html)
        PROBLEMS.setdefault(court, []).append(f"could not read the sittings data: {e}")
        return [], []
    save_debug(court, "sittings.json", json.dumps(sittings, indent=1, ensure_ascii=False))
    rows = []
    for day in sittings:
        date = day.get("date")
        if not date or date < keep_from:
            continue
        for loc in day.get("locations", []):
            code, name = loc.get("hl_code") or "", loc.get("location_display_name") or ""
            tz = NINTH_TZ.get(code) or NINTH_TZ_BY_NAME.get(name) or cfg["tz"]
            week = f"https://www.ca9.uscourts.gov/cases/calendar/calendar-week/?date={date}&loc={code}"
            for cr in loc.get("courtrooms", []):
                for slot in cr.get("times", []):
                    t = ampm(slot.get("time", ""))
                    for c in slot.get("cases", []):
                        at = (c.get("argument_time") or "").strip()
                        no_arg = at.lower().startswith("subm")
                        notes = []
                        if at and not no_arg and not at.lower().startswith("def"):
                            notes.append(f"{at} per side" if "min" in at else at)
                        if at.lower().startswith("def"):
                            notes.append("Submission deferred")
                        rows.append(make_row(
                            court, cfg, docket=c.get("case_num", ""), title=c.get("case_title", ""),
                            date=date, time_local=t, tz=tz, location=name or None, room=cr.get("courtroom"),
                            origin=" · ".join(x for x in [c.get("case_type"), c.get("originating_district")] if x) or None,
                            notes=notes, no_argument=no_arg, calendar=week))
    return rows, []


def read_ca5(court: str, cfg: dict, keep_from: str) -> tuple[list[dict], list[dict]]:
    page = cfg["index"][0]
    html = fetch(court, page) or ""
    m = re.search(r"<script[^>]*data-cal-json[^>]*>(.*?)</script>", html, re.S | re.I)
    if not m:
        PROBLEMS.setdefault(court, []).append("calendar page has no data-cal-json block; layout changed?")
        return [], []
    try:
        data = json.loads(m.group(1))
    except ValueError as e:
        PROBLEMS.setdefault(court, []).append(f"could not read the calendar JSON: {e}")
        return [], []
    save_debug(court, "calendar.json", json.dumps(data, indent=1, ensure_ascii=False))
    rows = []
    for day in data.get("days", []):
        date = day.get("date")
        if not date or date < keep_from:
            continue
        for p in day.get("panels", []):
            city = p.get("city") or cfg.get("location")
            tz = next((z for k, z in FIFTH_TZ_BY_CITY.items() if k in (city or "")), cfg["tz"])
            panel = ", ".join(p.get("judges") or []) or None
            for c in p.get("cases", []):
                notes = [x for x in [f"{c['perSide']} min per side" if c.get("perSide") else None,
                                     c.get("specialNote")] + list(p.get("notes") or []) if x]
                rows.append(make_row(
                    court, cfg, docket=c.get("docket", ""), title=c.get("caption", ""), date=date,
                    time_local=c.get("time") or None, tz=tz, location=city, room=p.get("room"),
                    origin=" · ".join(x for x in [c.get("nature"), c.get("origin")] if x) or None,
                    notes=notes, no_argument=bool(c.get("submitted")), panel=panel,
                    calendar=f"https://www.ca5.uscourts.gov/court-calendars/sitting?d={date}"))
    return rows, []


def read_ca7(court: str, cfg: dict, keep_from: str) -> tuple[list[dict], list[dict]]:
    fetch(court, cfg["index"][0], quiet=True)          # the public form page (also checks robots for the host)
    today = dt.date.today()
    months = sorted({(today.year, today.month), ((today + dt.timedelta(days=30)).year, (today + dt.timedelta(days=30)).month),
                     (dt.date.fromisoformat(keep_from).year, dt.date.fromisoformat(keep_from).month)})
    rows = []
    for y, mo in months:
        label = f"{MONTHS[mo - 1].title()}_{y}"
        html = request(court, "POST", cfg["search"], data={
            "CaseKey": "", "DateKey": "", "CourtRoom": "both", "SessionSearchType": "month",
            "SessionMonth": label, "SessionWeek": "", "submit": "Submit"}) or ""
        html = re.sub(r"<!--.*?-->", "", html, flags=re.S)
        save_debug(court, f"{label}.html", html)
        for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S | re.I):
            tds = [re.sub(r"\s+", " ", unescape(re.sub(r"<br\s*/?>", " | ", re.sub(r"<(?!br)[^>]+>", "", td, flags=re.I), flags=re.I))).strip()
                   for td in re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S | re.I)]
            if len(tds) < 9 or not re.fullmatch(r"\d{2}-\d{4}", tds[0]):
                continue
            dm = re.fullmatch(r"(\d{2})/(\d{2})/(\d{4})", tds[6])
            if not dm:
                continue
            date = f"{dm.group(3)}-{dm.group(1)}-{dm.group(2)}"
            if date < keep_from:
                continue
            tm = re.fullmatch(r"(\d{1,2})(\d{2})\s*([AP])M", tds[8].replace(":", ""), re.I)
            title = tds[1]
            low = re.search(r"\s*\[([^\]]+)\]\s*$", title)          # "[1:20-cr-00926-1]" lower-court number
            title = title[:low.start()] if low else title
            where = [x.strip() for x in tds[4].split("|")]
            judges = [re.sub(r"^(?:Chief\s+)?(?:Circuit|District|Senior)?\s*Judge\s+", "", x.strip(), flags=re.I)
                      for x in (tds[9] if len(tds) > 9 else "").split("|")]
            judges = [j for j in judges if j and j.upper() != "TBD"]
            panel = ", ".join(judges) or None
            notes = [f"{tds[5]} min per side"] if tds[5].isdigit() else []
            if tds[7].isdigit():
                notes.append(f"No. {tds[7]} on the day's list")
            dk_url = re.search(r"href='([^']+)'", tr)
            rows.append(make_row(
                court, cfg, docket=tds[0], title=title.replace(" v ", " v. "), date=date,
                time_local=to_time(*tm.groups()) if tm else None,
                location=where[0] or cfg["location"], room=where[1] if len(where) > 1 else None,
                origin=" · ".join(x for x in [tds[2].title() if tds[2] else None, tds[3],
                                              f"No. {low.group(1)}" if low else None] if x) or None,
                notes=notes, panel=panel,
                calendar=f"{cfg['search']}?DateKey={date}&CourtRoom=both"))   # the day's full list
            if dk_url:
                rows[-1]["links"]["docket"] = unescape(dk_url.group(1))
    return rows, []


def read_ca3(court: str, cfg: dict, keep_from: str) -> tuple[list[dict], list[dict]]:
    base = cfg["portal"]
    today = dt.date.today()
    nxt = (today.replace(day=1) + dt.timedelta(days=32)).replace(day=1)
    pages = [cfg["index"][0], f"https://www.ca3.uscourts.gov/calendar/month/{nxt:%Y-%m}"]
    dates = set()
    for p in pages:
        html = fetch(court, p, quiet=p != pages[0]) or ""
        dates |= set(re.findall(r"/calendar/day/(\d{4}-\d{2}-\d{2})", html))
    dates = sorted(d for d in dates if d >= max(keep_from, (today - dt.timedelta(days=7)).isoformat()))
    if not dates:
        return [], []
    tok_html = fetch(court, base + "/_layout/tokenhtml") or ""
    tm = re.search(r'value="([^"]+)"', tok_html)
    if not tm:
        PROBLEMS.setdefault(court, []).append("portal did not return a request token")
        return [], []
    hdr = {"__RequestVerificationToken": tm.group(1), "Content-Type": "application/json; charset=utf-8",
           "X-Requested-With": "XMLHttpRequest"}
    # The portal's own landing page runs this flow to turn a hearing date into a case-list id.
    flow = base + "/_api/cloudflow/v1.0/trigger/3d46af21-cbad-f011-bbd2-001dd80df774"
    rows = []
    for date in dates:
        got = request(court, "POST", flow, headers=hdr, quiet=True,
                      data=json.dumps({"eventData": json.dumps({"HearingDate": date})}))
        try:
            oid = json.loads(got or "{}").get("oralargumentid")
        except ValueError:
            oid = None
        if not oid:
            continue                                   # a calendar day with no argument list (e.g. a holiday entry)
        summary = f"{base}/Oral-Argument-Summary/?id={oid}&hrngDt={date}"
        page = fetch(court, summary) or ""
        for g in re.findall(r'<div class="entity-grid(?:&#32;|\s)subgrid"(.*?)>', page, re.S):
            a = {k: unescape(v) for k, v in re.findall(r'(data-[\w-]+)="([^"]*)"', g)}
            try:
                layout = json.loads(base64.b64decode(a["data-view-layouts"]))[0]
            except (KeyError, ValueError, IndexError):
                continue
            argued = "argued" in (layout.get("ViewName") or "").lower()
            body = {"base64SecureConfiguration": layout["Base64SecureConfiguration"],
                    "sortExpression": layout.get("SortExpression") or "", "search": None, "page": 1,
                    "pageSize": 100, "pagingCookie": "", "filter": None, "metaFilter": None,
                    "nlSearchFilter": None, "timezoneOffset": 300, "customParameters": [],
                    "entityName": a.get("data-ref-entity"), "entityId": a.get("data-ref-id")}
            res = request(court, "POST", base + a["data-get-url"], headers=hdr, data=json.dumps(body))
            try:
                recs = json.loads(res or "{}").get("Records", [])
            except ValueError:
                recs = []
            save_debug(court, f"{date}_{'argued' if argued else 'submitted'}.json",
                       json.dumps([{x["Name"]: x.get("DisplayValue") for x in r.get("Attributes", [])} for r in recs], indent=1))
            for r in recs:
                f = {x["Name"]: x.get("DisplayValue") or x.get("FormattedValue") for x in r.get("Attributes", [])}
                if not f.get("ca3dev_casenumber"):
                    continue
                lt = f.get("ca3dev_locationtime") or ""          # "Philadelphia, PA - Maris Courtroom / 9:30am"
                lm = re.match(r"\s*(.*?)\s+-\s+(.*?)\s*/\s*(.*)$", lt)
                city, room, t = (lm.group(1), lm.group(2), ampm(lm.group(3))) if lm else (None, None, ampm(lt))
                tz = next((z for k, z in THIRD_TZ_BY_CITY.items() if k in (city or "")), cfg["tz"])
                rows.append(make_row(
                    court, cfg, docket=f["ca3dev_casenumber"], title=f.get("ca3dev_caption") or "",
                    date=date, time_local=t if argued else None, tz=tz, location=city, room=room,
                    panel=(f.get("ca3dev_panel") or "").title() or None, no_argument=not argued,
                    calendar=summary))
    return rows, []


SESSION_RANGE_RE = re.compile(
    r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+(\d{1,2})\s*[-–]\s*"
    r"(?:(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+)?(\d{1,2})\b", re.I)


def read_ca6(court: str, cfg: dict, keep_from: str) -> tuple[list[dict], list[dict]]:
    page = cfg["index"][0]
    html = fetch(court, page) or ""
    rows, sessions = [], []
    # 1) weekly case lists, once posted ("Scheduled Oral Argument Calendars")
    sec = html[html.lower().find("scheduled oral argument"):] if "scheduled oral argument" in html.lower() else ""
    for m in re.finditer(r"href\s*=\s*[\"']([^\"']+\.pdf)[\"']", sec[:20000], re.I):
        url = urljoin(page, m.group(1))
        if re.search(r"sitting", url, re.I):
            continue
        data = fetch(court, url, binary=True)
        if not data:
            continue
        try:
            pages = [(p.extract_text() or "") for p in PdfReader(io.BytesIO(data)).pages]
        except Exception as e:  # noqa: BLE001
            PROBLEMS.setdefault(court, []).append(f"could not read PDF {url}: {e}")
            continue
        save_debug(court, Path(urlparse(url).path).stem + ".txt", "\n\f\n".join(pages))
        parsed = parse_pdf_text(pages, cfg)
        if not parsed:
            PROBLEMS.setdefault(court, []).append(f"parsed 0 cases from {url}")
        for c in parsed:
            if c["date"] >= keep_from:
                rows.append(make_row(court, cfg, docket=", ".join(c["dockets"]), title=c["caption"], date=c["date"],
                                     time_local=c["time"], room=c["room"], origin=c["origin"], issue=c["issue"],
                                     notes=c["notes"], no_argument=c["no_argument"], calendar=url))
    # 2) session weeks from the yearly sitting schedules
    posted_days = {r["date"] for r in rows}
    for m in re.finditer(r"href\s*=\s*[\"']([^\"']*sitting[^\"']*\.pdf)[\"']", html, re.I):
        url = urljoin(page, m.group(1))
        ym = re.search(r"(20\d{2})", unquote(url))
        data = fetch(court, url, binary=True)
        if not data or not ym:
            continue
        try:
            text = "\n".join((p.extract_text() or "") for p in PdfReader(io.BytesIO(data)).pages)
        except Exception as e:  # noqa: BLE001
            PROBLEMS.setdefault(court, []).append(f"could not read PDF {url}: {e}")
            continue
        save_debug(court, Path(unquote(urlparse(url).path)).stem.replace(" ", "_") + ".txt", text)
        year = int(ym.group(1))
        for sm in SESSION_RANGE_RE.finditer(text):
            m1, d1, m2, d2 = sm.groups()
            start = to_date(m1, d1, str(year))
            end = to_date(m2 or m1, d2, str(year))
            if not start or not end or end < start:
                continue
            d = dt.date.fromisoformat(start)
            while d.isoformat() <= end:
                iso = d.isoformat()
                if d.weekday() < 5 and iso >= keep_from and iso not in posted_days:
                    sessions.append({"id": f"{court}-session-{iso}", "court": court, "date": iso,
                                     "tz": cfg["tz"], "location": cfg["location"],
                                     "note": "Court in session. The case list is posted about two weeks ahead.",
                                     "links": {"calendar": page, "schedule": url, "live": cfg["live"]}})
                d += dt.timedelta(days=1)
    return rows, sorted({s["id"]: s for s in sessions}.values(), key=lambda s: s["date"])


def _cells(html: str) -> list[str]:
    return [re.sub(r"[ \t]+", " ", unescape(re.sub(r"<br\s*/?>", "\n", re.sub(r"<(?!br)[^>]+>", " ", td, flags=re.I), flags=re.I))
                   .replace("\xa0", " ")).strip()
            for td in re.findall(r"<td[^>]*>(.*?)</td>", html, re.S | re.I)]


def read_cadc(court: str, cfg: dict, keep_from: str) -> tuple[list[dict], list[dict]]:
    """D.C. Circuit 'Future Dates' page: one <tr id="docket"> per case, with the sitting's
    date, time, courtroom and panel repeated in each row."""
    page = cfg["index"][0]
    html = fetch(court, page) or ""
    save_debug(court, "future_dates.html", html)
    rows = []
    for m in re.finditer(r'<tr id="([^"]+)">(.*?)</tr>', html, re.S | re.I):
        body = m.group(2)
        head = re.search(r'class="print-bold">(.*?)</div>', body, re.S)
        judges = re.search(r"<i>(.*?)</i>", body, re.S)
        case = re.search(r'col-xs-3">(.*?)</div>\s*<div class="col-xs-5">(.*?)</div>\s*<div class="col-xs-4">(.*?)</div>', body, re.S)
        if not head or not case:
            continue
        h = re.sub(r"\s+", " ", unescape(head.group(1))).strip()      # "Thursday, November 5, 2026 9:30 A.M. USCA Courtroom 31"
        dm = DAY_RE.search(h)
        if not dm:
            continue
        date = to_date(*dm.groups())
        if not date or date < keep_from:
            continue
        room = re.sub(r"^.*?[ap]\.?\s*m\.?\s*", "", h, flags=re.I).strip() or None
        docket, title, note = (re.sub(r"\s+", " ", unescape(x)).strip() for x in case.groups())
        panel = re.sub(r"^Judges?\s+", "", unescape(judges.group(1)).strip()) if judges else ""
        no_arg = bool(re.search(r"w/o argument|without argument|on the briefs", note, re.I))
        rows.append(make_row(court, cfg, docket=docket, title=title, date=date, time_local=ampm(h), room=room,
                             panel=panel or None, notes=[note] if note else None, no_argument=no_arg,
                             calendar=page))
    return rows, []


def read_ca2(court: str, cfg: dict, keep_from: str) -> tuple[list[dict], list[dict]]:
    """2nd Circuit HuntCal: month list views, then each 'Argument Calendar' event page."""
    base = "https://www.huntcal.com/cal"
    today = dt.date.today()
    horizon = (today + dt.timedelta(days=45)).isoformat()
    months, d = [], dt.date.fromisoformat(keep_from).replace(day=1)
    while d.isoformat() <= horizon:
        months.append(d)
        d = (d + dt.timedelta(days=32)).replace(day=1)
    events = {}
    for mo in months:
        html = fetch(court, f"{base}/view/USCA2/USCA2?vm=r-h-f-l&s=l&m={mo.month}&yr={mo.year}") or ""
        for em in re.finditer(r"eventview/USCA2/USCA2\?([^\"']+)[\"'][^>]*>([^<]+)<", html):
            q = dict(x.split("=", 1) for x in unescape(em.group(1)).split("&") if "=" in x)
            if not re.search(r"argument calendar", em.group(2), re.I) or not q.get("id"):
                continue
            try:
                date = dt.date(int(q["yr"]), int(q["m"]), int(q["d"])).isoformat()
            except (KeyError, ValueError):
                continue
            if keep_from <= date <= horizon:
                events[q["id"]] = (date, em.group(2).strip())
    rows = []
    for eid, (date, label) in sorted(events.items(), key=lambda x: x[1]):
        y, mth, dd = date.split("-")
        url = f"{base}/eventview/USCA2/USCA2?yr={y}&m={int(mth)}&d={int(dd)}&id={eid}"
        html = fetch(court, url) or ""
        html = re.sub(r"<script.*?</script>|<style.*?</style>", "", html, flags=re.S | re.I)
        save_debug(court, f"{date}_{eid}.html", html)
        text = re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", " ", html))).replace("\xa0", " ")
        t = ampm((re.search(r"Time:\s*([\d:]+\s*[ap]\.?\s*m)", text, re.I) or re.search(r"(\d{1,2}:\d{2}\s*[ap]m)", text, re.I) or [None, ""])[1])
        pm = re.search(r"Panel:\s*(.*?)(?:,?\s*(?:C\.?\s*)?J\.?J\.?|\s{2,}|Docket number)", text)
        panel = pm.group(1).strip(" ,") if pm else None
        room = (re.search(r"(Courtroom\s+\w+)", label) or [None, None])[1]
        rn = re.search(r"(\d{3,4})", room or "")
        live = f"https://www.youtube.com/@USCourtsCA2-{rn.group(1)}/streams" if rn and rn.group(1) in {"1703", "1505"} else None
        for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S | re.I):
            cells = _cells(tr)
            if len(cells) < 2:
                continue
            dks = re.findall(r"\b(\d{2}-\d{1,5})(?:-[a-z]{2,3})?\b", cells[0])
            if not dks:
                continue
            cap_lines = [x.strip() for x in cells[1].split("\n") if x.strip()]
            caption = " ".join(cap_lines[:2]) if len(cap_lines) > 1 and cap_lines[1].lower().startswith("v") else (cap_lines[0] if cap_lines else "")
            allotted = " ".join(cells[2:]).strip()
            no_arg = bool(re.search(r"on submission", allotted, re.I)) and not re.search(r"\(\s*\d+\s*\)", allotted)
            mins = re.findall(r"\(\s*(\d+)\s*\)", allotted)
            rows.append(make_row(court, cfg, docket=", ".join(dict.fromkeys(dks)), title=caption, date=date, time_local=t,
                                 room=room, panel=panel, no_argument=no_arg,
                                 notes=[f"{mins[0]} min per side"] if mins else None, calendar=url, live=live))
    return rows, []


READERS = {"cadc": read_cadc, "ca2": read_ca2, "ca3": read_ca3, "ca5": read_ca5, "ca6": read_ca6, "ca7": read_ca7, "ca9": read_ca9}


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


def build_court(court: str, cfg: dict, keep_from: str) -> tuple[list[dict], list[dict]]:
    if cfg.get("reader"):
        try:
            return READERS[cfg["reader"]](court, cfg, keep_from)
        except Exception as e:  # noqa: BLE001  (one court's layout change shouldn't stop the others)
            PROBLEMS.setdefault(court, []).append(f"reader failed: {type(e).__name__}: {e}")
            return [], []
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
    return rows, []


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


def write_summary(counts: dict[str, int], sess: dict[str, int] | None = None) -> None:
    lines = ["### Circuit calendars", "", "| Court | Cases | Session days | Problems |", "|---|---|---|---|"]
    for court in counts or COURTS:
        probs = "; ".join(PROBLEMS.get(court, [])[:4]) or "—"
        lines.append(f"| {court} | {counts.get(court, 0)} | {(sess or {}).get(court, 0) or '—'} | {probs} |")
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
    ap.add_argument("--manual", action="store_true",
                    help="owner-requested refresh of the courts marked manual (cadc, ca2); never use on a schedule")
    ap.add_argument("--force", action="store_true", help="with --manual: ignore the cooldown")
    a = ap.parse_args()
    out_path = Path(a.out)
    old = json.loads(out_path.read_text()) if out_path.exists() else {"cases": []}
    old.setdefault("sessions", [])
    refreshed = dict(old.get("manual_refreshed") or {})
    asked = [c for c in a.only.split(",") if c]
    if a.manual:
        only = [c for c in (asked or COURTS) if COURTS.get(c, {}).get("manual")]
        now = dt.datetime.now(dt.timezone.utc)
        for c in list(only):
            last = refreshed.get(c)
            if last and not a.force and now - dt.datetime.fromisoformat(last) < dt.timedelta(hours=MANUAL_COOLDOWN_H):
                print(f"::notice title={c}::refreshed {last}; the next refresh is allowed {MANUAL_COOLDOWN_H} h after that. Skipped.")
                only.remove(c)
        for c in only:
            for u in COURTS[c]["index"]:
                _owner_requested_hosts.add(urlparse(u).netloc)
    else:
        only = [c for c in (asked or COURTS) if not COURTS.get(c, {}).get("manual")]

    keep_from = (dt.date.today() - dt.timedelta(days=14)).isoformat()   # keep two weeks back for "recent"
    rows, sessions, counts = [], [], {}
    for court in only:
        got, sess = build_court(court, COURTS[court], keep_from)
        if not got:   # nothing new: keep this court's previous rows so a bad day doesn't blank it
            got = [c for c in old["cases"] if c["court"] == court and c["date"] >= keep_from]
        if not sess:
            sess = [s for s in old["sessions"] if s["court"] == court and s["date"] >= keep_from]
        counts[court] = len(got)
        if a.manual and COURTS[court].get("manual") and not PROBLEMS.get(court):
            refreshed[court] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
        rows += got
        sessions += sess
    # courts not run this time keep their old rows
    rows += [c for c in old["cases"] if c["court"] not in only and c["date"] >= keep_from]
    sessions += [s for s in old["sessions"] if s["court"] not in only and s["date"] >= keep_from]

    seen, uniq = set(), []
    for c in sorted(rows, key=lambda c: (c["date"], c["court"], c.get("time_local") or "", c["docket"])):
        if c["id"] not in seen:
            seen.add(c["id"])
            uniq.append(c)
    have = {(c["court"], c["date"]) for c in uniq}
    sessions = [s for s in {s["id"]: s for s in sessions}.values() if (s["court"], s["date"]) not in have]
    link_recordings(uniq)
    out_path.write_text(json.dumps({
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "courts": [c for c in COURTS if not COURTS[c].get("manual") or any(r["court"] == c for r in uniq)],
        "cases": uniq,
        "sessions": sorted(sessions, key=lambda s: (s["date"], s["court"])),
        "manual_refreshed": refreshed,
    }, indent=1, ensure_ascii=False))
    write_summary(counts, {c: sum(1 for s in sessions if s["court"] == c) for c in COURTS})


if __name__ == "__main__":
    main()
