"""IG client sentiment: the share of IG clients long and short in each market, recorded nightly.

This is behavioural data (what the retail crowd is doing). IG only gives today's figure, so the
history starts on the day this runs for the first time. Read-only: nothing is traded.

    python -m helios_data.ig_sentiment            # record today's sentiment for every market below
    python -m helios_data.ig_sentiment --show     # also print which IG markets were matched
"""

from __future__ import annotations

import argparse
import logging
import time
from datetime import datetime, timezone

import pandas as pd

log = logging.getLogger("ig_sentiment")

# Helios symbol -> IG search term. The first search result whose name starts with the term is used;
# a known epic is used directly where we have one.
MARKETS = {
    "UK100": "FTSE 100", "US500": "US 500", "NAS100": "US Tech 100", "US30": "Wall Street",
    "GER40": "Germany 40", "AUS200": "Australia 200",
    "XAUUSD": "Spot Gold", "XAGUSD": "Spot Silver", "XPTUSD": "Platinum",
    "COPPER": "Copper", "BRENT": "Brent Crude", "BTCUSD": "Bitcoin", "ETHUSD": "Ether",
}
# Epics pinned from the markets this step matched on 10-12 Nov 2026 (table ig_sentiment), so the nightly run
# makes no search calls: fewer requests against IG's per-minute allowance, and nothing depends on the market
# search API (version 1 is decommissioned on 1 Dec 2026). Brent is still found by search (version 2).
KNOWN_EPICS = {
    "UK100": "IX.D.FTSE.DAILY.IP", "US500": "IX.D.SPTRD.DAILY.IP", "NAS100": "IX.D.NASDAQ.CASH.IP",
    "US30": "IX.D.DOW.DAILY.IP", "GER40": "IX.D.DAX.DAILY.IP", "AUS200": "IX.D.ASX.MONTH1.IP",
    "XAUUSD": "CS.D.USCGC.TODAY.IP", "XAGUSD": "CS.D.USCSI.TODAY.IP", "XPTUSD": "MT.D.PL.Month2.IP",
    "COPPER": "CS.D.COPPER.TODAY.IP", "BTCUSD": "CS.D.BITCOIN.TODAY.IP", "ETHUSD": "CS.D.ETHUSD.TODAY.IP",
}

# IG limits non-trading API requests per minute. This step makes two calls per market (search and market
# details), so it runs straight after the price and trade downloads and was hitting the per-minute limit
# (403 exceeded-api-key-allowance from 13 Nov 2026). Pace the calls, and wait out the minute once if IG refuses.
PAUSE_SECONDS = 2.5


def _paced(fn, *args, **kwargs):
    time.sleep(PAUSE_SECONDS)
    try:
        return fn(*args, **kwargs)
    except Exception as e:
        if "allowance" not in str(e):
            raise
        log.info("IG per-minute allowance reached; waiting 65 seconds and trying once more")
        time.sleep(65)
        return fn(*args, **kwargs)

SCHEMA = """
CREATE TABLE IF NOT EXISTS ig_sentiment (
  as_of DATE, symbol VARCHAR, epic VARCHAR, market_id VARCHAR,
  long_pct DOUBLE, short_pct DOUBLE, fetched_at TIMESTAMP
);
"""


def pick_epic(results: list[dict], term: str) -> str | None:
    """Prefer a daily-funded bet whose name starts with the search term."""
    t = term.lower()
    named = [m for m in results if m.get("instrumentName", "").lower().startswith(t)]
    for m in named:
        if m.get("expiry") in ("DFB", "-"):
            return m["epic"]
    return named[0]["epic"] if named else None


def parse_sentiment(resp: dict) -> dict:
    return {s["marketId"]: (s.get("longPositionPercentage"), s.get("shortPositionPercentage"))
            for s in resp.get("clientSentiments", [])}


def update(con, ig, today=None) -> pd.DataFrame:
    con.execute(SCHEMA)
    today = today or datetime.now(timezone.utc).date()
    rows = []
    for sym, term in MARKETS.items():
        try:
            epic = KNOWN_EPICS.get(sym) or pick_epic(_paced(ig.search, term), term)
            if not epic:
                log.warning("%s: no IG market found for '%s'", sym, term)
                continue
            market_id = _paced(ig._req, "GET", f"/markets/{epic}", "3").json()["instrument"]["marketId"]
            rows.append({"symbol": sym, "epic": epic, "market_id": market_id})
        except Exception as e:
            log.warning("%s: could not resolve IG market: %s", sym, e)
    if not rows:
        return pd.DataFrame()
    ids = ",".join(sorted({r["market_id"] for r in rows}))
    sent = parse_sentiment(_paced(ig._req, "GET", "/clientsentiment", "1", params={"marketIds": ids}).json())
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    for r in rows:
        r["long_pct"], r["short_pct"] = sent.get(r["market_id"], (None, None))
        r["as_of"], r["fetched_at"] = today, now
    df = pd.DataFrame(rows)[["as_of", "symbol", "epic", "market_id", "long_pct", "short_pct", "fetched_at"]]
    con.register("sent_df", df)
    con.execute("DELETE FROM ig_sentiment WHERE as_of = ?", [today])
    con.execute("INSERT INTO ig_sentiment SELECT * FROM sent_df")
    con.unregister("sent_df")
    log.info("ig_sentiment: %d markets recorded for %s", len(df), today)
    return df


def main(argv=None):
    from . import db
    from .ig_ingest import IG
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--show", action="store_true")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    ig = IG()
    ig.login()
    try:
        df = update(db.connect(), ig)
        if args.show:
            print(df.to_string(index=False))
    finally:
        ig.logout()


if __name__ == "__main__":
    main()
