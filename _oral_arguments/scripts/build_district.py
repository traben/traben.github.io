#!/usr/bin/env python3
"""Build courts/oral-arguments/data/district.json for the hand-picked district court cases.

For each case in _oral_arguments/district_cases.json:
  * pulls the latest docket entries from CourtListener (needs COURTLISTENER_TOKEN; free account)
  * finds hearings ("… set for 11/12/2026 at 10:00 AM in Courtroom 12"), whether they were held
    or vacated, and whether the public phone line was announced for them
  * lists upcoming deadlines ("… due by 11/30/2026") and the most recent docket activity
  * attaches the case brief: overview, plain question, why it matters, and case history.
    Hand-written briefs in _oral_arguments/editorial/district.json are the base; when the docket
    shows major new events (opinions, orders on motions, appeals, hearings held), Claude adds to the
    history, at most once a week per case (needs ANTHROPIC_API_KEY). Additions are kept in
    _oral_arguments/district_ai.json.

Usage:  python _oral_arguments/scripts/build_district.py [--refresh-brief CASE_ID]
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
from summarize import write_history_update  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
TOOLS = ROOT / "_oral_arguments"
DATA = ROOT / "courts" / "oral-arguments" / "data"
CL = "https://www.courtlistener.com"
UA = "oral-argument-calendar-bot/1.0 (+https://traben.github.io/courts/oral-arguments/)"
DCD_LINES = "https://www.dcd.uscourts.gov/public-access-teleconference-information"

HEARING_RE = re.compile(
    r"(?P<type>[A-Z][A-Za-z/'\- ]{2,60}?)\s+(?:re)?set for\s+(?P<date>\d{1,2}/\d{1,2}/\d{4})\s+at\s+"
    r"(?P<time>\d{1,2}:\d{2}\s*[AP]M)(?:\s+in\s+(?P<room>Courtroom\s+\w+|[A-Z][\w ]{2,30}?(?=\s+(?:before|Judge)|\.|$)))?", re.I)
HELD_RE = re.compile(r"(?P<type>[A-Z][A-Za-z/'\- ]{2,60}?)\s+held on\s+(?P<date>\d{1,2}/\d{1,2}/\d{4})", re.I)
DUE_RE = re.compile(r"(?P<what>[A-Z][A-Za-z0-9/'\-,() ]{2,80}?)\s+due\s+(?:by|on)\s+(?P<date>\d{1,2}/\d{1,2}/\d{4})", re.I)
SIGNIFICANT_RE = re.compile(r"MEMORANDUM|OPINION|NOTICE OF APPEAL|ORDER (?:granting|denying)|"
                            r"held on|INJUNCTION|JUDGMENT|USCA|MOTION for (?:Preliminary|Summary|Temporary)", re.I)

session = requests.Session()
session.headers["User-Agent"] = UA


def mdY(s: str) -> str:
    m, d, y = s.split("/")
    return f"{int(y):04d}-{int(m):02d}-{int(d):02d}"


def hhmm(s: str) -> str:
    t = dt.datetime.strptime(re.sub(r"\s+", " ", s.upper().strip()).replace(" AM", "AM").replace(" PM", "PM"), "%I:%M%p")
    return t.strftime("%H:%M")


def load(p: Path, default):
    return json.loads(p.read_text()) if p.exists() else default


def fetch_entries(docket_id: int, pages: int = 3) -> list[dict] | None:
    token = os.environ.get("COURTLISTENER_TOKEN")
    if not token:
        print("::warning title=District cases::COURTLISTENER_TOKEN is not set; district dockets can't be refreshed.")
        return None
    url = f"{CL}/api/rest/v4/docket-entries/"
    params = {"docket": docket_id, "order_by": "-recap_sequence_number",
              "fields": "entry_number,date_filed,description,recap_documents", "page_size": 100}
    out = []
    for _ in range(pages):
        r = session.get(url, params=params, headers={"Authorization": f"Token {token}"}, timeout=40)
        if r.status_code != 200:
            print(f"::warning title=District cases::CourtListener returned HTTP {r.status_code} for docket {docket_id}")
            return out or None
        body = r.json()
        out += body.get("results", [])
        url, params = body.get("next"), None
        if not url:
            break
    return out


def parse(entries: list[dict], today: str) -> dict:
    """Hearings, deadlines and recent activity from docket entry text (newest first in `entries`)."""
    hearings: dict[tuple, dict] = {}
    for e in sorted(entries, key=lambda e: (e.get("date_filed") or "")):   # oldest first so resets win
        text = (e.get("description") or "").strip()
        for m in HEARING_RE.finditer(text):
            date, typ = mdY(m["date"]), m["type"].strip()
            typ = re.sub(r"^(?:NOTICE of Hearing:|Set/Reset (?:Deadlines/)?Hearings:)\s*", "", typ, flags=re.I).strip()
            typ = re.sub(r"\s+(?:is|was|has been|will be|is hereby)$", "", typ, flags=re.I).strip()   # "Evidentiary Hearing is set for"
            h = hearings.setdefault((date, typ.lower()), {"date": date, "type": typ})
            h.update({"time": hhmm(m["time"]), "room": (m["room"] or "").strip() or None, "status": "scheduled",
                      "public_line": h.get("public_line", False) or bool(re.search(r"public access line", text, re.I)),
                      "noticed": e.get("date_filed")})
        for m in HELD_RE.finditer(text):
            date = mdY(m["date"])
            for h in hearings.values():
                if h["date"] == date:
                    h["status"] = "held"
        if re.search(r"vacat|cancel", text, re.I):
            # only dates in the sentence that does the vacating ("The motion presentment dates of
            # 11/05/2026 and 11/10/2026 are hereby vacated" must not cancel a hearing set earlier in the entry)
            for sent in re.split(r"(?<=[.;])\s+(?=[A-Z])", text):
                if not re.search(r"vacat|cancel", sent, re.I) or HEARING_RE.search(sent):
                    continue
                gone = {mdY(d) for d in re.findall(r"\d{1,2}/\d{1,2}/\d{4}", sent)}
                for h in hearings.values():
                    if h["date"] in gone and h["status"] == "scheduled" and (h.get("noticed") or "") <= (e.get("date_filed") or ""):
                        h["status"] = "vacated"
    # second pass: minute orders announcing the public line for a hearing
    # ("The hearing set for tomorrow, October 30, 2026, ... will be accessible to the public via ... dial-in line")
    for e in entries:
        text = e.get("description") or ""
        if not re.search(r"accessible to the public via the Court's public access", text, re.I):
            continue
        filed = e.get("date_filed") or ""
        dates = {mdY(d) for d in re.findall(r"\d{1,2}/\d{1,2}/\d{4}", text)}
        for mon, day, yr in re.findall(r"(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{1,2}),\s+(\d{4})", text):
            dates.add(dt.datetime.strptime(f"{mon} {day} {yr}", "%B %d %Y").date().isoformat())
        for h in hearings.values():
            soon = filed and 0 <= (dt.date.fromisoformat(h["date"]) - dt.date.fromisoformat(filed)).days <= 1
            if h["date"] in dates or (soon and re.search(r"\b(today|tomorrow|this (?:morning|afternoon))\b", text, re.I)):
                h["public_line"] = True

    deadlines = {}
    for e in entries:
        text = e.get("description") or ""
        if not re.search(r"Set/Reset Deadlines|ORDER", text):
            continue
        for m in DUE_RE.finditer(text):
            date = mdY(m["date"])
            if date >= today:
                what = re.sub(r"^(?:Set/Reset Deadlines(?:/Hearings)?:)\s*", "", m["what"].strip(), flags=re.I)
                deadlines.setdefault((date, what.lower()), {"date": date, "what": what[:120]})

    # deadlines written out in minute orders: "By November 2, 2026, at 10:00 a.m., the Government will file ..."
    MONTH = r"(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{1,2}),\s+(\d{4})"
    for e in entries:
        text = e.get("description") or ""
        if not re.match(r"\s*(MINUTE )?ORDER", text, re.I):
            continue
        for sent in re.split(r"(?<!Dkt\.)(?<!No\.)(?<!Nos\.)(?<!U\.S\.)(?<=[.;:])\s+(?=[A-Z(\d])", text):
            m = re.search(r"\b(?:[Bb]y|[Oo]n or before)\s+(?:tomorrow,\s+)?"
                          r"(?:\d{1,2}:\d{2}\s*[ap]\.?m\.?,?\s+on\s+)?" + MONTH, sent)
            if not m:
                continue
            date = dt.datetime.strptime(f"{m.group(1)} {m.group(2)} {m.group(3)}", "%B %d %Y").date().isoformat()
            if date < today:
                continue
            what = re.sub(r"^\(?\d\)?\.?\s*", "", sent.strip())
            what = re.sub(r"^(The Court (?:FURTHER )?ORDERS (?:that )?)", "", what)
            what = what[:1].upper() + what[1:]
            deadlines.setdefault((date, what.lower()), {"date": date, "what": what[:180] + ("…" if len(what) > 180 else "")})

    recent = []
    for e in entries:
        text = (e.get("description") or "").strip()
        if not text or re.match(r"(NOTICE of Appearance|RETURN OF SERVICE|LCvR|SUMMONS|MOTION for Leave to Appear Pro Hac)", text, re.I):
            continue
        doc = next((d.get("filepath_local") for d in (e.get("recap_documents") or []) if isinstance(d, dict) and d.get("filepath_local")), None)
        recent.append({"date": e.get("date_filed"), "entry": e.get("entry_number"),
                       "text": text[:600] + ("…" if len(text) > 600 else ""),
                       "doc": f"https://storage.courtlistener.com/{doc}" if doc else None})
        if len(recent) >= 10:
            break
    return {"hearings": sorted(hearings.values(), key=lambda h: (h["date"], h.get("time") or "")),
            "deadlines": sorted(deadlines.values(), key=lambda d: d["date"])[:15],
            "recent": recent}


def dcd_line(judge: str) -> dict | None:
    """D.D.C. publishes one toll-free number and a meeting ID per judge."""
    try:
        html = session.get(DCD_LINES, timeout=30).text
    except requests.RequestException:
        return None
    last = judge.split()[-1]
    m = re.search(rf"{re.escape(last)}[^<]{{0,60}}</td>\s*<td[^>]*>\s*([\d\-]{{10,14}})\s*</td>\s*<td[^>]*>\s*(\d{{6,12}})", html, re.I)
    if not m:
        return None
    return {"phone": m.group(1), "code": m.group(2), "source": DCD_LINES}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh-brief", default="", help="case id to re-brief now, regardless of the weekly limit")
    a = ap.parse_args()
    today = dt.date.today().isoformat()
    cases = load(TOOLS / "district_cases.json", {"cases": []})["cases"]
    editorial = load(TOOLS / "editorial" / "district.json", {"sources": {}, "cases": {}})
    ai = load(TOOLS / "district_ai.json", {"cases": {}})
    old = {c["id"]: c for c in load(DATA / "district.json", {"cases": []})["cases"]}
    out = []
    for c in cases:
        prev = old.get(c["id"], {})
        entries = fetch_entries(c["courtlistener_docket"])
        parsed = parse(entries, today) if entries else {k: prev.get(k, []) for k in ("hearings", "deadlines", "recent")}

        listen = c.get("listen") or prev.get("listen")
        if not listen and c["court"] == "dcd" and c.get("judge"):
            line = dcd_line(c["judge"])
            if line:
                listen = {**line, "note": "The judge decides hearing by hearing whether to open the public line; "
                          "the docket says so when it's available. Recording or rebroadcasting is prohibited."}

        # brief = hand-written base + AI additions
        ed = editorial["cases"].get(c["id"], {})
        srcs = editorial.get("sources", {})
        add = ai["cases"].get(c["id"], {})
        through = max(filter(None, [ed.get("through"), add.get("through")]), default="")
        new_major = [e for e in (entries or []) if (e.get("date_filed") or "") > through
                     and SIGNIFICANT_RE.search(e.get("description") or "")]
        due = (a.refresh_brief == c["id"]) or (new_major and (not add.get("written") or
               (dt.date.fromisoformat(today) - dt.date.fromisoformat(add["written"])).days >= 7))
        if due and os.environ.get("ANTHROPIC_API_KEY"):
            upd = write_history_update(
                title=c["title"], court=c["court_name"], docket=c["docket_number"],
                overview=add.get("overview") or ed.get("overview", ""),
                history=[h["text"] for h in (ed.get("history", []) + add.get("history", []))],
                new_entries=[f"{e.get('date_filed')}: {e.get('description')}" for e in new_major][:40],
                docket_url=f"{CL}/docket/{c['courtlistener_docket']}/")
            if upd:
                add = {"through": today, "written": today,
                       "overview": upd.get("overview") or add.get("overview"),
                       "history": add.get("history", []) + upd.get("history", [])}
                ai["cases"][c["id"]] = add

        def src(key_or_obj):
            return srcs.get(key_or_obj) if isinstance(key_or_obj, str) else key_or_obj

        history = [{"date": h["date"], "text": h["text"], "source": src(h["src"])} for h in ed.get("history", [])]
        history += add.get("history", [])
        out.append({
            "id": c["id"], "court": c["court"], "court_name": c["court_name"], "title": c["title"],
            "docket_number": c["docket_number"], "judge": c.get("judge"), "tz": c.get("tz", "America/New_York"),
            "links": {"docket": f"{CL}/docket/{c['courtlistener_docket']}/"},
            "related": c.get("related", []),
            "status_note": c.get("status_note"),      # e.g. "district case stayed; en banc decision pending"
            "listen": listen,
            **parsed,
            "brief": {
                "overview": add.get("overview") or ed.get("overview"),
                "question_plain": ed.get("question_plain"),
                "why_it_matters": [{"text": w["text"], "source": src(w["src"])} for w in ed.get("why_it_matters", [])],
                "history": sorted(history, key=lambda h: h["date"]),
                "through": add.get("through") or ed.get("through"),
                "author": "Claude (AI)",
            },
            "updated": today if entries else prev.get("updated"),
        })
    (TOOLS / "district_ai.json").write_text(json.dumps(ai, indent=1, ensure_ascii=False))
    (DATA / "district.json").write_text(json.dumps({
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), "cases": out},
        indent=1, ensure_ascii=False))
    print(f"wrote {len(out)} district case(s)")


if __name__ == "__main__":
    main()
