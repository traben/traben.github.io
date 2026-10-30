#!/usr/bin/env python3
"""Build data/scotus.json: Supreme Court argument calendar, listen links and summaries.

Sources (all public, supremecourt.gov):
  * Calendars page  -> links to the monthly argument-calendar PDFs
  * Calendar PDFs   -> which docket numbers are set for argument, and their captions
  * Docket JSON     -> "SET FOR ARGUMENT on <date>", lower court     (/RSS/Cases/JSON/<docket>.json)
  * QP PDFs         -> the Questions Presented                       (/qp/<yy>-<nnnnn>qp.pdf)
  * Audio page      -> which cases have been argued and have recordings

Case briefs (overview + plain question + "why it matters" with sources):
  1. hand-written briefs in _oral_arguments/editorial/*.json always win;
  2. otherwise an existing brief in courts/oral-arguments/data/scotus.json is kept;
  3. otherwise, if ANTHROPIC_API_KEY is set, scripts/summarize.py writes one using web
     search (and refreshes it once after the argument);
  4. with no key, the site shows the official Question Presented instead.

Usage:  python _oral_arguments/scripts/build_scotus.py [--out data/scotus.json] [--no-summaries]
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
from pathlib import Path

import requests
from pypdf import PdfReader

sys.path.insert(0, str(Path(__file__).resolve().parent))
from summarize import oyez_facts, write_brief  # noqa: E402

BASE = "https://www.supremecourt.gov"
CAL_PAGE = f"{BASE}/oral_arguments/calendarsandlists.aspx"
UA = {"User-Agent": "oral-argument-calendar (github.com/traben; personal, non-commercial)"}
ROOT = Path(__file__).resolve().parents[2]          # the traben.github.io repo
TOOLS = ROOT / "_oral_arguments"                      # scripts + hand-written briefs (not published)
DATA = ROOT / "courts" / "oral-arguments" / "data"   # published JSON the page reads

DOCKET_RE = re.compile(r"\b(\d{2}-\d{1,5})\b")
SET_FOR_ARG_RE = re.compile(r"SET FOR ARGUMENT on (?:\w+, )?(\w+ \d{1,2}, \d{4})", re.I)
MONTHS = "January February March April May June July August September October November December".split()


# --------------------------------------------------------------------------- fetch helpers
session = requests.Session()
session.headers.update(UA)


def get(url: str, *, binary: bool = False, tries: int = 3):
    for i in range(tries):
        try:
            r = session.get(url, timeout=30)
            if r.status_code == 404:
                return None
            r.raise_for_status()
            return r.content if binary else r.text
        except requests.RequestException as e:
            if i == tries - 1:
                print(f"  ! {url}: {e}", file=sys.stderr)
                return None
            time.sleep(2 * (i + 1))


def pdf_text(data: bytes) -> str:
    reader = PdfReader(io.BytesIO(data))
    return "\n".join((p.extract_text() or "") for p in reader.pages)


# --------------------------------------------------------------------------- parsing
def calendar_pdf_links(html: str, today: dt.date) -> list[str]:
    """Monthly argument-calendar PDFs for the current and next months (and last month)."""
    links = sorted(set(re.findall(r'href="([^"]*MonthlyArgumentCal(\w+?)(\d{4})\.pdf)"', html)))
    keep = []
    for href, month, year in links:
        try:
            m = MONTHS.index(month) + 1
        except ValueError:
            continue
        first = dt.date(int(year), m, 1)
        # keep calendars from ~2 months ago forward (covers "recently argued")
        if first >= (today.replace(day=1) - dt.timedelta(days=62)):
            keep.append(href if href.startswith("http") else f"{BASE}/{href.lstrip('/')}")
    return keep


def captions_from_calendar(text: str) -> dict[str, str]:
    """Docket -> caption from calendar text like '25-581 ST. MARY CATHOLIC\\nPARISH V. ROY'."""
    lines = [l.strip() for l in text.splitlines()]
    out: dict[str, str] = {}
    stop = re.compile(r"^(\(\d+\)|\(Consolidated|Monday|Tuesday|Wednesday|Thursday|Friday|Court [Cc]onvenes|LEGAL HOLIDAY|$)")
    i = 0
    while i < len(lines):
        m = re.match(r"^(\d{2}-\d{1,5})\)?\s+(.*)$", lines[i])
        if m:
            docket, cap = m.group(1), [m.group(2)]
            j = i + 1
            while j < len(lines) and not stop.match(lines[j]) and not DOCKET_RE.match(lines[j]):
                cap.append(lines[j])
                j += 1
            out[docket] = " ".join(c for c in cap if c)
            i = j
        else:
            i += 1
    return out


SMALL = {"v.", "of", "the", "and", "for", "in", "on", "at", "to", "a", "an", "de"}
KEEP_UPPER = {"LLC", "LLP", "USA", "U.S.", "FBI", "SEC", "EPA", "NLRB", "FCC", "FTC", "II", "III", "IV", "N.A.", "D.", "V.", "AT&T"}


ABBREV = {"Dept.": "Department", "Univ.": "University", "Natl.": "National", "Assn.": "Association", "Corp": "Corp."}


def title_case(caption: str) -> str:
    caption = re.sub(r"\b([A-Z])\. ([A-Z])\. ([A-Z])\.\s*$", r"\1.\2.\3.", caption.strip())  # "... V. D. V. D." -> "V. D.V.D."
    caption = re.sub(r"^([A-Z])\. ([A-Z])\. ([A-Z])\. (?=V\.)", r"\1.\2.\3. ", caption)
    words = re.sub(r"\s+", " ", caption).strip().split(" ")
    out = []
    for k, w in enumerate(words):
        lw = w.lower()
        if w in KEEP_UPPER and w != "V.":
            out.append(w)
        elif lw == "v.":
            out.append("v.")
        elif k and lw in SMALL:
            out.append(lw)
        elif re.fullmatch(r"([A-Z]\.){2,}", w):  # initials like D.V.D.
            out.append(w)
        else:
            out.append("-".join(p[:1].upper() + p[1:].lower() for p in w.split("-")))
    s = " ".join(ABBREV.get(w, w) for w in out)
    s = s.replace("Att’y Gen.", "Attorney General").replace("Att'y Gen.", "Attorney General")
    s = re.sub(r"\bInc\b\.?", "Inc.", s)
    return re.sub(r",\s*Attorney General$", "", s)


def parse_date(s: str) -> str | None:
    try:
        return dt.datetime.strptime(s.strip(), "%B %d, %Y").date().isoformat()
    except ValueError:
        return None


def qp_url(docket: str) -> str:
    y, n = docket.split("-")
    return f"{BASE}/qp/{y}-{int(n):05d}qp.pdf"


def docket_page(docket: str) -> str:
    return f"{BASE}/search.aspx?filename=/docket/docketfiles/html/public/{docket}.html"


def clean_qp(text: str) -> str:
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"-\n(\w)", r"\1", text)
    text = re.sub(r"\s*\n\s*", " ", text)
    return re.sub(r"\s{2,}", " ", text).strip()


def audio_index(term: int) -> dict[str, str]:
    """docket -> argued date (YYYY-MM-DD) from the term's audio page."""
    html = get(f"{BASE}/oral_arguments/argument_audio/{term}") or ""
    out = {}
    for docket, mmddyy in re.findall(r'audio/\d{4}/(\d{2}-\d{1,5})["\'][^<]*</a>.*?(\d{2}/\d{2}/\d{2})', html, re.S):
        mm, dd, yy = mmddyy.split("/")
        out[docket] = f"20{yy}-{mm}-{dd}"
    return out


# --------------------------------------------------------------------------- briefs
BRIEF_KEYS = ("overview", "question_plain", "why_it_matters", "summary_source", "summary_checked", "brief_after_argument")


def load_editorial() -> dict[str, dict]:
    """_oral_arguments/editorial/*.json -> {docket: {overview, question_plain, why_it_matters, ...}}"""
    out: dict[str, dict] = {}
    for f in sorted((TOOLS / "editorial").glob("*.json")):
        d = json.loads(f.read_text())
        srcs = d.get("sources", {})
        for docket, e in d.get("cases", {}).items():
            out[docket] = {
                "overview": e["overview"],
                "question_plain": e.get("question_plain", ""),
                "why_it_matters": [{"text": w["text"], "source": srcs[w["src"]]} for w in e.get("why_it_matters", [])],
                "summary_source": "editor",
            }
    return out


# --------------------------------------------------------------------------- main
def term_for(date: dt.date) -> int:
    """October Term N runs from the first Monday in October of year N."""
    return date.year if date.month >= 10 else date.year - 1


def build(out_path: Path, want_summaries: bool) -> None:
    today = dt.date.today()
    old = {}
    if out_path.exists():
        old = {c["docket"]: c for c in json.loads(out_path.read_text()).get("cases", [])}

    html = get(CAL_PAGE) or ""
    pdfs = calendar_pdf_links(html, today)
    print(f"calendar PDFs: {len(pdfs)}")

    captions: dict[str, str] = {}
    for url in pdfs:
        data = get(url, binary=True)
        if data:
            captions.update(captions_from_calendar(pdf_text(data)))
    print(f"dockets on calendars: {len(captions)}")

    terms = {term_for(today), term_for(today) - 1}
    argued: dict[str, str] = {}
    for t in terms:
        argued.update(audio_index(t))

    editorial = load_editorial()
    cases = []
    for docket, caption in captions.items():
        prev = old.get(docket, {})
        info = json.loads(get(f"{BASE}/RSS/Cases/JSON/{docket}.json") or "{}")
        date = None
        for p in info.get("ProceedingsandOrder", []):
            m = SET_FOR_ARG_RE.search(p.get("Text", ""))
            if m:
                date = parse_date(m.group(1)) or date  # keep the latest setting (re-sets happen)
        date = argued.get(docket) or date or prev.get("date")
        if not date:
            print(f"  ? no argument date for {docket}")
            continue

        qp_raw = get(qp_url(docket), binary=True)
        qp = clean_qp(pdf_text(qp_raw)) if qp_raw else prev.get("question_full", "")
        m = re.search(r"QUESTIONS? PRESENTED:?\s*(.*?)(?:LOWER COURT CASE NUMBER|$)", qp, re.S)
        granted = re.search(r"GRANTED LIMITED TO (?:THE FOLLOWING QUESTIONS?:)?\s*(.*?)(?:CERT\. GRANTED|ORDER OF|QUESTION PRESENTED)", qp, re.S)
        question = prev.get("question") or ((granted.group(1) if granted else (m.group(1) if m else qp)).strip()[:1200])
        note = prev.get("note")
        if not note and re.search(r"CONSOLIDATED WITH ([\d-]+)", qp):
            note = "Consolidated with " + re.search(r"CONSOLIDATED WITH ([\d-]+)", qp).group(1) + " for argument."

        lower = info.get("LowerCourt") or prev.get("lower_court")
        title = prev.get("title") or title_case(caption)
        t = term_for(dt.date.fromisoformat(date))
        is_argued = docket in argued

        # Case brief: keep what we have; write one for new cases; refresh an AI brief once
        # after argument so "why it matters" can include how the argument went.
        brief = {k: prev[k] for k in BRIEF_KEYS if k in prev}
        stale = prev.get("summary_source") == "ai" and is_argued and not prev.get("brief_after_argument")
        if want_summaries and (not brief.get("overview") or stale) and docket not in editorial:
            new = write_brief(title=title, court="Supreme Court of the United States", docket=docket,
                              official_text=qp, background=oyez_facts(term_for(dt.date.fromisoformat(date)), docket),
                              argued=is_argued)
            if new:
                brief = new | {"brief_after_argument": is_argued}

        cases.append({
            "id": f"scotus-{docket}",
            "court": "scotus",
            "docket": docket,
            "title": title,
            "date": date,
            "time_et": prev.get("time_et", "10:00"),
            "time_note": "Court convenes at 10 a.m. ET; cases are heard in calendar order, so later cases start after the earlier argument ends.",
            "order": prev.get("order", 1),
            "lower_court": lower,
            "question": question,
            "question_full": qp[:8000],
            **brief,
            "note": note,
            "argued": is_argued,
            "links": {
                "recording": f"{BASE}/oral_arguments/audio/{t}/{docket}" if is_argued else None,
                "recording_expected": f"{BASE}/oral_arguments/audio/{t}/{docket}",
                "live": f"{BASE}/",
                "qp": qp_url(docket),
                "docket": docket_page(docket),
            },
        })
        time.sleep(0.5)  # be polite

    # order within a day: the calendar lists cases in argument order
    order_in_cal = {d: i for i, d in enumerate(captions)}
    cases.sort(key=lambda c: (c["date"], order_in_cal.get(c["docket"], 99)))
    by_day: dict[str, int] = {}
    slot_of: dict[str, int] = {}
    for c in cases:
        partner = re.search(r"Consolidated with ([\d-]+)", c.get("note") or "")
        if partner and partner.group(1) in slot_of:
            slot = slot_of[partner.group(1)]           # consolidated cases share one slot
        else:
            by_day[c["date"]] = by_day.get(c["date"], 0) + 1
            slot = by_day[c["date"]]
        slot_of[c["docket"]] = slot
        if "order" not in old.get(c["docket"], {}):
            c["order"] = slot
            c["time_et"] = "10:00" if slot == 1 else "11:00"

    # Hand-written briefs (_oral_arguments/editorial/*.json) win over AI ones.
    for c in cases:
        if c["docket"] in editorial:
            c.update(editorial[c["docket"]])

    # Manual fixes win: _oral_arguments/overrides.json  {"25-159": {"title": "...", "summary": "..."}}
    ov_path = TOOLS / "overrides.json"
    overrides = json.loads(ov_path.read_text()) if ov_path.exists() else {}
    for c in cases:
        for k, v in overrides.get(c["docket"], {}).items():
            c[k] = v

    out = {
        "court": "scotus",
        "name": "Supreme Court of the United States",
        "term": f"October Term {term_for(today)}",
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "sources": [CAL_PAGE, f"{BASE}/oral_arguments/argument_audio/{term_for(today)}",
                    f"{BASE}/RSS/Cases/JSON/{{docket}}.json", f"{BASE}/qp/"],
        "cases": cases,
    }
    if not cases and old:
        print("No cases parsed; keeping the existing file so the site does not go blank.", file=sys.stderr)
        sys.exit(1)
    out_path.write_text(json.dumps(out, indent=2, ensure_ascii=False))
    print(f"wrote {len(cases)} cases -> {out_path}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(DATA / "scotus.json"))
    ap.add_argument("--no-summaries", action="store_true")
    a = ap.parse_args()
    build(Path(a.out), not a.no_summaries)
