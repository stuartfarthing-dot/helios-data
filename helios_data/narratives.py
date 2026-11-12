"""MR-10 narratives: classify stored headlines into market narratives and measure them each day.

Method "rules-1.0" (transparent, no paid services):
  * A fixed starting set of narratives (NARRATIVES). Each has two or three word groups; a headline
    (plus its permitted summary) belongs to the narrative only if it matches at least one term from
    EVERY group, e.g. Fed easing = {Fed words} AND {cut / easing words}.
  * Confidence of each link: high = all groups matched in the headline itself and 3+ distinct terms;
    medium = all groups matched in the headline; low = the summary was needed. Low links are kept
    (as evidence of uncertainty) but do not count towards prevalence.
  * Nothing is invented: a headline that matches nothing stays unassigned. New narratives are added
    by hand after review, under a new method version; old observations are never rewritten.

Daily measures (one row per day, narrative and method version in news.narrative_daily; definitions in
docs/news-narratives.md):
  story        distinct normalised headline (dup_key) within one source family on one UTC day, so
               syndicated copies and repeats count once
  prevalence   mean over source families (each with >= MIN_STORIES relevant stories that day) of
               narrative stories / relevant stories; families: official, gdelt, guardian, nyt, finnhub
  prevalence_core  the same using only Guardian + NYT, whose history runs from 2015 (comparable over time)
  momentum     mean prevalence of the last 5 days / mean prevalence of the 60 days before them - 1
  persistence  consecutive days (to this day) with prevalence >= 2% and at least 2 stories
  sentiment    mean headline tone (-1..+1) from a small financial word list, with the asset most often
               named in those headlines; this is the tone of the language, not a price forecast
  intensity    share of the narrative's headlines using strong words (plunge, record, crisis...)
  confidence   high: 10+ stories from 2+ families; medium: 4+ stories; otherwise low

    python -m helios_data.narratives                 # classify new items, measure completed days, print report
    python -m helios_data.narratives --recompute 2026-11-01 2026-11-10   # adds new rows; never overwrites
"""

from __future__ import annotations

import argparse
import json
import logging
import re
from datetime import date, datetime, timedelta, timezone

import pandas as pd

log = logging.getLogger("narratives")

METHOD_VERSION = "rules-1.0"
MIN_STORIES = 5            # a family needs this many relevant stories on a day to count
ACTIVE_PREV = 0.02         # prevalence threshold for "active"
CORE_FAMILIES = ("guardian", "nyt")

# ---------------------------------------------------------------- taxonomy (method rules-1.0)

FED = ["fed", "federal reserve", "fomc", "us central bank", "fed chair", "fed's"]
BOE = ["bank of england", "boe", "monetary policy committee", "threadneedle street"]
RBA = ["rba", "reserve bank of australia", "reserve bank", "australian rates", "aussie rates"]
ECB = ["ecb", "european central bank", "eurozone rates", "lagarde"]
BOJ = ["boj", "bank of japan", "ueda"]
CUT = ["rate cut*", "cut rates", "cuts rates", "cutting rates", "cut interest rates", "cuts interest rates",
       "easing", "dovish", "lower rates", "rate reduction*", "pivot"]
HIKE = ["rate hike*", "raise rates", "raises rates", "raising rates", "hawkish", "higher for longer",
        "tighten*", "rate rise*", "hold rates", "holds rates", "keeps rates", "on hold", "pause*"]
RATES = ["rate*", "interest rate*", "cut*", "hike*", "monetary policy", "inflation"]
INFL = ["inflation", "cpi", "pce", "consumer prices", "price pressures", "price growth", "core prices"]
STOCKS = ["stocks", "shares", "equities", "s&p 500", "s&p", "nasdaq", "dow", "ftse", "wall street",
          "stock market", "dax", "nikkei", "asx", "hang seng", "global markets", "markets"]
BONDS = ["bond*", "yield*", "treasur*", "gilt*", "jgb*", "bund*", "term premium", "debt auction",
         "bond auction", "bond market*"]

NARRATIVES = [
    dict(id="fed_easing", title="Fed easing cycle",
         description="Coverage framing the Federal Reserve as cutting or about to cut rates.",
         countries="US", assets="rates,fx,equities", groups=[FED, CUT]),
    dict(id="fed_restrictive", title="Fed stays restrictive",
         description="Coverage framing the Fed as hawkish, holding or raising rates, 'higher for longer'.",
         countries="US", assets="rates,fx,equities", groups=[FED, HIKE]),
    dict(id="boe_rate_path", title="Bank of England rate path",
         description="Coverage of where UK rates go next: BoE decisions, votes, guidance.",
         countries="UK", assets="rates,gbp,ftse", groups=[BOE, RATES]),
    dict(id="rba_rate_path", title="RBA rate path",
         description="Coverage of where Australian rates go next.",
         countries="AU", assets="rates,aud,asx", groups=[RBA, RATES]),
    dict(id="ecb_rate_path", title="ECB rate path",
         description="Coverage of ECB decisions and euro-area rate expectations.",
         countries="EU", assets="rates,eur,dax", groups=[ECB, RATES]),
    dict(id="japan_yields_yen", title="Japan rates, yields and the yen",
         description="BoJ normalisation, JGB yields, yen weakness or intervention, carry trade.",
         countries="JP", assets="rates,jpy,nikkei",
         groups=[BOJ + ["japan", "japanese", "yen", "jgb*"], ["yield*", "rate*", "hike*", "carry trade",
                                                              "intervention", "intervene", "normalis*",
                                                              "normaliz*", "yen"]]),
    dict(id="sticky_inflation", title="Sticky or re-accelerating inflation",
         description="Inflation described as stubborn, rising, hotter than expected.",
         countries="global", assets="rates,gold",
         groups=[INFL, ["sticky", "stubborn", "hotter", "accelerat*", "re-accelerat*", "rebound*", "persistent",
                        "rises", "rose", "jumps", "jumped", "higher than expected", "above expectations",
                        "beats", "surge*", "soar*", "climb*", "heats up", "picks up", "uptick", "hot"]]),
    dict(id="disinflation", title="Disinflation / inflation cooling",
         description="Inflation described as cooling, easing, falling towards target.",
         countries="global", assets="rates,equities",
         groups=[INFL, ["cool*", "eases", "eased", "slows", "slowed", "falls", "fell", "lower than expected",
                        "below expectations", "disinflation", "retreat*", "moderat*", "softer", "dips",
                        "declines", "back to target", "lowest"]]),
    dict(id="bond_selloff_fiscal", title="Bond sell-off and fiscal worries",
         description="Rising yields linked to deficits, debt supply, weak auctions or term premium.",
         countries="global", assets="rates,gilts,treasuries",
         groups=[BONDS, ["surge*", "jump*", "soar*", "spike*", "sell-off", "selloff", "rout", "slump*",
                         "weak demand", "tail*", "deficit*", "fiscal", "debt pile", "borrowing", "term premium",
                         "vigilante*", "highest since", "climb*", "rise", "rises", "rising"]]),
    dict(id="corporate_financing", title="Corporate financing demand",
         description="Heavy corporate bond issuance, IPOs, share sales and large capex financing.",
         countries="global", assets="credit,rates,equities",
         groups=[["bond sale*", "bond issuance", "debt sale*", "debt offering*", "jumbo bond*", "bond deal*",
                  "ipo", "ipos", "initial public offering*", "listing*", "share sale*", "share placement*",
                  "rights issue*", "capital rais*", "raise capital", "raises capital", "convertible*",
                  "borrowing binge", "record issuance", "issuance", "debt financing", "financing package*",
                  "private credit", "leveraged loan*", "refinanc*", "capex"]]),
    dict(id="ai_capex", title="AI investment boom (or bubble)",
         description="Spending on AI, data centres and chips, and debate about its returns or a bubble.",
         countries="US,global", assets="equities,credit,copper",
         groups=[["ai", "artificial intelligence", "data cent*", "nvidia", "hyperscaler*", "gpu*", "chipmaker*",
                  "openai"], ["capex", "spending", "spend", "invest*", "bubble", "boom", "valuation*", "billion",
                              "trillion", "build-out", "buildout", "capital expenditure", "fund", "funding", "financ*",
                              "bn", "tn"]]),
    dict(id="gold_haven_debasement", title="Gold as safe haven / debasement hedge",
         description="Gold and silver framed as havens, records, central-bank buying or currency debasement.",
         countries="global", assets="gold,silver,usd",
         groups=[["gold", "bullion", "precious metal*", "silver"],
                 ["record*", "safe haven", "safe-haven", "haven", "debasement", "central bank buying",
                  "central banks buy*", "hedge", "all-time high", "surge*", "soar*", "rally", "rallies",
                  "rallied", "highest", "demand"]]),
    dict(id="dollar_weakness", title="Dollar weakness / de-dollarisation",
         description="US dollar described as weakening, losing reserve status or being diversified away from.",
         countries="US,global", assets="usd,gold,em",
         groups=[["dollar", "greenback", "dxy", "usd"],
                 ["weak*", "slump*", "slide*", "falls", "fell", "drop*", "lowest", "de-dollar*", "dedollar*",
                  "debasement", "sell-off", "selloff", "retreat*", "tumble*", "sink*"]]),
    dict(id="dollar_strength", title="Dollar strength",
         description="US dollar described as strengthening, a haven, or at multi-month highs.",
         countries="US,global", assets="usd,gold,em",
         groups=[["dollar", "greenback", "dxy", "usd"],
                 ["strengthen*", "strong", "stronger", "rall*", "surge*", "jump*", "gains", "climbs",
                  "highest", "firm*", "haven"]]),
    dict(id="china_slowdown_stimulus", title="China slowdown and stimulus",
         description="China's growth, property, deflation and policy support.",
         countries="CN", assets="equities,copper,aud,commodities",
         groups=[["china", "chinese", "beijing", "pboc", "yuan"],
                 ["slowdown", "slow*", "deflation", "stimulus", "property", "real estate", "weak demand",
                  "contraction", "contract*", "growth", "gdp", "support measures", "easing", "exports",
                  "factory", "pmi"]]),
    dict(id="europe_growth", title="European growth and policy",
         description="Euro-area and German growth, recession risk, fiscal plans.",
         countries="EU,DE", assets="dax,eur,bunds",
         groups=[["eurozone", "euro zone", "euro area", "europe's economy", "european economy", "germany",
                  "german economy", "france", "french"],
                 ["growth", "gdp", "recession", "slowdown", "stagnat*", "contraction", "fiscal", "budget",
                  "stimulus", "pmi", "output", "economy"]]),
    dict(id="trade_tariffs", title="Tariffs and trade conflict",
         description="Tariffs, trade wars, export controls and their economic effects.",
         countries="US,CN,global", assets="equities,fx,commodities",
         groups=[["tariff*", "trade war*", "trade deal*", "trade talks", "export control*", "trade tension*",
                  "levies", "duties", "trade dispute*"]]),
    dict(id="recession_fears", title="Growth scare / recession fears",
         description="Coverage of recession risk, layoffs, weak jobs data, hard landing.",
         countries="global", assets="equities,rates",
         groups=[["recession*", "hard landing", "downturn", "layoff*", "job cuts", "jobless", "unemployment",
                  "slowdown", "contraction", "stagflation", "economy shrinks", "economy shrank"]]),
    dict(id="liquidity_conditions", title="Liquidity and funding conditions",
         description="Market liquidity, repo and funding stress, QT, bank reserves, credit spreads.",
         countries="global", assets="rates,credit,equities",
         groups=[["liquidity", "repo", "funding stress", "funding market*", "quantitative tightening",
                  "balance sheet", "bank reserves", "financial conditions", "credit spread*", "money market*",
                  "standing repo", "qt", "quantitative easing", "qe"]]),
    dict(id="equity_risk_on", title="Equity rally / risk appetite",
         description="Stocks described as rallying, at records, risk-on.",
         countries="global", assets="equities",
         groups=[STOCKS, ["record high*", "all-time high*", "rally", "rallies", "rallied", "surge*", "soar*",
                          "risk-on", "risk appetite", "bull market", "gains", "climb*", "jump*", "best day",
                          "best week"]]),
    dict(id="equity_risk_off", title="Market stress / risk-off",
         description="Stocks or markets described as selling off, volatile, fearful.",
         countries="global", assets="equities,vix,gold",
         groups=[STOCKS, ["sell-off", "selloff", "slump*", "plunge*", "rout", "tumble*", "tank*", "volatil*",
                          "fear*", "risk-off", "crash*", "correction", "bear market", "worst day", "worst week",
                          "wipe*", "turmoil", "panic"]]),
    dict(id="geopolitical_risk", title="Geopolitical risk",
         description="War, conflict, sanctions and security shocks in market coverage.",
         countries="global", assets="oil,gold,equities",
         groups=[["war", "conflict", "sanction*", "missile*", "invasion", "ceasefire", "military", "attack*",
                  "geopolitic*", "strikes on", "airstrike*", "troops", "nuclear"]]),
]

# Broad vocabulary that makes a headline "relevant" market / economic coverage (the denominator).
RELEVANT = ["econom*", "market*", "stock*", "shares", "bond*", "yield*", "rate*", "inflation", "cpi", "gdp",
            "jobs", "unemployment", "payroll*", "central bank*", "fed", "federal reserve", "ecb", "boe",
            "bank of england", "rba", "boj", "treasur*", "gilt*", "dollar", "sterling", "pound", "euro", "yen",
            "yuan", "currenc*", "gold", "silver", "oil", "brent", "copper", "commodit*", "recession", "growth",
            "tariff*", "trade", "budget", "deficit", "debt", "borrowing", "ipo", "earnings", "profit*",
            "investor*", "wall street", "ftse", "s&p", "nasdaq", "dow", "nikkei", "dax", "asx", "hang seng",
            "imf", "opec", "fiscal", "monetary", "liquidity", "credit", "price*", "wage*", "pmi",
            "consumer", "retail sales", "housing", "mortgage*", "spending"]

REGIONS = {
    "US": ["us", "u.s.", "america*", "fed", "federal reserve", "wall street", "treasur*", "washington", "s&p",
           "nasdaq", "dow"],
    "UK": ["uk", "britain", "british", "bank of england", "boe", "sterling", "pound", "gilt*", "ftse",
           "chancellor", "london"],
    "AU": ["australia*", "rba", "aussie", "asx"],
    "EU": ["eurozone", "euro zone", "europe*", "ecb", "germany", "german", "france", "french", "italy", "dax",
           "bund*", "euro"],
    "CN": ["china", "chinese", "beijing", "pboc", "yuan", "hong kong", "hang seng"],
    "JP": ["japan*", "boj", "bank of japan", "yen", "nikkei", "jgb*", "tokyo"],
}
ASSETS = {
    "rates": ["rate*", "yield*", "bond*", "treasur*", "gilt*", "bund*", "jgb*"],
    "equities": ["stock*", "shares", "equit*", "s&p", "nasdaq", "dow", "ftse", "dax", "nikkei", "asx",
                 "hang seng", "wall street"],
    "usd": ["dollar", "greenback", "dxy"],
    "fx": ["sterling", "pound", "euro", "yen", "yuan", "currenc*", "aussie dollar"],
    "gold": ["gold", "bullion", "silver", "precious metal*"],
    "oil": ["oil", "brent", "crude", "opec"],
    "credit": ["credit", "corporate bond*", "high yield", "junk bond*", "private credit", "loan*", "issuance"],
    "copper": ["copper", "metals"],
}
POSITIVE = ["rally", "rallies", "rallied", "surge*", "soar*", "gain", "gains", "jump*", "rebound*", "recover*",
            "boost*", "strong", "stronger", "beat", "beats", "optimis*", "upbeat", "record high", "boom*",
            "improv*", "robust", "resilien*", "upgrade*", "bullish", "confidence", "best"]
NEGATIVE = ["fall", "falls", "fell", "drop*", "slump*", "plunge*", "tumble*", "sell-off", "selloff", "rout",
            "crash*", "fear*", "worr*", "concern*", "crisis", "recession", "weak", "weaker", "miss", "misses",
            "slowdown", "downturn", "loss", "losses", "warn*", "turmoil", "stress*", "default*", "layoff*",
            "downgrade*", "bearish", "slide*", "sink*", "worst", "threat*", "risk*", "pressure*", "struggl*"]
INTENSE = ["soar*", "plunge*", "crash*", "rout", "turmoil", "panic*", "crisis", "record", "historic", "biggest",
           "worst", "emergency", "surge*", "collapse*", "meltdown", "shock*", "skyrocket*", "frenzy", "tumble*",
           "slump*", "spike*", "alarm*", "bloodbath", "carnage", "massive", "unprecedented"]


# Administrative / supervisory items (mostly from official feeds) that are not market coverage, unless a
# narrative matched anyway.
NOT_RELEVANT = ["enforcement action*", "approval of application*", "approval of two applications", "banknote*",
                "court of directors", "appointment*", "appoints", "fines", "fined", "consults on", "consultation",
                "office space", "vacanc*", "obituar*", "recipe*", "cv and questionnaire", "industry forum",
                "sub-committee", "sub committee", "taskforce", "statistical notice", "green notice", "regulation o",
                "resolution plan*", "stablecoin issuer*", "senior manager"]

# Official feeds rarely name their own institution ("Bank rate increased to 4%"), so for matching only the
# institution is put in front of the headline. The stored headline is unchanged.
SOURCE_CONTEXT = {"fed_press": "Federal Reserve", "fed_speeches": "Federal Reserve", "boe_news": "Bank of England",
                  "boe_speeches": "Bank of England", "ecb_press": "ECB", "boj_news": "Bank of Japan",
                  "rba_media": "Reserve Bank of Australia", "rba_speeches": "Reserve Bank of Australia"}


def _rx(terms) -> re.Pattern:
    parts = []
    for t in terms:
        if t.endswith("*"):
            parts.append(r"(?<![a-z0-9])" + re.escape(t[:-1]) + r"[a-z0-9'-]*")
        else:
            parts.append(r"(?<![a-z0-9])" + re.escape(t) + r"(?![a-z0-9])")
    return re.compile("|".join(parts), re.I)


class Rules:
    def __init__(self):
        self.narr = [(n, [_rx(g) for g in n["groups"]]) for n in NARRATIVES]
        self.relevant = _rx(RELEVANT)
        self.not_relevant = _rx(NOT_RELEVANT)
        self.regions = {k: _rx(v) for k, v in REGIONS.items()}
        self.assets = {k: _rx(v) for k, v in ASSETS.items()}
        self.pos, self.neg, self.intense = _rx(POSITIVE), _rx(NEGATIVE), _rx(INTENSE)


def rules_json() -> str:
    return json.dumps({"source_context": SOURCE_CONTEXT, "narratives": NARRATIVES, "relevant": RELEVANT, "not_relevant": NOT_RELEVANT, "regions": REGIONS, "assets": ASSETS,
                       "positive": POSITIVE, "negative": NEGATIVE, "intense": INTENSE,
                       "min_stories": MIN_STORIES, "active_prevalence": ACTIVE_PREV, "core": CORE_FAMILIES})


def classify(headline: str, excerpt: str | None, rules: Rules, context: str | None = None) -> tuple[dict, list[dict]]:
    """One item -> (item tags, narrative links). Pure function, so results are reproducible."""
    h = f"{context}: {headline or ''}" if context else (headline or "")
    full = f"{h} {excerpt or ''}"
    links = []
    for n, groups in rules.narr:
        in_head = [sorted({m.group(0).lower() for m in g.finditer(h)}) for g in groups]
        if all(in_head):
            terms = sorted({t for g in in_head for t in g})
            conf = "high" if len(terms) >= 3 else "medium"
        else:
            in_full = [sorted({m.group(0).lower() for m in g.finditer(full)}) for g in groups]
            if not all(in_full):
                continue
            terms = sorted({t for g in in_full for t in g})
            conf = "low"
        links.append({"narrative_id": n["id"], "score": len(terms), "confidence": conf,
                      "evidence": "; ".join(terms)[:300]})
    pos = len(rules.pos.findall(h))
    neg = len(rules.neg.findall(h))
    tags = {
        "relevant": bool(links) or (bool(rules.relevant.search(f"{headline or ''} {excerpt or ''}"))
                                    and not rules.not_relevant.search(headline or "")),
        "regions": ",".join(k for k, rx in rules.regions.items() if rx.search(full)) or None,
        "assets": ",".join(k for k, rx in rules.assets.items() if rx.search(full)) or None,
        "sentiment": (pos - neg) / (pos + neg) if pos + neg else 0.0,
        "intensity": 1.0 if rules.intense.search(h) else 0.0,
    }
    return tags, links


# ---------------------------------------------------------------- schema

SCHEMA = """
CREATE SCHEMA IF NOT EXISTS news;
CREATE TABLE IF NOT EXISTS news.methods (method_version VARCHAR, created_utc TIMESTAMP, rules JSON, notes VARCHAR);
CREATE TABLE IF NOT EXISTS news.narratives (
  narrative_id VARCHAR, title VARCHAR, description VARCHAR, countries VARCHAR, asset_classes VARCHAR,
  method_version VARCHAR, first_detected DATE, status VARCHAR, status_date DATE, updated_utc TIMESTAMP
);
CREATE TABLE IF NOT EXISTS news.item_class (
  item_id VARCHAR, method_version VARCHAR, processed_utc TIMESTAMP, relevant BOOLEAN,
  regions VARCHAR, assets VARCHAR, sentiment DOUBLE, intensity DOUBLE
);
CREATE TABLE IF NOT EXISTS news.item_narratives (
  item_id VARCHAR, narrative_id VARCHAR, method_version VARCHAR, score INTEGER, confidence VARCHAR,
  evidence VARCHAR, processed_utc TIMESTAMP
);
CREATE TABLE IF NOT EXISTS news.narrative_daily (
  obs_date DATE, narrative_id VARCHAR, method_version VARCHAR, computed_utc TIMESTAMP,
  n_stories BIGINT, n_low_conf BIGINT, n_families INTEGER, families_used VARCHAR, relevant_stories BIGINT,
  prevalence DOUBLE, prevalence_core DOUBLE, baseline DOUBLE, momentum DOUBLE, momentum_core DOUBLE,
  persistence INTEGER, sentiment DOUBLE, sentiment_asset VARCHAR, intensity DOUBLE,
  confidence VARCHAR, status VARCHAR, backfill_share DOUBLE
);
CREATE TABLE IF NOT EXISTS news.narrative_daily_family (
  obs_date DATE, narrative_id VARCHAR, family VARCHAR, method_version VARCHAR, computed_utc TIMESTAMP,
  narrative_stories BIGINT, relevant_stories BIGINT, share DOUBLE
);
CREATE TABLE IF NOT EXISTS news.daily_report (
  report_date DATE, method_version VARCHAR, generated_utc TIMESTAMP, markdown VARCHAR
);

-- Latest computation of each day (earlier computations stay in narrative_daily as history)
CREATE OR REPLACE VIEW news.narrative_daily_latest AS
SELECT * FROM news.narrative_daily
QUALIFY row_number() OVER (PARTITION BY obs_date, narrative_id, method_version ORDER BY computed_utc DESC) = 1;

-- For the Macro Pressure Map: each narrative's latest observation
CREATE OR REPLACE VIEW news.narrative_now AS
SELECT d.obs_date, n.narrative_id, n.title, n.description, n.countries, n.asset_classes, n.first_detected,
       d.status, d.prevalence, d.prevalence_core, d.momentum, d.persistence, d.sentiment, d.sentiment_asset,
       d.intensity, d.n_stories, d.n_families, d.confidence, d.method_version, d.computed_utc
FROM news.narrative_daily_latest d
JOIN news.narratives n ON n.narrative_id = d.narrative_id AND n.method_version = d.method_version
WHERE d.obs_date = (SELECT max(obs_date) FROM news.narrative_daily WHERE method_version = d.method_version);
"""


def register_method(con, now: datetime, mv: str, definition: str, notes: str) -> None:
    """Record a method version (once) and give it its own copy of the narrative list."""
    if not con.execute("SELECT count(*) FROM news.methods WHERE method_version = ?", [mv]).fetchone()[0]:
        con.execute("INSERT INTO news.methods VALUES (?,?,?,?)", [mv, now, definition, notes])
    for n in NARRATIVES:
        exists = con.execute("SELECT count(*) FROM news.narratives WHERE narrative_id=? AND method_version=?",
                             [n["id"], mv]).fetchone()[0]
        if not exists:
            con.execute("INSERT INTO news.narratives VALUES (?,?,?,?,?,?,NULL,'new',NULL,?)",
                        [n["id"], n["title"], n["description"], n["countries"], n["assets"], mv, now])


def init(con, now: datetime) -> None:
    con.execute(SCHEMA)
    register_method(con, now, METHOD_VERSION, rules_json(), "rule-based phrase groups; see narratives.py docstring")


# ---------------------------------------------------------------- classification

def classify_new(con, now: datetime, batch: int = 50000) -> int:
    """Classify every stored item that has no classification under this method version."""
    rules = Rules()
    total = 0
    while True:
        df = con.execute("""SELECT i.item_id, i.source_id, i.headline, i.excerpt FROM news.items i
                            WHERE NOT EXISTS (SELECT 1 FROM news.item_class c
                                              WHERE c.item_id = i.item_id AND c.method_version = ?)
                            LIMIT ?""", [METHOD_VERSION, batch]).fetchdf()
        if df.empty:
            return total
        tags, links = [], []
        for r in df.itertuples(index=False):
            t, ls = classify(r.headline if isinstance(r.headline, str) else "",
                             r.excerpt if isinstance(r.excerpt, str) else None, rules, SOURCE_CONTEXT.get(r.source_id))
            tags.append({"item_id": r.item_id, "method_version": METHOD_VERSION, "processed_utc": now, **t})
            links += [{"item_id": r.item_id, **l, "method_version": METHOD_VERSION, "processed_utc": now} for l in ls]
        tdf = pd.DataFrame(tags)
        con.register("tdf", tdf)
        con.execute("""INSERT INTO news.item_class SELECT item_id, method_version, processed_utc, relevant, regions,
                       assets, sentiment, intensity FROM tdf""")
        con.unregister("tdf")
        if links:
            ldf = pd.DataFrame(links)
            con.register("ldf", ldf)
            con.execute("""INSERT INTO news.item_narratives SELECT item_id, narrative_id, method_version, score,
                           confidence, evidence, processed_utc FROM ldf""")
            con.unregister("ldf")
        total += len(df)
        log.info("classified %d items", total)


# ---------------------------------------------------------------- daily measures

MEASURES_SQL = """
WITH it AS (   -- items known at as_of, with their classification
  SELECT i.item_id, i.family, i.dup_key, CAST(i.published_utc AS DATE) AS d, i.is_backfill,
         c.relevant, c.sentiment, c.intensity, c.assets
  FROM news.items i JOIN news.item_class c ON c.item_id = i.item_id AND c.method_version = $mv
  WHERE i.collected_utc <= $as_of AND c.processed_utc <= $as_of AND i.published_utc IS NOT NULL
    AND i.family <> 'event'
),
rel AS (SELECT d, family, count(DISTINCT dup_key) AS relevant_stories FROM it WHERE relevant GROUP BY ALL),
fam_ok AS (SELECT * FROM rel WHERE relevant_stories >= $min_stories),
lk AS (
  SELECT it.*, l.narrative_id, l.confidence FROM it
  JOIN news.item_narratives l ON l.item_id = it.item_id AND l.method_version = $mv AND l.processed_utc <= $as_of
),
fam AS (   -- per day, narrative, family: distinct stories (high/medium links only)
  SELECT f.d, n.narrative_id, f.family, f.relevant_stories,
         count(DISTINCT lk.dup_key) FILTER (WHERE lk.confidence IN ('high', 'medium')) AS narrative_stories
  FROM fam_ok f CROSS JOIN (SELECT DISTINCT narrative_id FROM news.narratives WHERE method_version = $mv) n
  LEFT JOIN lk ON lk.d = f.d AND lk.family = f.family AND lk.narrative_id = n.narrative_id
  GROUP BY ALL
),
day AS (
  SELECT d, narrative_id,
         avg(narrative_stories / relevant_stories) AS prevalence,
         avg(narrative_stories / relevant_stories) FILTER (WHERE family IN $core) AS prevalence_core,
         sum(narrative_stories) AS n_stories,
         count(*) FILTER (WHERE narrative_stories > 0) AS n_families,
         string_agg(family, ',' ORDER BY family) AS families_used,
         sum(relevant_stories) AS relevant_stories
  FROM fam GROUP BY ALL
),
extra AS (
  SELECT d, narrative_id,
         count(DISTINCT dup_key) FILTER (WHERE confidence = 'low') AS n_low_conf,
         avg(sentiment) FILTER (WHERE confidence <> 'low') AS sentiment,
         avg(intensity) FILTER (WHERE confidence <> 'low') AS intensity,
         avg(CASE WHEN is_backfill THEN 1 ELSE 0 END) AS backfill_share
  FROM lk GROUP BY ALL
),
asset AS (   -- the asset most often named in the narrative's headlines that day
  SELECT d, narrative_id, a AS sentiment_asset FROM (
    SELECT d, narrative_id, unnest(string_split(assets, ',')) AS a FROM lk
    WHERE confidence <> 'low' AND assets IS NOT NULL)
  GROUP BY d, narrative_id, a
  QUALIFY row_number() OVER (PARTITION BY d, narrative_id ORDER BY count(*) DESC, a) = 1
),
w AS (
  SELECT day.*,
    avg(prevalence) OVER (PARTITION BY narrative_id ORDER BY d RANGE BETWEEN INTERVAL 4 DAY PRECEDING AND CURRENT ROW) AS recent,
    avg(prevalence) OVER (PARTITION BY narrative_id ORDER BY d RANGE BETWEEN INTERVAL 64 DAY PRECEDING AND INTERVAL 5 DAY PRECEDING) AS baseline,
    count(prevalence) OVER (PARTITION BY narrative_id ORDER BY d RANGE BETWEEN INTERVAL 64 DAY PRECEDING AND INTERVAL 5 DAY PRECEDING) AS baseline_days,
    avg(prevalence_core) OVER (PARTITION BY narrative_id ORDER BY d RANGE BETWEEN INTERVAL 4 DAY PRECEDING AND CURRENT ROW) AS recent_core,
    avg(prevalence_core) OVER (PARTITION BY narrative_id ORDER BY d RANGE BETWEEN INTERVAL 64 DAY PRECEDING AND INTERVAL 5 DAY PRECEDING) AS baseline_core,
    (prevalence >= $active AND n_stories >= 2) AS active
  FROM day
),
runs AS (   -- persistence: length of the current run of active days (calendar days with data)
  SELECT *, sum(CASE WHEN active THEN 0 ELSE 1 END) OVER (PARTITION BY narrative_id ORDER BY d) AS grp FROM w
),
p AS (
  SELECT *, CASE WHEN active THEN count(*) FILTER (WHERE active) OVER (PARTITION BY narrative_id, grp ORDER BY d) ELSE 0 END AS persistence
  FROM runs
)
SELECT p.d AS obs_date, p.narrative_id, $mv AS method_version, $as_of AS computed_utc,
       p.n_stories, coalesce(e.n_low_conf, 0) AS n_low_conf, p.n_families, p.families_used, p.relevant_stories,
       p.prevalence, p.prevalence_core, p.baseline,
       CASE WHEN p.baseline_days >= 20 AND p.baseline > 0 THEN p.recent / p.baseline - 1 END AS momentum,
       CASE WHEN p.baseline_core > 0 THEN p.recent_core / p.baseline_core - 1 END AS momentum_core,
       p.persistence, e.sentiment, s.sentiment_asset, e.intensity,
       CASE WHEN p.n_stories >= 10 AND p.n_families >= 2 THEN 'high' WHEN p.n_stories >= 4 THEN 'medium' ELSE 'low' END AS confidence,
       CASE WHEN NOT p.active THEN 'dormant'
            WHEN p.baseline_days >= 20 AND p.baseline > 0 AND p.recent / p.baseline - 1 >= 0.5 THEN 'emerging'
            WHEN p.baseline_days >= 20 AND p.baseline > 0 AND p.recent / p.baseline - 1 <= -0.3 THEN 'fading'
            ELSE 'active' END AS status,
       e.backfill_share
FROM p LEFT JOIN extra e ON e.d = p.d AND e.narrative_id = p.narrative_id
     LEFT JOIN asset s ON s.d = p.d AND s.narrative_id = p.narrative_id
"""

FAMILY_SQL = """
SELECT d AS obs_date, narrative_id, family, $mv AS method_version, $as_of AS computed_utc,
       narrative_stories, relevant_stories, narrative_stories / relevant_stories AS share
FROM fam
"""


def _params(as_of: datetime, mv: str = METHOD_VERSION) -> dict:
    return {"mv": mv, "as_of": as_of, "min_stories": MIN_STORIES, "active": ACTIVE_PREV,
            "core": list(CORE_FAMILIES)}


def _sql(body: str, core_list: str) -> str:
    return body.replace("IN $core", f"IN {core_list}")


def compute_days(con, as_of: datetime, days: list[date], mv: str = METHOD_VERSION) -> int:
    """Measure the given days from everything known at as_of; append rows (never update)."""
    if not days:
        return 0
    core = "(" + ",".join(f"'{f}'" for f in CORE_FAMILIES) + ")"
    params = {k: v for k, v in _params(as_of, mv).items() if k != "core"}
    con.register("target_days", pd.DataFrame({"d": pd.to_datetime(days).date}))
    main_sql = _sql(MEASURES_SQL, core)
    con.execute(f"""INSERT INTO news.narrative_daily
                    SELECT m.* FROM ({main_sql}) m WHERE m.obs_date IN (SELECT d FROM target_days)""", params)
    fam_sql = main_sql.split("\nday AS (")[0].rstrip().rstrip(",") + "\n" + FAMILY_SQL     # reuse the CTEs up to `fam`
    con.execute(f"""INSERT INTO news.narrative_daily_family
                    SELECT f.* FROM ({fam_sql}) f WHERE f.obs_date IN (SELECT d FROM target_days)""",
                {k: v for k, v in params.items() if k != "active"})
    n = con.execute("SELECT count(*) FROM news.narrative_daily WHERE computed_utc = ? AND method_version = ?",
                    [as_of, mv]).fetchone()[0]
    con.unregister("target_days")
    return n


def pending_days(con, now: datetime, mv: str = METHOD_VERSION, min_classified: float = 0.99) -> list[date]:
    """Completed UTC days (ended at least 6 hours ago) that need a (new) measurement for this method:
    never measured, or new items for that day were collected after its last measurement (late items,
    a backfill). The earlier measurement is kept; a new row is appended. A day is only measured once at
    least 99% of its items are classified by the method, so a backlog never gives a partial measurement."""
    last_complete = (now - timedelta(hours=30)).date()
    rows = con.execute("""
        WITH d AS (
          SELECT CAST(i.published_utc AS DATE) AS d, count(*) AS n, count(c.item_id) AS k,
                 max(i.collected_utc) AS last_collected
          FROM news.items i LEFT JOIN news.item_class c ON c.item_id = i.item_id AND c.method_version = ?
          WHERE i.published_utc IS NOT NULL AND i.family <> 'event' GROUP BY 1),
        m AS (SELECT obs_date, max(computed_utc) AS last_computed FROM news.narrative_daily
              WHERE method_version = ? GROUP BY 1)
        SELECT d.d FROM d LEFT JOIN m ON m.obs_date = d.d
        WHERE d.d <= ? AND d.k >= ? * d.n AND (m.obs_date IS NULL OR d.last_collected > m.last_computed)
        ORDER BY d.d""", [mv, mv, last_complete, min_classified]).fetchall()
    return [r[0] for r in rows]


def update_narratives(con, now: datetime, mv: str = METHOD_VERSION) -> None:
    """Refresh first-detected date and current status (history stays in narrative_daily)."""
    con.execute("""
        UPDATE news.narratives n SET
          first_detected = s.first_active, status = s.status, status_date = s.obs_date, updated_utc = $now
        FROM (
          SELECT l.narrative_id, l.status, l.obs_date, f.first_active
          FROM (SELECT * FROM news.narrative_daily_latest WHERE method_version = $mv
                QUALIFY row_number() OVER (PARTITION BY narrative_id ORDER BY obs_date DESC) = 1) l
          LEFT JOIN (SELECT narrative_id, min(obs_date) AS first_active FROM news.narrative_daily_latest
                     WHERE method_version = $mv AND persistence > 0 GROUP BY 1) f USING (narrative_id)
        ) s
        WHERE n.narrative_id = s.narrative_id AND n.method_version = $mv""", {"now": now, "mv": mv})


# ---------------------------------------------------------------- daily report

def _pct(x):
    return "–" if x is None or pd.isna(x) else f"{100 * x:.1f}%"


def _mom(x):
    return "no baseline yet" if x is None or pd.isna(x) else f"{100 * x:+.0f}%"


def _sent(s, a):
    if s is None or pd.isna(s):
        return "–"
    word = "positive" if s > 0.15 else "negative" if s < -0.15 else "mixed"
    return f"{word} ({s:+.2f}){' on ' + a if a else ''}"


def build_report(con, report_date: date | None = None, mv: str = METHOD_VERSION) -> str:
    if report_date is None:
        report_date = con.execute("SELECT max(obs_date) FROM news.narrative_daily WHERE method_version=?", [mv]).fetchone()[0]
    if report_date is None:
        return "No narrative observations yet."
    cur = con.execute("""SELECT d.*, n.title, n.description, n.first_detected FROM news.narrative_daily_latest d
                         JOIN news.narratives n USING (narrative_id, method_version)
                         WHERE d.obs_date = ? AND d.method_version = ? ORDER BY d.prevalence DESC""",
                      [report_date, mv]).fetchdf()
    prev_date = con.execute("SELECT max(obs_date) FROM news.narrative_daily WHERE method_version=? AND obs_date < ?",
                            [mv, report_date]).fetchone()[0]
    prev = con.execute("""SELECT narrative_id, prevalence, status FROM news.narrative_daily_latest
                          WHERE obs_date = ? AND method_version = ?""", [prev_date, mv]).fetchdf() if prev_date else pd.DataFrame()
    cov = con.execute("""SELECT family, count(DISTINCT dup_key) AS stories FROM news.items i
                         JOIN news.item_class c ON c.item_id = i.item_id AND c.method_version = ?
                         WHERE CAST(published_utc AS DATE) = ? AND c.relevant GROUP BY 1 ORDER BY 1""",
                      [mv, report_date]).fetchall()
    errs = con.execute("""SELECT source_id, count(*) FROM news.collection_log WHERE status = 'error'
                          AND CAST(coalesce(window_to, run_utc) AS DATE) = ? GROUP BY 1 ORDER BY 1""",
                       [report_date]).fetchall()

    out = [f"# Market narratives – {report_date:%a %d %b %Y}",
           f"_Method {mv}. Coverage (relevant stories): " + (", ".join(f"{f} {n}" for f, n in cov) or "none") +
           (". Collection errors: " + ", ".join(f"{s} {n}" for s, n in errs) if errs else ". No collection errors") +
           ". Descriptive only – not a trading signal._", ""]
    active = cur[cur.status != "dormant"]
    out.append("## Top five active narratives")
    if active.empty:
        out.append("None active.")
    for i, r in enumerate(active.head(5).itertuples(), 1):
        out.append(f"**{i}. {r.title}** – {r.description}")
        out.append(f"Prevalence {_pct(r.prevalence)} ({r.n_stories} stories, {r.n_families} source families, "
                   f"confidence {r.confidence}); momentum {_mom(r.momentum)}; sentiment {_sent(r.sentiment, r.sentiment_asset)}; "
                   f"active {r.persistence} day(s); first detected {r.first_detected or '–'}.")
        refs = con.execute("""
            SELECT i.headline, i.domain, i.url FROM news.item_narratives l JOIN news.items i USING (item_id)
            WHERE l.narrative_id = ? AND l.method_version = ? AND CAST(i.published_utc AS DATE) = ?
              AND l.confidence <> 'low'
            QUALIFY row_number() OVER (PARTITION BY i.dup_key ORDER BY l.score DESC) = 1
            ORDER BY l.score DESC, i.family LIMIT 3""", [r.narrative_id, mv, report_date]).fetchall()
        out += [f"- {h} ({d}) {u}" for h, d, u in refs]
        out.append("")
    em = cur[cur.status == "emerging"]
    fd = cur[cur.status == "fading"]
    out.append("## Emerging")
    out.append(", ".join(f"{r.title} ({_mom(r.momentum)})" for r in em.itertuples()) or "None.")
    out.append("\n## Losing prominence")
    out.append(", ".join(f"{r.title} ({_mom(r.momentum)})" for r in fd.itertuples()) or "None.")
    out.append(f"\n## Changes since {prev_date or 'the previous observation'}")
    changes = []
    if not prev.empty:
        m = cur.merge(prev, on="narrative_id", how="left", suffixes=("", "_prev"))
        for r in m.itertuples():
            if isinstance(r.status_prev, str) and r.status != r.status_prev:
                changes.append(f"{r.title}: {r.status_prev} → {r.status}")
            elif r.prevalence_prev is not None and not pd.isna(r.prevalence_prev) and abs(r.prevalence - r.prevalence_prev) >= 0.03:
                changes.append(f"{r.title}: prevalence {_pct(r.prevalence_prev)} → {_pct(r.prevalence)}")
    out.append("\n".join(f"- {c}" for c in changes) or "No significant changes.")
    return "\n".join(out)


def save_report(con, md: str, report_date: date, now: datetime, mv: str = METHOD_VERSION) -> None:
    con.execute("INSERT INTO news.daily_report VALUES (?,?,?,?)", [report_date, mv, now, md])


# ---------------------------------------------------------------- entry point

def run(con, now: datetime | None = None, recompute: tuple[date, date] | None = None) -> str | None:
    now = (now or datetime.now(timezone.utc)).replace(tzinfo=None, microsecond=0)
    init(con, now)
    n = classify_new(con, now)
    log.info("%d items newly classified", n)
    if recompute:
        a, b = recompute
        days = [a + timedelta(days=i) for i in range((b - a).days + 1)]
    else:
        days = pending_days(con, now)
    rows = compute_days(con, now, days)
    log.info("%d day(s) measured, %d rows written", len(days), rows)
    if not days:
        return None
    update_narratives(con, now)
    last = max(days)
    md = build_report(con, last)
    save_report(con, md, last, now)
    return md


def main(argv=None):
    from . import db, news_ingest
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--recompute", nargs=2, type=date.fromisoformat, metavar=("START", "END"))
    p.add_argument("--report", type=date.fromisoformat, help="print the report for this date and exit")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    con = db.connect()
    news_ingest.init(con)
    if args.report:
        print(build_report(con, args.report))
        return
    md = run(con, recompute=tuple(args.recompute) if args.recompute else None)
    print(md or "No completed days to measure.")
    con.close()


if __name__ == "__main__":
    main()
