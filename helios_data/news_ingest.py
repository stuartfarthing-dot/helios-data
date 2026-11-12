"""MR-10 news collection: headlines and links from free, licensed sources into the `news` schema.

What is stored is deliberately small: headline, link, source, times, and a short summary only where
the source permits it (Guardian trail text, NYT abstract, RSS description). Never full article text.

Sources (see SOURCES below and docs/news-narratives.md):
  official   central-bank RSS feeds (Fed, BoE, ECB, BoJ, RBA)                      no key
  gdelt      GDELT 2.0 GKG 15-minute files, filtered to a whitelist of outlets       no key
  guardian   Guardian Open Platform, business section                               GUARDIAN_KEY
  nyt        NYT Archive API (one call per month), business / world desks           NYT_KEY
  finnhub    Finnhub general market news (optional)                                  FINNHUB_KEY
  ust        US Treasury auction results (Fiscal Data) -> news.events, not headlines no key

Three timestamps keep research free of look-ahead: published_utc (from the source; for GDELT the
time GDELT first saw the page), collected_utc (first time Helios stored it - never updated) and, in
the narratives step, processed_utc. Rows loaded by a backfill are flagged is_backfill.

    python -m helios_data.news_ingest                       # collect everything new (every 4 hours)
    python -m helios_data.news_ingest --sources gdelt official
    python -m helios_data.news_ingest --backfill guardian --start 2015-01-01 --end 2026-11-12
"""

from __future__ import annotations

import argparse
import hashlib
import html
import io
import json
import logging
import os
import re
import time
import uuid
import zipfile
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from xml.etree import ElementTree as ET

import pandas as pd
import requests

log = logging.getLogger("news")

COLLECTOR_VERSION = "news-1.1"   # 1.1 (13 Nov 2026): tighter GDELT filter
UA = {"User-Agent": "helios-data/1.0 (personal market research; github.com/stuartfarthing-dot/helios-data)"}

SCHEMA = """
CREATE SCHEMA IF NOT EXISTS news;

CREATE TABLE IF NOT EXISTS news.sources (
  source_id VARCHAR, family VARCHAR, name VARCHAR, kind VARCHAR, url VARCHAR,
  licence VARCHAR, stores VARCHAR, active BOOLEAN
);

CREATE TABLE IF NOT EXISTS news.items (
  item_id VARCHAR,            -- sha1 of the cleaned URL (16 hex chars)
  source_id VARCHAR,          -- feed / API it came from (see news.sources)
  family VARCHAR,             -- source family used to normalise prevalence: official, gdelt, guardian, nyt, finnhub
  domain VARCHAR,             -- publisher domain, e.g. reuters.com
  published_utc TIMESTAMP,    -- source's publication time (GDELT: first seen by GDELT)
  collected_utc TIMESTAMP,    -- first stored by Helios; never updated
  headline VARCHAR,
  url VARCHAR,
  excerpt VARCHAR,            -- permitted short summary only, never article text
  gdelt_themes VARCHAR,       -- economic GDELT themes (GDELT items only)
  tone DOUBLE,                -- GDELT document tone (GDELT items only)
  dup_key VARCHAR,            -- hash of the normalised headline: syndicated copies share it
  is_backfill BOOLEAN,
  collector_version VARCHAR
);

CREATE TABLE IF NOT EXISTS news.events (
  event_id VARCHAR, source_id VARCHAR, event_date DATE, event_type VARCHAR, country VARCHAR,
  title VARCHAR, url VARCHAR, detail JSON, collected_utc TIMESTAMP
);

CREATE TABLE IF NOT EXISTS news.collection_log (
  run_id VARCHAR, run_utc TIMESTAMP, source_id VARCHAR, mode VARCHAR,
  window_from TIMESTAMP, window_to TIMESTAMP,
  fetched BIGINT, new_items BIGINT, duplicates BIGINT,
  status VARCHAR,             -- ok, empty, error, skipped
  error VARCHAR, collector_version VARCHAR
);
"""

ITEM_COLS = ["item_id", "source_id", "family", "domain", "published_utc", "collected_utc", "headline", "url",
             "excerpt", "gdelt_themes", "tone", "dup_key", "is_backfill", "collector_version"]

# ---------------------------------------------------------------- source registry

RSS_FEEDS = {
    "fed_press":    ("Federal Reserve press releases", "https://www.federalreserve.gov/feeds/press_all.xml"),
    "fed_speeches": ("Federal Reserve speeches", "https://www.federalreserve.gov/feeds/speeches.xml"),
    "boe_news":     ("Bank of England news", "https://www.bankofengland.co.uk/rss/news"),
    "boe_speeches": ("Bank of England speeches", "https://www.bankofengland.co.uk/rss/speeches"),
    "ecb_press":    ("ECB press, speeches, interviews", "https://www.ecb.europa.eu/rss/press.html"),
    "boj_news":     ("Bank of Japan what's new", "https://www.boj.or.jp/en/rss/whatsnew.xml"),
    "rba_media":    ("RBA media releases", "https://www.rba.gov.au/rss/rss-cb-media-releases.xml"),
    "rba_speeches": ("RBA speeches", "https://www.rba.gov.au/rss/rss-cb-speeches.xml"),
}

SOURCES = [
    # source_id, family, name, kind, url, licence, what is stored
    *[(sid, "official", name, "rss", url, "public sector; reuse with attribution", "title, link, description")
      for sid, (name, url) in RSS_FEEDS.items()],
    ("gdelt", "gdelt", "GDELT 2.0 GKG (whitelisted outlets)", "bulk", "http://data.gdeltproject.org/gdeltv2/",
     "open use with citation of the GDELT Project", "page title, link, themes, tone"),
    ("guardian", "guardian", "Guardian Open Platform - business", "api", "https://content.guardianapis.com/search",
     "free developer key, non-commercial", "headline, trail text, link"),
    ("nyt", "nyt", "New York Times Archive API", "api", "https://api.nytimes.com/svc/archive/v1/",
     "free key, non-commercial, attribution", "headline, abstract, link"),
    ("finnhub", "finnhub", "Finnhub general market news", "api", "https://finnhub.io/api/v1/news",
     "free key, personal use", "headline, summary, link"),
    ("ust", "event", "US Treasury auction results (Fiscal Data)", "api",
     "https://api.fiscaldata.treasury.gov/services/api/fiscal_service/v1/accounting/od/auctions_query",
     "US public domain", "auction results (events, not coverage)"),
]

# Outlets kept from GDELT: established news organisations with financial / economic coverage.
GDELT_DOMAINS = {
    "reuters.com", "apnews.com", "bloomberg.com", "ft.com", "wsj.com", "cnbc.com", "marketwatch.com",
    "barrons.com", "economist.com", "bbc.co.uk", "bbc.com", "theguardian.com", "nytimes.com",
    "washingtonpost.com", "afr.com", "abc.net.au", "smh.com.au", "theaustralian.com.au", "scmp.com",
    "nikkei.com", "japantimes.co.jp", "kitco.com", "finance.yahoo.com", "businessinsider.com", "fortune.com",
    "axios.com", "politico.com", "politico.eu", "telegraph.co.uk", "cityam.com", "thetimes.co.uk",
    "thetimes.com", "independent.co.uk", "news.sky.com", "forbes.com", "caixinglobal.com", "dw.com",
    "euronews.com", "bnnbloomberg.ca", "morningstar.com", "investing.com", "fxstreet.com", "thisismoney.co.uk",
    "economictimes.indiatimes.com", "straitstimes.com", "channelnewsasia.com", "france24.com", "cnn.com",
    "foxbusiness.com", "investopedia.com", "spglobal.com", "mining.com", "oilprice.com",
}
# GKG themes that mark a page as economic / market coverage (prefix match). Broad themes such as EPU_* and
# TAX_FNCACT_ECONOMIST were dropped in news-1.1: they tag a lot of general news.
GDELT_THEME_PREFIXES = ("ECON_", "WB_1104_MACROECONOMIC", "WB_442_INFLATION", "WB_1235_CENTRAL_BANKS",
                        "WB_318_FINANCIAL_ARCHITECTURE", "WB_1920_FINANCIAL_SECTOR", "WB_332_CAPITAL_MARKETS",
                        "WB_450_DEBT")
_RELEVANT_RX = None


def _relevant_title(title: str) -> bool:
    """GDELT pages must also have market / economic words in the title (the narratives RELEVANT list)."""
    global _RELEVANT_RX
    if _RELEVANT_RX is None:
        from .narratives import RELEVANT, _rx
        _RELEVANT_RX = _rx(RELEVANT)
    return bool(_RELEVANT_RX.search(title))

NYT_SECTIONS = {"Business Day", "Business", "Economy", "Your Money", "DealBook", "World", "Markets"}

# ---------------------------------------------------------------- helpers

_TRACKING = re.compile(r"^(utm_|fbclid|gclid|cmpid|mod|ref|smid|partner|ito|taid)", re.I)


def canonical_url(url: str) -> str:
    """Lower-case scheme/host, drop tracking parameters, fragment and trailing slash."""
    p = urlsplit(url.strip())
    host = p.netloc.lower().removeprefix("www.")
    q = urlencode([(k, v) for k, v in parse_qsl(p.query) if not _TRACKING.match(k)])
    path = p.path.rstrip("/") or "/"
    return urlunsplit(("https", host, path, q, ""))


def item_id(url: str) -> str:
    return hashlib.sha1(canonical_url(url).encode()).hexdigest()[:16]


def domain_of(url: str) -> str:
    return urlsplit(url).netloc.lower().removeprefix("www.")


_SUFFIX = re.compile(r"\s+[-|–—:]\s+[^-|–—:]{2,40}$")


def normalise_headline(h: str) -> str:
    """Strip a trailing ' - Outlet' / ' | Outlet', punctuation and case, so syndicated copies match."""
    h = html.unescape(h or "").strip()
    m = _SUFFIX.search(h)
    if m and len(m.group(0).split()) <= 5:
        h = h[: m.start()]
    h = re.sub(r"[^a-z0-9 ]", " ", h.lower())
    return re.sub(r"\s+", " ", h).strip()


def dup_key(headline: str, fallback: str) -> str:
    n = normalise_headline(headline)
    return hashlib.sha1(n.encode()).hexdigest()[:16] if len(n) >= 12 else fallback


def clean_text(s: str | None, limit: int = 400) -> str | None:
    if not s:
        return None
    s = re.sub(r"<[^>]+>", " ", html.unescape(s))
    s = re.sub(r"\s+", " ", s).strip()
    return s[:limit] or None


def to_utc_naive(ts) -> datetime | None:
    if ts is None or ts == "":
        return None
    t = pd.Timestamp(ts)
    if t.tzinfo is None:
        t = t.tz_localize("UTC")
    return t.tz_convert("UTC").tz_localize(None).to_pydatetime()


def make_items(rows: list[dict], source_id: str, family: str, now: datetime, backfill: bool) -> pd.DataFrame:
    out = []
    for r in rows:
        if not r.get("url") or not r.get("headline"):
            continue
        iid = item_id(r["url"])
        out.append({
            "item_id": iid, "source_id": source_id, "family": family,
            "domain": r.get("domain") or domain_of(r["url"]),
            "published_utc": to_utc_naive(r.get("published")), "collected_utc": now,
            "headline": clean_text(r["headline"], 300), "url": r["url"].strip(),
            "excerpt": clean_text(r.get("excerpt")), "gdelt_themes": r.get("themes"), "tone": r.get("tone"),
            "dup_key": dup_key(r["headline"], iid), "is_backfill": backfill, "collector_version": COLLECTOR_VERSION,
        })
    df = pd.DataFrame(out, columns=ITEM_COLS)
    return df.drop_duplicates("item_id", keep="first")


# ---------------------------------------------------------------- warehouse writes

def init(con) -> None:
    con.execute(SCHEMA)
    con.execute("DELETE FROM news.sources")
    con.executemany("INSERT INTO news.sources VALUES (?,?,?,?,?,?,?,TRUE)", [list(s) for s in SOURCES])


def write_items(con, df: pd.DataFrame) -> tuple[int, int]:
    """Insert items not already stored (by item_id). Returns (new, duplicates). Re-running is harmless."""
    if df.empty:
        return 0, 0
    con.register("new_items", df)
    new = con.execute("""SELECT count(*) FROM new_items n
                         WHERE NOT EXISTS (SELECT 1 FROM news.items i WHERE i.item_id = n.item_id)""").fetchone()[0]
    con.execute(f"""INSERT INTO news.items SELECT {', '.join(ITEM_COLS)} FROM new_items n
                    WHERE NOT EXISTS (SELECT 1 FROM news.items i WHERE i.item_id = n.item_id)""")
    con.unregister("new_items")
    return new, len(df) - new


def write_events(con, df: pd.DataFrame) -> int:
    if df.empty:
        return 0
    con.register("new_events", df)
    n = con.execute("""SELECT count(*) FROM new_events n
                       WHERE NOT EXISTS (SELECT 1 FROM news.events e WHERE e.event_id = n.event_id)""").fetchone()[0]
    con.execute("""INSERT INTO news.events SELECT event_id, source_id, event_date, event_type, country, title, url,
                          detail, collected_utc FROM new_events n
                   WHERE NOT EXISTS (SELECT 1 FROM news.events e WHERE e.event_id = n.event_id)""")
    con.unregister("new_events")
    return n


def log_run(con, run_id, source_id, mode, wfrom, wto, fetched, new, dups, status, error=None) -> None:
    con.execute("INSERT INTO news.collection_log VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                [run_id, datetime.now(timezone.utc).replace(tzinfo=None), source_id, mode, wfrom, wto,
                 fetched, new, dups, status, (str(error)[:500] if error else None), COLLECTOR_VERSION])


def last_window(con, source_id: str):
    return con.execute("""SELECT max(window_to) FROM news.collection_log
                          WHERE source_id = ? AND status IN ('ok', 'empty')""", [source_id]).fetchone()[0]


def done_windows(con, source_id: str) -> set:
    rows = con.execute("""SELECT DISTINCT window_from FROM news.collection_log
                          WHERE source_id = ? AND mode = 'backfill' AND status IN ('ok', 'empty')""",
                       [source_id]).fetchall()
    return {r[0] for r in rows}


# ---------------------------------------------------------------- fetchers (network) and parsers (pure)

def http_get(url, params=None, timeout=60, retries=3, pause=5.0) -> requests.Response:
    last = None
    for i in range(retries):
        try:
            r = requests.get(url, params=params, timeout=timeout, headers=UA)
            if r.status_code in (429, 500, 502, 503, 504):
                last = requests.HTTPError(f"HTTP {r.status_code}")
                time.sleep(pause * (i + 1))
                continue
            return r
        except requests.RequestException as exc:
            last = exc
            time.sleep(pause * (i + 1))
    raise last


def parse_rss(text: str) -> list[dict]:
    """RSS 2.0 or Atom -> [{headline, url, published, excerpt}]."""
    root = ET.fromstring(text.encode() if isinstance(text, str) else text)
    rows = []
    for it in root.iter():
        tag = it.tag.split("}")[-1]
        if tag not in ("item", "entry"):
            continue
        get = {c.tag.split("}")[-1]: c for c in it}
        title = (get.get("title").text or "") if get.get("title") is not None else ""
        link_el = get.get("link")
        link = ""
        if link_el is not None:
            link = (link_el.text or link_el.get("href") or "").strip()
        when = None
        for k in ("pubDate", "published", "updated", "date"):
            if get.get(k) is not None and get[k].text:
                raw = get[k].text.strip()
                try:
                    when = parsedate_to_datetime(raw)
                except (TypeError, ValueError):
                    when = pd.Timestamp(raw)
                break
        desc = None
        for k in ("description", "summary"):
            if get.get(k) is not None:
                desc = get[k].text
                break
        rows.append({"headline": title.strip(), "url": link, "published": when, "excerpt": desc})
    return rows


def fetch_rss(url: str) -> str:
    r = http_get(url, timeout=30)
    r.raise_for_status()
    return r.content


GKG_BASE = "http://data.gdeltproject.org/gdeltv2/{stamp}.gkg.csv.zip"
_TITLE = re.compile(r"<PAGE_TITLE>(.*?)</PAGE_TITLE>", re.S)


def parse_gkg(raw: bytes, domains=GDELT_DOMAINS) -> list[dict]:
    """One GKG 2.1 file (tab-separated, 27 columns) -> whitelisted, economics-themed pages."""
    rows = []
    for line in raw.decode("utf-8", errors="replace").splitlines():
        f = line.split("\t")
        if len(f) < 27 or f[2] != "1":           # 1 = ordinary web page
            continue
        dom = f[3].lower().removeprefix("www.")
        if dom not in domains:
            continue
        themes = {t.split(",")[0] for t in f[8].split(";") if t} | {t for t in f[7].split(";") if t}
        econ = sorted(t for t in themes if t.startswith(GDELT_THEME_PREFIXES))
        if not econ:
            continue
        m = _TITLE.search(f[26])
        title = html.unescape(m.group(1)).strip() if m else ""
        if not title or not _relevant_title(title):
            continue
        try:
            tone = float(f[15].split(",")[0])
        except ValueError:
            tone = None
        rows.append({"headline": title, "url": f[4], "domain": dom,
                     "published": datetime.strptime(f[1], "%Y%m%d%H%M%S"),
                     "themes": ",".join(econ[:25]), "tone": tone})
    return rows


def fetch_gkg(stamp: str) -> bytes | None:
    r = http_get(GKG_BASE.format(stamp=stamp), timeout=60, retries=2, pause=3)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
        return z.read(z.namelist()[0])


def gkg_stamps(start: datetime, end: datetime) -> list[str]:
    """15-minute GKG file stamps in (start, end]."""
    t = start.replace(minute=start.minute - start.minute % 15, second=0, microsecond=0) + timedelta(minutes=15)
    out = []
    while t <= end:
        out.append(t.strftime("%Y%m%d%H%M%S"))
        t += timedelta(minutes=15)
    return out


def parse_guardian(js: dict) -> tuple[list[dict], int]:
    resp = js.get("response", {})
    rows = [{"headline": r.get("webTitle"), "url": r.get("webUrl"), "published": r.get("webPublicationDate"),
             "excerpt": (r.get("fields") or {}).get("trailText")} for r in resp.get("results", [])]
    return rows, int(resp.get("pages") or 0)


def fetch_guardian(day_from: date, day_to: date, page: int, key: str) -> dict:
    r = http_get("https://content.guardianapis.com/search", params={
        "section": "business", "from-date": day_from.isoformat(), "to-date": day_to.isoformat(),
        "page-size": 200, "page": page, "order-by": "oldest", "show-fields": "trailText",
        "use-date": "published", "api-key": key}, timeout=60)
    r.raise_for_status()
    return r.json()


def parse_nyt_archive(js: dict, sections=NYT_SECTIONS) -> list[dict]:
    rows = []
    for d in js.get("response", {}).get("docs", []):
        if d.get("section_name") not in sections and d.get("news_desk") not in ("Business", "Financial", "DealBook"):
            continue
        if (d.get("type_of_material") or "").lower() in ("obituary (obit)", "obituary", "correction", "recipe"):
            continue
        h = d.get("headline") or {}
        rows.append({"headline": h.get("main"), "url": d.get("web_url"), "published": d.get("pub_date"),
                     "excerpt": d.get("abstract")})
    return rows


def fetch_nyt_archive(year: int, month: int, key: str) -> dict:
    r = http_get(f"https://api.nytimes.com/svc/archive/v1/{year}/{month}.json", params={"api-key": key},
                 timeout=180, pause=15)
    r.raise_for_status()
    return r.json()


def parse_finnhub(js: list) -> list[dict]:
    return [{"headline": d.get("headline"), "url": d.get("url"),
             "published": datetime.fromtimestamp(d["datetime"], timezone.utc) if d.get("datetime") else None,
             "excerpt": d.get("summary")} for d in js or []]


def fetch_finnhub(key: str) -> list:
    r = http_get("https://finnhub.io/api/v1/news", params={"category": "general", "token": key}, timeout=30)
    r.raise_for_status()
    return r.json()


UST_URL = "https://api.fiscaldata.treasury.gov/services/api/fiscal_service/v1/accounting/od/auctions_query"


def parse_ust(js: dict, now: datetime) -> pd.DataFrame:
    rows = []
    for d in js.get("data", []):
        btc = d.get("bid_to_cover_ratio")
        if btc in (None, "null", ""):
            continue                                # announced but not yet auctioned
        hy = d.get("high_yield") if d.get("high_yield") not in (None, "null") else d.get("high_discnt_rate")
        title = f"US {d.get('security_term')} {d.get('security_type')} auction: high yield/rate {hy}, bid-to-cover {btc}"
        rows.append({"event_id": f"ust_{d.get('cusip')}_{d.get('auction_date')}", "source_id": "ust",
                     "event_date": d.get("auction_date"), "event_type": "treasury_auction", "country": "US",
                     "title": title, "url": "https://fiscaldata.treasury.gov/datasets/treasury-securities-auctions-data/",
                     "detail": json.dumps({k: d.get(k) for k in (
                         "security_type", "security_term", "cusip", "offering_amt", "total_accepted", "high_yield",
                         "high_discnt_rate", "bid_to_cover_ratio", "primary_dealer_accepted",
                         "direct_bidder_accepted", "indirect_bidder_accepted", "issue_date")}),
                     "collected_utc": now})
    return pd.DataFrame(rows, columns=["event_id", "source_id", "event_date", "event_type", "country", "title",
                                       "url", "detail", "collected_utc"])


def fetch_ust(since: date, page: int = 1) -> dict:
    r = http_get(UST_URL, params={"filter": f"auction_date:gte:{since.isoformat()}", "sort": "auction_date",
                                  "page[size]": 1000, "page[number]": page}, timeout=60)
    r.raise_for_status()
    return r.json()


# ---------------------------------------------------------------- collectors

class Ctx:
    def __init__(self, con, now=None, run_id=None):
        self.con = con
        self.now = (now or datetime.now(timezone.utc)).replace(tzinfo=None, microsecond=0)
        self.run_id = run_id or uuid.uuid4().hex[:12]
        self.summary = {}

    def store(self, source_id, family, rows, mode, wfrom, wto, backfill=False):
        df = make_items(rows, source_id, family, self.now, backfill)
        new, dups = write_items(self.con, df)
        status = "ok" if len(rows) else "empty"
        log_run(self.con, self.run_id, source_id, mode, wfrom, wto, len(rows), new, dups, status)
        s = self.summary.setdefault(source_id, [0, 0, 0])
        s[0] += len(rows); s[1] += new; s[2] += dups
        return new

    def fail(self, source_id, mode, wfrom, wto, exc):
        log.warning("%s failed: %s", source_id, exc)
        log_run(self.con, self.run_id, source_id, mode, wfrom, wto, 0, 0, 0, "error", exc)
        self.summary.setdefault(source_id, [0, 0, 0])


def collect_official(ctx: Ctx, fetch=fetch_rss) -> None:
    for sid, (_, url) in RSS_FEEDS.items():
        try:
            rows = parse_rss(fetch(url))
            ctx.store(sid, "official", rows, "live", None, ctx.now)
        except Exception as exc:
            ctx.fail(sid, "live", None, ctx.now, exc)


def collect_gdelt(ctx: Ctx, fetch=fetch_gkg, max_hours: int = 48) -> None:
    """Process every 15-minute GKG file since the last processed one (at most max_hours back)."""
    last = last_window(ctx.con, "gdelt")
    # files appear ~15 minutes after their stamp; stop 30 minutes short of now
    end = ctx.now - timedelta(minutes=30)
    start = max(last, end - timedelta(hours=max_hours)) if last else end - timedelta(hours=4)
    for stamp in gkg_stamps(start, end):
        t = datetime.strptime(stamp, "%Y%m%d%H%M%S")
        try:
            raw = fetch(stamp)
            if raw is None:
                log_run(ctx.con, ctx.run_id, "gdelt", "live", t - timedelta(minutes=15), t, 0, 0, 0, "error",
                        "file missing (404)")
                continue
            ctx.store("gdelt", "gdelt", parse_gkg(raw), "live", t - timedelta(minutes=15), t)
        except Exception as exc:
            ctx.fail("gdelt", "live", t - timedelta(minutes=15), t, exc)


def collect_guardian(ctx: Ctx, start: date, end: date, mode="live", fetch=fetch_guardian,
                     key=None, chunk_days=31, max_calls=4500) -> None:
    key = key or os.getenv("GUARDIAN_KEY")
    if not key:
        log_run(ctx.con, ctx.run_id, "guardian", mode, None, None, 0, 0, 0, "skipped", "GUARDIAN_KEY not set")
        return
    done = done_windows(ctx.con, "guardian") if mode == "backfill" else set()
    calls, d = 0, start
    while d <= end and calls < max_calls:
        d2 = min(d + timedelta(days=chunk_days - 1), end)
        wfrom, wto = datetime.combine(d, datetime.min.time()), datetime.combine(d2, datetime.max.time()).replace(microsecond=0)
        if wfrom in done:
            d = d2 + timedelta(days=1)
            continue
        try:
            rows, page, pages = [], 1, 1
            while page <= pages:
                js = fetch(d, d2, page, key)
                calls += 1
                got, pages = parse_guardian(js)
                rows += got
                page += 1
                time.sleep(0.2 if fetch is fetch_guardian else 0)
            ctx.store("guardian", "guardian", rows, mode, wfrom, wto, backfill=(mode == "backfill"))
        except Exception as exc:
            ctx.fail("guardian", mode, wfrom, wto, exc)
        d = d2 + timedelta(days=1)


def collect_nyt(ctx: Ctx, start: date, end: date, mode="live", fetch=fetch_nyt_archive, key=None,
                pause=13.0) -> None:
    key = key or os.getenv("NYT_KEY")
    if not key:
        log_run(ctx.con, ctx.run_id, "nyt", mode, None, None, 0, 0, 0, "skipped", "NYT_KEY not set")
        return
    done = done_windows(ctx.con, "nyt") if mode == "backfill" else set()
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        wfrom = datetime(y, m, 1)
        wto = (datetime(y + (m == 12), m % 12 + 1, 1) - timedelta(seconds=1))
        if wfrom not in done:
            try:
                rows = parse_nyt_archive(fetch(y, m, key))
                ctx.store("nyt", "nyt", rows, mode, wfrom, wto, backfill=(mode == "backfill"))
            except Exception as exc:
                ctx.fail("nyt", mode, wfrom, wto, exc)
            if fetch is fetch_nyt_archive:
                time.sleep(pause)               # NYT allows about 5 calls a minute
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)


def collect_finnhub(ctx: Ctx, fetch=fetch_finnhub, key=None) -> None:
    key = key or os.getenv("FINNHUB_KEY")
    if not key:
        log_run(ctx.con, ctx.run_id, "finnhub", "live", None, None, 0, 0, 0, "skipped", "FINNHUB_KEY not set")
        return
    try:
        ctx.store("finnhub", "finnhub", parse_finnhub(fetch(key)), "live", None, ctx.now)
    except Exception as exc:
        ctx.fail("finnhub", "live", None, ctx.now, exc)


def collect_ust(ctx: Ctx, since: date, mode="live", fetch=fetch_ust) -> None:
    try:
        page, pages, total, new = 1, 1, 0, 0
        while page <= pages:
            js = fetch(since, page)
            pages = int((js.get("meta") or {}).get("total-pages") or 1)
            df = parse_ust(js, ctx.now)
            total += len(df)
            new += write_events(ctx.con, df)
            page += 1
        log_run(ctx.con, ctx.run_id, "ust", mode, datetime.combine(since, datetime.min.time()), ctx.now,
                total, new, total - new, "ok" if total else "empty")
        ctx.summary["ust"] = [total, new, total - new]
    except Exception as exc:
        ctx.fail("ust", mode, None, ctx.now, exc)


ALL_LIVE = ["official", "gdelt", "guardian", "nyt", "finnhub", "ust"]


def run_live(con, sources=None, now=None) -> Ctx:
    ctx = Ctx(con, now)
    today = ctx.now.date()
    for s in sources or ALL_LIVE:
        if s == "official":
            collect_official(ctx)
        elif s == "gdelt":
            collect_gdelt(ctx)
        elif s == "guardian":
            collect_guardian(ctx, today - timedelta(days=2), today)
        elif s == "nyt":
            # one archive call per day is enough (and NYT's limit is tight): only on the first run after 20:00 UTC
            ran_today = con.execute("""SELECT count(*) FROM news.collection_log WHERE source_id='nyt' AND mode='live'
                                       AND status='ok' AND CAST(run_utc AS DATE) = ?""", [today]).fetchone()[0]
            if ctx.now.hour >= 20 and not ran_today:
                if today.day <= 2:              # catch the end of the previous month too
                    collect_nyt(ctx, (today.replace(day=1) - timedelta(days=1)).replace(day=1), today)
                else:
                    collect_nyt(ctx, today, today)
        elif s == "finnhub":
            collect_finnhub(ctx)
        elif s == "ust":
            collect_ust(ctx, today - timedelta(days=45))   # Fiscal Data posts results a week or two late
    return ctx


def run_backfill(con, source: str, start: date, end: date) -> Ctx:
    ctx = Ctx(con)
    if source == "guardian":
        collect_guardian(ctx, start, end, mode="backfill")
    elif source == "nyt":
        collect_nyt(ctx, start, end, mode="backfill")
    elif source == "ust":
        collect_ust(ctx, start, mode="backfill")
    else:
        raise SystemExit(f"no backfill for {source} (gdelt, official and finnhub are collected going forward only)")
    return ctx


def main(argv=None):
    from . import db
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--sources", nargs="*", choices=ALL_LIVE)
    p.add_argument("--backfill", choices=["guardian", "nyt", "ust"])
    p.add_argument("--start", type=date.fromisoformat, default=date(2015, 1, 1))
    p.add_argument("--end", type=date.fromisoformat, default=None)
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    con = db.connect()
    init(con)
    if args.backfill:
        ctx = run_backfill(con, args.backfill, args.start, args.end or datetime.now(timezone.utc).date())
    else:
        ctx = run_live(con, args.sources)
    for sid, (fetched, new, dups) in sorted(ctx.summary.items()):
        log.info("%-13s fetched %6d  new %6d  already stored %6d", sid, fetched, new, dups)
    errors = con.execute("""SELECT source_id, count(*) FROM news.collection_log WHERE run_id = ?
                            AND status = 'error' GROUP BY 1""", [ctx.run_id]).fetchall()
    for sid, n in errors:
        log.warning("%s: %d failed fetches this run (see news.collection_log)", sid, n)
    con.close()


if __name__ == "__main__":
    main()
