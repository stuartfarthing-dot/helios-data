"""Offline tests for MR-10 news collection and narrative measures (no network)."""

import io
import zipfile
from datetime import date, datetime, timedelta

import pandas as pd
import pytest

from helios_data import db, narratives as nar, news_ingest as ni

NOW = datetime(2026, 11, 12, 21, 0)

RSS = b"""<?xml version="1.0" encoding="utf-8"?><rss version="2.0"><channel><title>FRB</title>
<item><title>Federal Reserve issues FOMC statement</title><link>https://www.federalreserve.gov/newsevents/pressreleases/monetary20261028a.htm</link>
<description>The Federal Reserve cut rates by 25 basis points</description><pubDate>Wed, 28 Oct 2026 18:00:00 GMT</pubDate></item>
<item><title>Minutes of the Board's discount rate meetings</title><link>https://www.federalreserve.gov/x.htm?utm_source=rss</link>
<pubDate>Tue, 10 Nov 2026 12:00:00 +0000</pubDate></item>
</channel></rss>"""

ATOM = b"""<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>BoJ keeps rates on hold</title>
<link href="https://www.boj.or.jp/en/a.htm"/><updated>2026-11-12T08:00:00+09:00</updated><summary>s</summary></entry></feed>"""


def gkg_line(domain, url, title, themes="ECON_INFLATION;ECON_INTEREST_RATES", stamp="20261112101500", kind="1"):
    f = [""] * 27
    f[0], f[1], f[2], f[3], f[4] = "rec", stamp, kind, domain, url
    f[7] = themes
    f[15] = "-2.5,1,3"
    f[26] = f"<PAGE_TITLE>{title}</PAGE_TITLE>"
    return "\t".join(f)


@pytest.fixture
def con(tmp_path, monkeypatch):
    monkeypatch.delenv("MOTHERDUCK_TOKEN", raising=False)
    monkeypatch.setenv("HELIOS_DB", str(tmp_path / "helios.duckdb"))
    c = db.connect()
    ni.init(c)
    return c


# ---------------------------------------------------------------- parsing and identity

def test_canonical_url_and_dup_key():
    a = ni.item_id("https://www.Reuters.com/markets/fed-cuts/?utm_source=x#top")
    b = ni.item_id("https://reuters.com/markets/fed-cuts")
    assert a == b
    assert ni.dup_key("Fed cuts rates again - Reuters", "x") == ni.dup_key("Fed cuts rates again | CNBC", "y")
    assert ni.dup_key("Fed cuts rates again", "x") != ni.dup_key("Fed holds rates steady", "x")
    assert ni.dup_key("Hi", "fallback") == "fallback"          # too short to trust


def test_parse_rss_and_atom():
    rows = ni.parse_rss(RSS)
    assert len(rows) == 2 and rows[0]["headline"].startswith("Federal Reserve")
    assert rows[0]["published"].year == 2026
    atom = ni.parse_rss(ATOM)
    assert atom[0]["url"] == "https://www.boj.or.jp/en/a.htm"
    assert ni.to_utc_naive(atom[0]["published"]) == datetime(2026, 11, 11, 23, 0)


def test_parse_gkg_filters_outlets_themes_and_type():
    raw = "\n".join([
        gkg_line("reuters.com", "https://reuters.com/a", "Inflation picks up in October - Reuters"),
        gkg_line("randomblog.net", "https://randomblog.net/a", "Inflation picks up"),            # not whitelisted
        gkg_line("cnbc.com", "https://cnbc.com/b", "Celebrity news", themes="TAX_FNCACT_ACTOR"),  # no econ theme
        gkg_line("cnbc.com", "https://cnbc.com/c", "Fed cut", kind="2"),                          # not a web page
    ]).encode()
    rows = ni.parse_gkg(raw)
    assert [r["url"] for r in rows] == ["https://reuters.com/a"]
    assert rows[0]["tone"] == -2.5 and "ECON_INFLATION" in rows[0]["themes"]


def test_parse_guardian_nyt_finnhub_ust():
    g, pages = ni.parse_guardian({"response": {"pages": 3, "results": [
        {"webTitle": "Bank of England holds rates", "webUrl": "https://www.theguardian.com/business/x",
         "webPublicationDate": "2026-11-06T12:00:00Z", "fields": {"trailText": "<p>MPC votes 5-4</p>"}}]}})
    assert pages == 3 and g[0]["excerpt"] == "<p>MPC votes 5-4</p>"
    n = ni.parse_nyt_archive({"response": {"docs": [
        {"section_name": "Business Day", "headline": {"main": "Yields jump"}, "web_url": "https://nytimes.com/1",
         "pub_date": "2015-01-02T05:00:00+0000", "abstract": "a"},
        {"section_name": "Arts", "headline": {"main": "A play"}, "web_url": "https://nytimes.com/2"}]}})
    assert [r["headline"] for r in n] == ["Yields jump"]
    f = ni.parse_finnhub([{"datetime": 1762900000, "headline": "Gold hits record", "url": "https://x.com/g"}])
    assert f[0]["published"].tzinfo is not None
    e = ni.parse_ust({"data": [{"cusip": "X1", "auction_date": "2026-11-10", "security_term": "10-Year",
                                "security_type": "Note", "high_yield": "4.25", "bid_to_cover_ratio": "2.4"},
                               {"cusip": "X2", "auction_date": "2026-11-20", "bid_to_cover_ratio": "null"}]}, NOW)
    assert list(e.event_id) == ["ust_X1_2026-11-10"]


# ---------------------------------------------------------------- idempotent storage and logging

def test_items_are_stored_once_and_logged(con):
    ctx = ni.Ctx(con, NOW)
    ni.collect_official(ctx, fetch=lambda url: RSS)
    n1 = con.execute("SELECT count(*) FROM news.items").fetchone()[0]
    assert n1 == 2                       # the same two releases appear in every feed fixture: stored once
    ni.collect_official(ni.Ctx(con, NOW + timedelta(hours=4)), fetch=lambda url: RSS)
    assert con.execute("SELECT count(*) FROM news.items").fetchone()[0] == n1
    # collected_utc is never moved forward by a later run
    assert con.execute("SELECT max(collected_utc) FROM news.items").fetchone()[0] == NOW
    log = con.execute("SELECT sum(new_items), sum(duplicates), count(*) FROM news.collection_log").fetchone()
    assert log[0] == 2 and log[2] == 2 * len(ni.RSS_FEEDS)


def test_failures_are_recorded_not_hidden(con):
    def boom(url):
        raise RuntimeError("HTTP 503")
    ni.collect_official(ni.Ctx(con, NOW), fetch=boom)
    rows = con.execute("SELECT DISTINCT status, error FROM news.collection_log").fetchall()
    assert rows == [("error", "HTTP 503")]


def test_missing_keys_are_logged_as_skipped(con, monkeypatch):
    monkeypatch.delenv("GUARDIAN_KEY", raising=False)
    ni.collect_guardian(ni.Ctx(con, NOW), date(2026, 11, 10), date(2026, 11, 12))
    assert con.execute("SELECT status FROM news.collection_log").fetchone()[0] == "skipped"


def test_gdelt_resumes_from_last_file_and_logs_missing(con):
    seen = []

    def fetch(stamp):
        seen.append(stamp)
        if stamp == "20261112100000":
            return None                                           # a missing GDELT file
        return gkg_line("reuters.com", f"https://reuters.com/{stamp}", f"Bond yields jump on deficit fears {stamp}",
                        stamp=stamp).encode()
    ni.collect_gdelt(ni.Ctx(con, datetime(2026, 11, 12, 12, 0)), fetch=fetch)
    first = list(seen)
    assert first[0] == "20261112074500" and first[-1] == "20261112113000"
    seen.clear()
    ni.collect_gdelt(ni.Ctx(con, datetime(2026, 11, 12, 13, 0)), fetch=fetch)
    assert seen == ["20261112114500", "20261112120000", "20261112121500", "20261112123000"]
    assert con.execute("SELECT count(*) FROM news.collection_log WHERE error LIKE 'file missing%'").fetchone()[0] == 1


def test_guardian_backfill_skips_completed_windows(con):
    calls = []

    def fetch(d1, d2, page, key):
        calls.append((d1, page))
        return {"response": {"pages": 1, "results": [
            {"webTitle": f"Gilt yields surge as borrowing rises {d1}", "webUrl": f"https://theguardian.com/{d1}",
             "webPublicationDate": f"{d1}T09:00:00Z"}]}}
    ni.collect_guardian(ni.Ctx(con, NOW), date(2015, 1, 1), date(2015, 3, 31), mode="backfill", fetch=fetch, key="k")
    n = len(calls)
    ni.collect_guardian(ni.Ctx(con, NOW), date(2015, 1, 1), date(2015, 3, 31), mode="backfill", fetch=fetch, key="k")
    assert len(calls) == n                                       # nothing fetched twice
    assert con.execute("SELECT bool_and(is_backfill) FROM news.items").fetchone()[0]


# ---------------------------------------------------------------- classification

@pytest.mark.parametrize("headline,expected", [
    ("Fed signals rate cuts ahead as inflation cools", {"fed_easing", "disinflation"}),
    ("Powell: Fed will keep rates higher for longer", {"fed_restrictive"}),
    ("Gilt yields surge to highest since 1998 on borrowing fears", {"bond_selloff_fiscal"}),
    ("Gold hits record as investors seek safe haven", {"gold_haven_debasement"}),
    ("Microsoft sells $25bn of bonds in jumbo bond deal to fund AI data centres",
     {"corporate_financing", "ai_capex"}),
    ("Bank of England holds interest rates at 4%", {"boe_rate_path"}),
    ("Recipe: autumn soup", set()),
])
def test_classifier_examples(headline, expected):
    _, links = nar.classify(headline, None, nar.Rules())
    assert {l["narrative_id"] for l in links} == expected


def test_low_confidence_when_summary_needed():
    tags, links = nar.classify("Bank of England decision today", "The MPC is expected to cut rates", nar.Rules())
    assert links[0]["confidence"] == "low" and tags["relevant"]


# ---------------------------------------------------------------- measures

def seed(con, day: date, family: str, headlines: list[str], collected=None, backfill=False):
    published = datetime.combine(day, datetime.min.time()) + timedelta(hours=9)
    rows = [{"headline": h, "url": f"https://{family}.example/{day}/{hash(h) % 10**8}", "published": published}
            for h in headlines]
    df = ni.make_items(rows, family, family, collected or published + timedelta(hours=1), backfill)
    ni.write_items(con, df)


FED_CUT = "Fed signals rate cuts ahead"
OTHER = ["Stock market rises on earnings {}", "Oil prices fall {}", "Sterling gains against euro {}",
         "UK retail sales slip {}", "Copper market tight {}", "Bank profits up {}", "Housing market cools {}",
         "Consumer spending steady {}", "Wage growth slows {}"]


def make_history(con, start: date, days: int, fed_from: int):
    """Guardian and NYT each 10 relevant stories a day; the Fed-cut story appears in both from day fed_from."""
    for k in range(days):
        d = start + timedelta(days=k)
        for fam in ("guardian", "nyt"):
            hs = [o.format(f"{fam}{k}") for o in OTHER]
            hs.append(f"{FED_CUT} {fam}{k}" if k >= fed_from else f"Bond market quiet {fam}{k}")
            seed(con, d, fam, hs)


def test_prevalence_momentum_persistence_are_reproducible(con):
    start = date(2026, 8, 1)
    make_history(con, start, 100, fed_from=90)
    nar.run(con, now=datetime(2026, 11, 12, 21, 0))
    d = con.execute("""SELECT obs_date, prevalence, prevalence_core, momentum, persistence, status, n_stories
                       FROM news.narrative_daily WHERE narrative_id = 'fed_easing' ORDER BY obs_date""").fetchdf()
    last = d.iloc[-1]
    assert last.obs_date.date() == start + timedelta(days=99)
    assert last.prevalence == pytest.approx(0.1)                 # 1 of 10 stories in each family
    assert last.prevalence_core == pytest.approx(0.1)
    assert last.persistence == 10 and last.n_stories == 2
    # 5-day mean 0.1 against a 60-day baseline holding five days of 0.1: 0.1 / (0.5 / 60) - 1
    assert last.momentum == pytest.approx(0.1 / (0.5 / 60) - 1) and last.status == "emerging"
    first = d.iloc[90]                                           # first day: baseline all zero -> no ratio invented
    assert pd.isna(first.momentum) and first.status == "active"
    assert d.iloc[50].prevalence == 0 and d.iloc[50].status == "dormant"
    # duplicate copies of a story in the same family do not raise prevalence
    seed(con, start + timedelta(days=99), "guardian", [f"{FED_CUT} guardian99 - Reuters"])
    # recomputing adds rows (history kept) and, at the original as_of, reproduces the numbers exactly
    first_as_of = con.execute("SELECT max(computed_utc) FROM news.narrative_daily").fetchone()[0]
    before = con.execute("SELECT count(*) FROM news.narrative_daily").fetchone()[0]
    nar.compute_days(con, first_as_of + timedelta(seconds=1), [start + timedelta(days=99)])
    after = con.execute("SELECT count(*) FROM news.narrative_daily").fetchone()[0]
    assert after == before + len(nar.NARRATIVES)
    rows = con.execute("""SELECT computed_utc, prevalence FROM news.narrative_daily
                          WHERE narrative_id='fed_easing' AND obs_date=? ORDER BY computed_utc""",
                       [start + timedelta(days=99)]).fetchall()
    assert rows[0][1] == pytest.approx(rows[1][1])                # same-headline copy counted once


def test_momentum_detects_a_rising_narrative(con):
    start = date(2026, 6, 1)
    for k in range(80):
        d = start + timedelta(days=k)
        for fam in ("guardian", "nyt"):
            hs = [o.format(f"{fam}{k}") for o in OTHER]
            # Fed-cut story: 1 in 10 for the first 75 days, 3 in 12 for the last 5
            hs.append(f"{FED_CUT} {fam}{k}")
            if k >= 75:
                hs += [f"Fed set to cut rates again {fam}{k}", f"Federal Reserve easing bets grow {fam}{k}"]
            seed(con, d, fam, hs)
    nar.run(con, now=datetime(2026, 8, 25, 12, 0))
    r = con.execute("""SELECT momentum, status FROM news.narrative_daily WHERE narrative_id='fed_easing'
                       ORDER BY obs_date DESC LIMIT 1""").fetchone()
    assert r[0] == pytest.approx(0.25 / 0.1 - 1)
    assert r[1] == "emerging"


def test_point_in_time_excludes_later_collected_items(con):
    day = date(2026, 11, 10)
    make_history(con, day - timedelta(days=5), 6, fed_from=0)
    as_of = datetime(2026, 11, 12, 12, 0)
    nar.run(con, now=as_of)
    # a late backfill of the same day, collected after as_of, must not change the stored observation
    seed(con, day, "guardian", [f"Fed easing talk {i}" for i in range(20)], collected=as_of + timedelta(days=1), backfill=True)
    nar.classify_new(con, as_of + timedelta(days=1))
    nar.compute_days(con, as_of, [day])
    vals = con.execute("""SELECT DISTINCT prevalence FROM news.narrative_daily
                          WHERE narrative_id='fed_easing' AND obs_date=?""", [day]).fetchall()
    assert len(vals) == 1


def test_report_lists_top_narratives_and_sources(con):
    make_history(con, date(2026, 11, 1), 10, fed_from=0)
    md = nar.run(con, now=datetime(2026, 11, 12, 21, 0))
    assert "Top five active narratives" in md and "Fed easing cycle" in md
    assert "https://guardian.example/" in md
    assert con.execute("SELECT count(*) FROM news.daily_report").fetchone()[0] == 1
    assert con.execute("SELECT status FROM news.narratives WHERE narrative_id='fed_easing'").fetchone()[0] != "new"
    assert con.execute("SELECT count(*) FROM news.narrative_now").fetchone()[0] == len(nar.NARRATIVES)


def test_official_feeds_get_their_institution_as_context():
    r = nar.Rules()
    assert not nar.classify("Bank rate increased to 4% - November 2026 Monetary Policy Summary", None, r)[1]
    _, links = nar.classify("Bank rate increased to 4% - November 2026 Monetary Policy Summary", None, r,
                            nar.SOURCE_CONTEXT["boe_news"])
    assert [l["narrative_id"] for l in links] == ["boe_rate_path"]
    tags, links = nar.classify("Federal Reserve Board announces approval of application by Peoples Bancorp Inc.",
                               None, r, nar.SOURCE_CONTEXT["fed_press"])
    assert not links and not tags["relevant"]              # supervisory notices are not market coverage


# ---------------------------------------------------------------- llm-1.0 (Claude replaced by a fake)

from helios_data import narratives_llm as llm   # noqa: E402


def fake_claude(answer_fn):
    calls = []

    def call(system, user, key, max_tokens=6000):
        calls.append(user)
        items = [line for line in user.splitlines() if line.startswith("[")]
        out = [answer_fn(i + 1, line) for i, line in enumerate(items)]
        return {"content": [{"type": "text", "text": "Here you go:\n" + __import__("json").dumps([o for o in out if o])}],
                "usage": {"input_tokens": 2000, "output_tokens": 800, "cache_read_input_tokens": 1500}}
    call.calls = calls
    return call


def label(n, line):
    if "Fed" in line:
        return {"n": n, "relevant": True, "asset": "rates", "direction": 1, "intense": False, "suggest": None,
                "narratives": [{"id": "fed_easing", "confidence": "high", "evidence": "rate cuts"},
                               {"id": "made_up", "confidence": "high", "evidence": "x"}]}
    if "Oil" in line:
        return {"n": n, "relevant": True, "asset": "oil", "direction": -1, "intense": False,
                "narratives": [], "suggest": "OPEC supply glut"}
    return {"n": n, "relevant": True, "asset": "equities", "direction": 0, "intense": False, "narratives": []}


def test_llm_validation_rules():
    tags, links, sug = llm.validate({"relevant": True, "direction": 5, "asset": "bananas", "narratives": [
        {"id": "gold_haven_debasement", "confidence": "high", "evidence": "Gold soars"},
        {"id": "dollar_weakness", "confidence": "high", "evidence": "dollar collapses"}]},
        "Gold soars to a record", None)
    conf = {l["narrative_id"]: l["confidence"] for l in links}
    assert conf == {"gold_haven_debasement": "high", "dollar_weakness": "low"}   # evidence not in the text
    assert tags["sentiment"] == 0.0 and tags["assets"] is None                  # invalid values neutralised
    assert sug is None


def test_llm_runs_alongside_rules_and_measures(con):
    make_history(con, date(2026, 11, 1), 10, fed_from=0)
    nar.run(con, now=NOW)
    call = fake_claude(label)
    md = llm.run(con, now=NOW, key="k", call=call)
    assert "Fed easing cycle" in md
    n_items = con.execute("SELECT count(*) FROM news.items").fetchone()[0]
    by_mv = dict(con.execute("SELECT method_version, count(*) FROM news.item_class GROUP BY 1").fetchall())
    assert by_mv == {"rules-1.0": n_items, "llm-1.0": n_items}
    assert len(call.calls) == -(-n_items // llm.BATCH)
    assert not con.execute("SELECT count(*) FROM news.item_narratives WHERE narrative_id='made_up'").fetchone()[0]
    assert con.execute("SELECT count(*) FROM news.narrative_suggestions").fetchone()[0] == 20   # one Oil story a day, two families
    calls = con.execute("SELECT count(*), sum(cost_usd) FROM news.llm_calls").fetchone()
    assert calls[1] == pytest.approx(calls[0] * (2000 * 0.10 + 800 * 0.50 + 1500 * 0.01) / 1e6)
    p = con.execute("""SELECT prevalence FROM news.narrative_daily_latest WHERE method_version='llm-1.0'
                       AND narrative_id='fed_easing' ORDER BY obs_date DESC LIMIT 1""").fetchone()[0]
    assert p == pytest.approx(0.1)
    # nothing is asked twice
    llm.run(con, now=NOW + timedelta(hours=4), key="k", call=call)
    assert len(call.calls) == -(-n_items // llm.BATCH)


def test_llm_budget_and_partial_answers(con):
    make_history(con, date(2026, 11, 1), 10, fed_from=0)
    # the model skips every 5th item: those stay unclassified and the days are not measured from a partial set
    skip = fake_claude(lambda n, line: None if n % 5 == 0 else label(n, line))
    llm.run(con, now=NOW, key="k", call=skip)
    left = con.execute("""SELECT count(*) FROM news.items i WHERE NOT EXISTS (SELECT 1 FROM news.item_class c
                          WHERE c.item_id=i.item_id AND c.method_version='llm-1.0')""").fetchone()[0]
    assert left > 0
    assert not con.execute("SELECT count(*) FROM news.narrative_daily WHERE method_version='llm-1.0'").fetchone()[0]
    # budget reached: no further calls this month
    con.execute("UPDATE news.llm_calls SET cost_usd = 50")
    n_before = len(skip.calls)
    stats = llm.classify_new(con, NOW, "k", call=skip)
    assert stats["skipped_budget"] and len(skip.calls) == n_before


def test_llm_skipped_without_key(con, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert llm.run(con, now=NOW) is None
    assert con.execute("SELECT count(*) FROM news.methods WHERE method_version='llm-1.0'").fetchone()[0] == 1
