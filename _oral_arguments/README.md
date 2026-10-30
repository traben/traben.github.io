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
| Courts of Appeals (13) | ⏳ next phase | ✅ last 10 days of recordings, playable in-page (via CourtListener); live-stream links per court | — |
| District courts | ⏳ later | — | — |

## How it works

```
GitHub Action (8 a.m. + 5 p.m. ET)          GitHub Pages (static)
 ├─ _oral_arguments/scripts/build_scotus.py   ──► courts/oral-arguments/data/scotus.json ─┐
 ├─ _oral_arguments/scripts/build_circuits.py ──► courts/oral-arguments/data/circuits_recent.json ─┼─► courts/oral-arguments/index.html + app.js
 └─ commits data/ if changed      courts/oral-arguments/data/courts.json (hand-maintained) ─┘
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

- **Circuit calendars.** `scripts/build_circuit_calendars.py` reads the calendar PDFs of the 1st, 4th, 8th, 10th, 11th and Federal Circuits into `courts/oral-arguments/data/circuits_upcoming.json` (settings per court in `COURTS`; one generic line parser). It obeys robots.txt, saves the extracted PDF text to `_oral_arguments/debug/<court>/` for fixing parsers, and reports per-court counts in the Actions job summary. Still to do: 5th, 9th, 7th, 6th, 3rd (no machine-readable index yet); 2nd and D.C. block automated access via robots.txt.
- **Circuit calendars, original plan (kept for reference).** Each circuit posts its calendar differently (PDFs, HTML tables, daily schedules). Plan: one small parser per circuit in `_oral_arguments/scripts/circuits/`, starting with the ones that publish structured weekly calendars, writing to `courts/oral-arguments/data/circuits_upcoming.json` in the same case schema as SCOTUS. Oyez doesn't cover the circuits, so briefs come from `_oral_arguments/scripts/summarize.py`, fed the district-court opinion or docket (CourtListener RECAP) instead of a Question Presented. Most circuit cases get little press, so expect shorter "why it matters" sections; a filter for cases that do have coverage would help.
- **District courts (phase 3).** A hand-curated `courts/oral-arguments/data/district.json` of notable hearings with public audio.
- **Podcast feed.** An RSS feed of new recordings so arguments show up in a podcast app.
- **Per-case pages** with briefs and argument transcripts once posted.

## Data notes

- Times: the Supreme Court convenes at 10 a.m. ET; second and later cases start when the prior argument ends, so their times are approximate.
- The 22 OT2026 briefs in `_oral_arguments/editorial/` were written by hand on Oct. 29, 2026 from the Questions Presented and the linked coverage.
- Court data are public records. Circuit recordings are hosted by the Free Law Project's CourtListener.
