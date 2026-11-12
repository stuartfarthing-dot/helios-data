# MR-10 Market narrative intelligence

A daily, reproducible record of which market narratives dominate financial news coverage. **This is descriptive only. It produces no trading signals.**

Code: `helios_data/news_ingest.py` (collection) and `helios_data/narratives.py` (classification, measures, report). Workflow: **Update news narratives** (`.github/workflows/update-news.yml`), every 4 hours at :23 UTC. Tests: `tests/test_news.py`.

## Sources

| source_id | Family | What is stored | History | Key / limit | Terms |
|---|---|---|---|---|---|
| `fed_press`, `fed_speeches`, `boe_news`, `boe_speeches`, `ecb_press`, `boj_news`, `rba_media`, `rba_speeches` | official | title, link, RSS description | Recent items only (from the first run) | None | Public-sector content, reused with attribution |
| `gdelt` | gdelt | Page title, link, outlet, GDELT economic themes, tone | From the first run, every 15 minutes | None (static files) | GDELT: open use with citation |
| `guardian` | guardian | Headline, trail text, link (business section) | Backfill from 2015 | `GUARDIAN_KEY`: 5,000 calls/day, 12 calls/second | Developer key, non-commercial |
| `nyt` | nyt | Headline, abstract, link (business, economy, world desks) | Backfill from 2015 (Archive API, one call per month) | `NYT_KEY`: about 5 calls/minute | Non-commercial, with attribution |
| `finnhub` | finnhub | Headline, summary, link | Recent only | `FINNHUB_KEY` (optional): 60 calls/minute | Personal use |
| `ust` | event | US Treasury auction results (stored in `news.events`, not counted as coverage) | From 2015 | None | US public domain |

Full article text is never stored, and nothing behind a paywall is fetched.

GDELT pages are kept only if:
- the outlet is on the whitelist (`GDELT_DOMAINS`), and
- GDELT tagged the page with an economic theme.

A missing GDELT file, a failed feed or a missing key is written to `news.collection_log` with status `error` or `skipped`. These failures are recorded there rather than hidden.

## Tables (schema `news`)

| Table / view | Contents |
|---|---|
| `sources` | Source registry: family, licence, what is stored |
| `items` | One row per article URL. `published_utc` (GDELT: when GDELT first saw it), `collected_utc` (first stored, never changed), headline, url, excerpt, `dup_key`, `is_backfill` |
| `events` | Dated events (Treasury auctions) |
| `collection_log` | Every fetch: window, fetched / new / duplicates, status, error |
| `methods` | Each classification method version with its full rule set (JSON) |
| `narratives` | Narrative list: title, description, countries, assets, `first_detected`, current status |
| `item_class` | Per item and method: relevant?, regions, assets, headline sentiment, intensity, `processed_utc` |
| `item_narratives` | Item-to-narrative links: score, confidence, evidence (matched terms). Several per item allowed |
| `narrative_daily` | **Append-only** daily observations per narrative and method version |
| `narrative_daily_family` | The same, per source family (for checking normalisation) |
| `daily_report` | Each day's markdown report |
| `narrative_daily_latest` (view) | Latest computation of each day |
| `narrative_now` (view) | Latest observation of every narrative, for the Macro Pressure Map |

## Method `rules-1.0`

**Assigning headlines to narratives:**
- Each narrative is defined by two or three word groups. A headline belongs to the narrative only if it matches a term from every group. For example, *Fed easing* = {Fed words} AND {cut / easing words}.
- The rules are in `narratives.py` and are copied to `news.methods`.
- A headline can carry several narratives.

**Confidence of each link:**
- **High:** every group matched in the headline, with 3 or more distinct terms.
- **Medium:** every group matched in the headline.
- **Low:** the summary was needed to match. Low links are stored, but they don't count towards prevalence.

**Counting stories:**
- A **story** is a distinct normalised headline (`dup_key`) within one source family on one UTC day.
- Syndicated copies (" – Reuters", " | CNBC") and repeats therefore count once.

**Relevant coverage (the denominator):** a story that matches a narrative or the broad market / economy vocabulary (`RELEVANT`). Administrative items such as enforcement actions, appointments and banknote news (`NOT_RELEVANT`) are left out unless a narrative matched.

**Official feeds:** central-bank headlines rarely name their own institution ("Bank Rate maintained at 3.75%"). For matching only, the institution's name is put in front of the headline (`SOURCE_CONTEXT`); the stored headline is unchanged.

**Measures for each day *D* and narrative *k*:**

| Measure | Definition |
|---|---|
| prevalence | Mean, over families with at least 5 relevant stories on D, of (narrative stories ÷ relevant stories). Each family counts equally, so a busy source can't dominate |
| prevalence_core | The same using Guardian and NYT only, which have comparable history from 2015 |
| momentum | Mean prevalence over D−4..D ÷ mean prevalence over D−64..D−5, minus 1. Left blank when the baseline has fewer than 20 days or is zero |
| persistence | Consecutive days, ending D, with prevalence ≥ 2% and at least 2 stories |
| sentiment | Mean headline tone from a small financial word list: (positive − negative) ÷ (positive + negative), between −1 and +1. Given with the asset most often named. It describes the language, not a price direction |
| intensity | Share of the narrative's headlines that use strong words (plunge, record, crisis…) |
| confidence | High: 10 or more stories from 2 or more families. Medium: 4 or more stories. Otherwise low |
| status | dormant (not active); emerging (momentum ≥ +50%); fading (momentum ≤ −30%); otherwise active |

## Method `llm-1.0` (Claude Haiku, alongside the rules)

`helios_data/narratives_llm.py` sends headlines to Claude Haiku (`claude-haiku-5-5`) in batches of 40, with thinking off and effort set to low. The narrative list and the instructions are recorded in `news.methods`.

**What the model returns for each item:**
- whether it is relevant market or economic coverage;
- narratives from the fixed list, each with a confidence and an evidence phrase copied from the text;
- the asset class it is mostly about, and the direction its wording implies for that asset (−1, 0 or +1). This is stored as `sentiment`;
- whether the language is dramatic (stored as `intensity`);
- for relevant items that fit no narrative, a suggested theme name. Suggestions go into `news.narrative_suggestions` for review and are never added automatically.

**Safeguards:**
- **Evidence check:** an evidence phrase not found in the item downgrades the link to low, so it doesn't count towards prevalence.
- **Unknown narratives:** ids not on the list are dropped.
- **Gaps:** items the model skipped stay unclassified and are retried on the next run.
- **No partial days:** a day is measured only when 99% of its items are classified.
- **Fixed answers:** answers are stored once and never re-asked.
- **Cost logging:** every call is logged in `news.llm_calls`, with tokens and cost.
- **Spend limits:** `LLM_MONTHLY_USD` (default $10) stops calls for the month, and `LLM_MAX_ITEMS` (default 3,000) caps each run.

The measures, report and views are identical for both methods; filter on `method_version`. Without `ANTHROPIC_API_KEY` this step is skipped.

**Validation in progress:** compare the two methods' links and daily prevalence, and hand-check a sample where they disagree.

## Avoiding look-ahead and keeping history

- **Point in time:** each observation is computed from items with `collected_utc` ≤ its `computed_utc`. Passing an old `computed_utc` as `as_of` reproduces that row exactly (tested).
- **No overwriting:** observations are never updated. A recomputation (`--recompute START END`) or a new method version adds new rows. `narrative_daily_latest` picks the newest; the old rows stay.
- **Backfilled items:** these are flagged `is_backfill`, and each observation reports `backfill_share`. Archive headlines can be edited after publication.
- **Timing:** a day is measured only once it has been over for 6 hours. That lets late GDELT files and the evening NYT archive call arrive first.

## Running

```bash
python -m helios_data.news_ingest                            # normal collection
python -m helios_data.news_ingest --backfill guardian --start 2015-01-01
python -m helios_data.news_ingest --backfill nyt --start 2015-01-01
python -m helios_data.news_ingest --backfill ust --start 2015-01-01
python -m helios_data.narratives                             # rules: classify, measure completed days, print report
python -m helios_data.narratives_llm [--max-items 20000]     # Claude method, the same steps
python -m helios_data.narratives --report 2026-11-13         # print a stored day's report
```

A backfill can be stopped and restarted. Completed windows are skipped, and the Guardian run stops after 4,500 calls a day.

## Limitations

- Keyword rules miss paraphrase and sarcasm, and can misfire on ambiguous words. Low-confidence links and the weekly review of unmatched headlines are the guard against this.
- The narrative list is fixed by hand. New narratives come from reviewing unassigned headlines, not from automatic clustering.
- Coverage mix changes over time: before GDELT and the official feeds start, only Guardian and NYT exist. Use `prevalence_core` for long-run comparisons.
- Headline sentiment is crude and is not a statement about prices.
