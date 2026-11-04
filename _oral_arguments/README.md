# Oral Argument Calendar

A static site that answers three questions for each court: **what's being argued and when, how to listen, and what the case is about.**

Priority order: (1) U.S. Supreme Court, (2) federal courts of appeals, (3) selected district-court hearings.

Lives inside the traben.github.io Jekyll site at **https://traben.github.io/courts/oral-arguments/** and is linked from the Courts page.

| Path | What it is |
|---|---|
| `courts/oral-arguments/` | the page (`index.html` uses the site's `archive` layout), `app.js`, `style.css`, and `data/*.json` the page reads |
| `_oral_arguments/` | build scripts, hand-written briefs, overrides. The leading underscore keeps Jekyll from publishing it |
| `.github/workflows/update-court-data.yml` | runs the scripts daily, commits new data, and asks Pages to rebuild |

## What works now

| Court | Upcoming calendar | Listen | Summaries |
|---|---|---|---|
| Supreme Court | ✅ built daily from supremecourt.gov | live link on argument days; recording link once posted (same day) | ✅ overview, plain-English question, and sourced "why it matters" commentary |
| Courts of Appeals (13) | ✅ 1st, 3rd, 4th, 5th, 7th, 8th, 9th, 10th, 11th, Fed. (cases); 6th (sitting days until cases post); 2nd and D.C. linked only | ✅ live-stream and YouTube links per court; last 10 days of recordings playable in-page (via CourtListener) | AI summaries for cases you select |
| District courts | ⏳ later | — | — |

## How it works

```
GitHub Action (8 a.m. + 5 p.m. ET)          GitHub Pages (static)
 ├─ _oral_arguments/scripts/build_scotus.py   ──► courts/oral-arguments/data/scotus.json ─┐
 ├─ _oral_arguments/scripts/build_circuits.py ──► courts/oral-arguments/data/circuits_recent.json ─┼─► courts/oral-arguments/index.html + app.js
 └─ commits data/ if changed      courts/oral-arguments/courts.json (hand-maintained) ─┘
```

**Supreme Court pipeline** (`build_scotus.py`)
1. Reads the [Calendars and Lists](https://www.supremecourt.gov/oral_arguments/calendarsandlists.aspx) page and downloads recent monthly argument-calendar PDFs → docket numbers + captions.
2. For each docket, reads the Court's docket JSON (`/RSS/Cases/JSON/<docket>.json`) for the `SET FOR ARGUMENT on …` date and the lower court.
3. Downloads the Questions Presented PDF (`/qp/<yy>-<nnnnn>qp.pdf`), and uses the "granted limited to" text when the Court narrowed the questions.
4. Reads the term's [argument-audio page](https://www.supremecourt.gov/oral_arguments/argument_audio/2026) to mark which cases have recordings.
5. Attaches a **case brief**, modeled on Oyez's "facts + question" but shorter and in our own words, plus commentary:
   - `overview`: 60–110 words on who, what happened, and what the lower court did;
   - `question_plain`: one plain-English sentence;
   - `why_it_matters`: 2–4 points on scope, stakes and context from news, legal blogs, briefs and advocacy groups, balanced across sides, **each linked to its source**.

   Hand-written briefs in `_oral_arguments/editorial/*.json` always win. For new cases, `_oral_arguments/scripts/summarize.py` asks the Claude API (with web search) to draft one, and drops any point whose link wasn't actually among the search results. An AI brief is refreshed once after the argument so it can say how the argument went. Every summary on the site is labeled as written by Claude (AI), and both the calendar page and the Courts page carry a written notice saying so.
6. Applies manual fixes from `_oral_arguments/overrides.json` last.

**Circuits** (`build_circuits.py`) pulls recent recordings from CourtListener's oral-argument search for `ca1`–`ca11`, `cadc`, `cafc`.

## Setup (one-time)

1. Repo **Settings → Actions → General → Workflow permissions**: choose **Read and write permissions**.
2. Optional secrets (**Settings → Secrets and variables → Actions**): `ANTHROPIC_API_KEY` for AI-drafted briefs on new cases (about 5 web searches per case, a few cents each); `COURTLISTENER_TOKEN` (free) to avoid rate limits.
3. **Actions → Update oral argument data → Run workflow** for the first refresh.

## AI summaries for selected appeals cases

Only the appeals cases you pick get an AI summary (Supreme Court cases all do). The list is
`_oral_arguments/selected_cases.json`; summaries are written to `courts/oral-arguments/data/circuit_briefs.json`.

**Add a case, either way:**
1. **From the calendar.** Visit `https://traben.github.io/courts/oral-arguments/?owner=1` once in your browser to turn on owner mode (`?owner=0` turns it off; it is remembered per browser and shows nothing special to visitors). Each upcoming appeals case without a summary then shows **✦ Request AI summary**. It opens a pre-filled GitHub issue (`Summarize ca4 25-2342`); click **Submit new issue**. The *Summarize an appeals case* workflow adds the case to the list, writes the summary, commits, and closes the issue with a comment. Issues from anyone but the repo owner are ignored.
2. **By hand.** Edit `selected_cases.json` on github.com and add `{"court": "ca4", "docket": "25-2342"}`. For a case on a calendar the site doesn't read (2nd, D.C.) add `"title": "Case name"` too. The daily run picks it up. Or use **Actions → Summarize an appeals case → Run workflow**.

To remove a summary, delete the entry from the list; the next run drops it. A summary is refreshed once after the argument so it can say how the argument went. Each one costs a few cents (up to 5 web searches plus tokens). Requires the `ANTHROPIC_API_KEY` secret.

## District court cases (hand-picked)

Tracked cases are listed in `_oral_arguments/district_cases.json` (CourtListener docket id, title, judge, related appeals). Each day `scripts/build_district.py`:
- pulls the latest docket entries from CourtListener (**needs the `COURTLISTENER_TOKEN` secret**; a free CourtListener account gives you one at courtlistener.com/profile/api-token);
- finds hearings from entries like "Motion Hearing set for 11/12/2026 at 2:30 PM in Courtroom 12", marks them held or vacated, and notes when the judge announces the public phone line;
- lists upcoming deadlines (from "due by" entries and from dates written out in minute orders) and the ten most recent docket entries;
- for D.D.C., looks up the judge's public line on the court's teleconference page.

The case brief (overview, plain question, why it matters, case history) starts from a hand-written version in `_oral_arguments/editorial/district.json`, written when the case is added. When the docket shows major new events (opinions, orders on motions, appeals, hearings held), Claude adds to the history at most once a week (needs `ANTHROPIC_API_KEY`); additions are stored in `_oral_arguments/district_ai.json` and labeled on the page.

**Adding a case:** add an entry to `district_cases.json` and ask Claude to write the starting brief into `editorial/district.json` (or leave it out; the first major docket event will start one).

## D.C. and 2nd Circuits: refresh on request

Both courts' calendar pages are closed to automated readers by robots.txt (`media.cadc.uscourts.gov` disallows `/calendar/`; HuntCal disallows `/cal/`). So the daily job never reads them. Instead:

- In owner mode (`?owner=1`), the Courts of Appeals tab shows **↻ Refresh D.C. & 2nd Circuit calendars**. It opens a pre-filled issue titled "Refresh calendars"; submitting it runs `.github/workflows/refresh-calendars.yml`, which runs `build_circuit_calendars.py --manual`, commits, and replies on the issue. You can also run that workflow from the Actions tab. Issues from anyone but the owner are ignored.
- A refresh reads each court at most once per 12 hours (`MANUAL_COOLDOWN_H`); earlier requests are skipped. Its user agent says the refresh was requested by a person. The D.C. Circuit refresh is one page. The 2nd Circuit's is one page per month plus one per upcoming argument day (about 20–30 pages), 6 seconds apart because HuntCal rate-limits; if it answers 429 (too many requests), the script waits as asked and retries, and after repeated 429s stops and keeps the earlier data. A refresh cut short isn't counted against the 12-hour limit, so you can try again later.
- These cases are labeled on the page as refreshed on request, with the time, and may be out of date; each links to the court's own calendar.
- This is the owner's choice to make a person-requested exception to those robots.txt rules. If either court asks you to stop, stop clicking the button (or delete the workflow), and remove `cadc`/`ca2` from `COURTS`.

## Shipping updates (rule)

The daily job keeps these files current on GitHub, so code updates must **never** include them:
`courts/oral-arguments/data/*.json`, `_oral_arguments/debug/`, `_oral_arguments/district_ai.json`,
`_oral_arguments/selected_cases.json` (edited by you and by the summary-request workflow).
The court directory moved to `courts/oral-arguments/courts.json` (outside `data/`) so it can ship with code updates; the old
`data/courts.json` is no longer used and can be deleted.
Updates should contain only scripts, page files (`index.html`, `app.js`, `style.css`, `courts.json`), workflows, docs and
hand-written editorial files that the job never writes.

## Run locally

```bash
pip install -r _oral_arguments/requirements.txt
python _oral_arguments/scripts/build_scotus.py            # add --no-summaries to skip the API
python _oral_arguments/scripts/build_circuits.py --days 10
bundle exec jekyll serve                  # then open http://localhost:4000/courts/oral-arguments/
```

## Editing briefs

- To rewrite a case brief, add or edit it in `_oral_arguments/editorial/scotus_ot2026.json` (or a new file in that folder). Each `why_it_matters` point names a key from that file's `sources`.
- For one-off field fixes, use `_oral_arguments/overrides.json`:

```json
{ "25-159": { "title": "Hoffmann v. WBI Energy Transmission, Inc." } }
```

## Roadmap

- **Circuit calendars.** `scripts/build_circuit_calendars.py` writes `courts/oral-arguments/data/circuits_upcoming.json` (cases, plus `sessions` for courts that only publish sitting days). Settings per court are in `COURTS`. How each court is read:

  | Court | Source | Notes |
  |---|---|---|
  | 1st, 4th, 8th, 10th, 11th, Fed. | calendar PDFs | generic line parser (`parse_pdf_text`); 4th has its own (`parse_ca4`) |
  | 3rd | month grid on www.ca3.uscourts.gov/calendar → the court's Power Pages portal | the portal's own landing-page flow turns a date into a case-list id; the two grids (argued / submitted) come back as JSON. www.ca3 sets a 10-second crawl delay, which is obeyed |
  | 5th | JSON block (`<script data-cal-json>`) in the Court Calendars page | includes panels and per-courtroom live feeds |
  | 6th | yearly sitting-schedule PDFs (session weeks) + weekly case-list PDFs once posted | shown as "in session" days until the cases post (~2 weeks ahead); 10-second crawl delay obeyed |
  | 7th | the public search form (POST, month view), up to 30 days ahead | panel names appear an hour before argument |
  | 9th | the `global_panel_sittings` array embedded in the calendar page | "Subm." = submitted without argument |
  | 2nd | HuntCal month lists → each "Argument Calendar" event page | **on request only** (see below) |
  | D.C. | the "Future Dates" page (`media.cadc.uscourts.gov/calendar/`) | **on request only** (see below) |

  Every scheduled reader obeys robots.txt (including Crawl-delay), identifies itself with an honest user agent, saves what it read to `_oral_arguments/debug/<court>/`, and reports per-court counts and problems in the Actions job summary. If a court's reader finds nothing on a given day, that court's previous rows are kept.
- **Circuit calendars, original plan (kept for reference).** Each circuit posts its calendar differently (PDFs, HTML tables, daily schedules). Plan: one small parser per circuit in `_oral_arguments/scripts/circuits/`, starting with the ones that publish structured weekly calendars, writing to `courts/oral-arguments/data/circuits_upcoming.json` in the same case schema as SCOTUS. Oyez doesn't cover the circuits, so briefs come from `_oral_arguments/scripts/summarize.py`, fed the district-court opinion or docket (CourtListener RECAP) instead of a Question Presented. Most circuit cases get little press, so expect shorter "why it matters" sections; a filter for cases that do have coverage would help.
- **District courts (phase 3).** A hand-curated `courts/oral-arguments/data/district.json` of notable hearings with public audio.
- **Podcast feed.** An RSS feed of new recordings so arguments show up in a podcast app.
- **Per-case pages** with briefs and argument transcripts once posted.

## Data notes

- Times: the Supreme Court convenes at 10 a.m. ET; second and later cases start when the prior argument ends, so their times are approximate.
- The 22 OT2026 briefs in `_oral_arguments/editorial/` were written by hand on Oct. 29, 2026 from the Questions Presented and the linked coverage.
- Court data are public records. Circuit recordings are hosted by the Free Law Project's CourtListener.
