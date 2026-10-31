"""Write a case brief (overview, plain question, why it matters) with the Claude API + web search.

Used by build_scotus.py now, and meant for the circuit calendars later. It needs
nothing court-specific: give it the case name, court, and whatever official text you
have (question presented, docket entries, Oyez facts); it searches for news and
commentary itself.

Every "why it matters" point must cite a page the model actually retrieved in this
request; points whose URL isn't among the search results are dropped, so the site
never shows a made-up link.

Env: ANTHROPIC_API_KEY (required), SUMMARY_MODEL (default claude-sonnet-5-5).
Cost: up to MAX_SEARCHES web searches per case ($10 per 1,000 searches) plus tokens.
"""
from __future__ import annotations

import json
import os
import re
import sys

import requests

API = "https://api.anthropic.com/v1/messages"
MAX_SEARCHES = 5

GUIDE = """You write short case briefs for a public calendar of oral arguments. Readers are curious
non-lawyers who are about to listen to the argument. Produce three parts, modeled on Oyez's
"Facts of the case" and "Question" but shorter and in your own words (never copy Oyez or any source verbatim):

1. overview: 60-110 words of plain narrative. Who the parties are, what happened, what the
   lower court(s) held. Concrete facts (places, amounts, dates) over abstractions.
2. question_plain: ONE sentence, a plain-English question a listener could answer yes/no or pick a side on.
3. why_it_matters: 1-4 points, each 1-2 sentences, drawn from news coverage, legal blogs, briefs and
   advocacy groups found by searching. Cover scope (who else is affected, circuit splits, related
   cases), real-world stakes, and, if the case has been argued, how the justices/judges seemed to lean.
   Be even-handed: on contested issues include the strongest point from each side and attribute
   characterizations to whoever made them. Every point needs a source URL from your search results.

Federal courts of appeals cases get far less coverage than Supreme Court cases. For them, also look
for the lower-court decision being appealed (CourtListener, Justia, Casetext, Google Scholar) and local
news. If facts can't be confirmed, keep the overview short and say what is unknown rather than guess;
one or two well-sourced "why it matters" points are better than four thin ones.

Rules: state only what the official text or a source supports; if sources disagree, say so; no
predictions of your own; no markdown. Search before writing (SCOTUSblog, Bloomberg Law, Reuters, AP,
law-firm alerts, Lawfare, Volokh, Balkinization, party and amicus press releases are all fair game).

Return ONLY a JSON object:
{"overview": "...", "question_plain": "...",
 "why_it_matters": [{"text": "...", "source_name": "Outlet (Author), Mon. D, YYYY", "source_url": "https://..."}]}"""


def _norm(u: str) -> str:
    return re.sub(r"[#?].*$", "", u.strip().rstrip("/")).lower().replace("://www.", "://")


def write_brief(*, title: str, court: str, docket: str, official_text: str,
                background: str = "", argued: bool = False) -> dict | None:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return None
    user = (f"Case: {title}\nCourt: {court}\nDocket: {docket}\n"
            f"Status: {'already argued; include how the argument went if coverage exists' if argued else 'not yet argued'}\n\n"
            f"Official text (questions presented / docket):\n{official_text[:6000]}\n\n"
            + (f"Background facts for context only (do not copy wording):\n{background[:3000]}\n\n" if background else "")
            + "Search for coverage and commentary, then return the JSON.")
    try:
        r = requests.post(API, timeout=180, headers={
            "x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json={
                "model": os.environ.get("SUMMARY_MODEL", "claude-sonnet-5-5"),
                "max_tokens": 2000,
                "system": GUIDE,
                "tools": [{"type": "web_search_20250305", "name": "web_search", "max_uses": MAX_SEARCHES}],
                "messages": [{"role": "user", "content": user}],
            })
        r.raise_for_status()
        blocks = r.json().get("content", [])
    except requests.RequestException as e:
        print(f"  ! brief failed for {docket}: {e}", file=sys.stderr)
        return None

    # URLs the model actually saw (search results + cited spans)
    seen: set[str] = set()
    for b in blocks:
        if b.get("type") == "web_search_tool_result" and isinstance(b.get("content"), list):
            seen |= {_norm(x["url"]) for x in b["content"] if x.get("url")}
        for c in b.get("citations") or []:
            if c.get("url"):
                seen.add(_norm(c["url"]))

    text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        print(f"  ! no JSON in brief for {docket}", file=sys.stderr)
        return None
    try:
        out = json.loads(m.group(0))
    except json.JSONDecodeError:
        print(f"  ! bad JSON in brief for {docket}", file=sys.stderr)
        return None

    why = []
    for w in out.get("why_it_matters", []):
        url = (w.get("source_url") or "").strip()
        if url.startswith("http") and _norm(url) in seen and w.get("text"):
            why.append({"text": w["text"].strip(), "source": {"name": (w.get("source_name") or url).strip(), "url": url}})
        else:
            print(f"  - dropped unsourced point for {docket}: {str(w.get('text'))[:60]}", file=sys.stderr)
    if not out.get("overview"):
        return None
    return {
        "overview": out["overview"].strip(),
        "question_plain": (out.get("question_plain") or "").strip(),
        "why_it_matters": why,
        "summary_source": "ai",
    }


def oyez_facts(term: int, docket: str) -> str:
    """Oyez's 'facts of the case' (SCOTUS only), used as background input, never published."""
    try:
        r = requests.get(f"https://api.oyez.org/cases/{term}/{docket}", timeout=20)
        if r.ok:
            return re.sub(r"<[^>]+>", " ", r.json().get("facts_of_the_case") or "").strip()
    except (requests.RequestException, ValueError):
        pass
    return ""
