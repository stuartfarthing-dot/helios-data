"""Economic calendar and UK share dividends.

Two feeds, both free and key-less:

  econ_calendar  scheduled economic releases (all major currencies) with an impact rating.
                 History 2007 - Apr 2025 was loaded once from the Forex Factory archive on Hugging Face
                 (source = 'ff_archive'). This script adds this week's and next week's events from the
                 Forex Factory weekly feed (source = 'ff_weekly'). The weekly feed carries forecast and
                 previous values but not the actual figure.
  dividends      ex-dividend dates and amounts for every UK share in config.UK_SHARES, from Yahoo
                 Finance via yfinance. Amounts are as Yahoo reports them (normally pence per share for
                 .L tickers). The full history is re-read each run and replaces the symbol's rows.

policy_events (BoE / Fed / ECB decisions, UK Budgets) is a curated table maintained by hand in the
warehouse; it is not touched here.

    python -m helios_data.events_ingest                 # calendar + ETF daily prices (the nightly default)
    python -m helios_data.events_ingest --only dividends --symbols BP VOD

UK shares were taken out of scope on 10 Nov 2026, so dividends are no longer refreshed nightly;
the tables are kept and can be refreshed by hand with --only dividends.
"""

from __future__ import annotations

import argparse
import logging
import os
import time

import pandas as pd
import requests

from .config import UK_SHARES

log = logging.getLogger("events")

FF_FEEDS = [
    "https://nfs.faireconomy.media/ff_calendar_thisweek.json",
    "https://nfs.faireconomy.media/ff_calendar_nextweek.json",
]

# Yahoo uses different tickers for a few LSE lines. Anything not listed is "<ticker>.L".
YAHOO_TICKERS = {
    "BT": "BT-A.L",      # BT Group trades as BT.A
    "RDSB": "SHEL.L",    # Shell unified its share lines in 2022
    "RBS": "NWG.L",      # renamed NatWest Group
    "RB": "RKT.L",       # Reckitt ticker change
    "BDEV": "BTRW.L",    # Barratt Redrow
    "ECM": "RS1.L",      # Electrocomponents -> RS Group
    "SLA": "ABDN.L",     # Standard Life Aberdeen -> abrdn
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS econ_calendar (
  event_date DATE, event_time_utc TIMESTAMP, currency VARCHAR, impact VARCHAR, event VARCHAR,
  actual VARCHAR, forecast VARCHAR, previous VARCHAR, source VARCHAR
);
CREATE TABLE IF NOT EXISTS dividends (
  symbol VARCHAR, yahoo_symbol VARCHAR, ex_date DATE, amount DOUBLE,
  source VARCHAR, loaded_at TIMESTAMP DEFAULT current_timestamp
);
CREATE TABLE IF NOT EXISTS splits (
  symbol VARCHAR, yahoo_symbol VARCHAR, split_date DATE, ratio DOUBLE,
  source VARCHAR, loaded_at TIMESTAMP DEFAULT current_timestamp
);
"""

IMPACT = {"High": "high", "Medium": "medium", "Low": "low", "Holiday": "holiday", "Non-Economic": "holiday"}


# ---------------------------------------------------------------- economic calendar

def parse_ff_week(items: list[dict]) -> pd.DataFrame:
    rows = []
    for e in items:
        ts = pd.Timestamp(e["date"])
        ts = ts.tz_convert("UTC").tz_localize(None) if ts.tzinfo else ts
        rows.append({
            "event_date": ts.date(), "event_time_utc": ts,
            "currency": e.get("country"), "impact": IMPACT.get(e.get("impact"), (e.get("impact") or "").lower()),
            "event": e.get("title"), "actual": e.get("actual") or None,
            "forecast": e.get("forecast") or None, "previous": e.get("previous") or None,
            "source": "ff_weekly",
        })
    return pd.DataFrame(rows, columns=["event_date", "event_time_utc", "currency", "impact", "event",
                                       "actual", "forecast", "previous", "source"])


def fetch_ff_week(url: str) -> list[dict]:
    r = requests.get(url, timeout=30, headers={"User-Agent": "helios-data/1.0"})
    if r.status_code == 404:          # next week's file only appears part-way through the week
        return []
    r.raise_for_status()
    return r.json()


def write_calendar(con, df: pd.DataFrame) -> int:
    """Replace weekly-feed rows for the dates covered (archive rows are never touched)."""
    if df.empty:
        return 0
    con.register("cal_df", df)
    con.execute("""DELETE FROM econ_calendar WHERE source = 'ff_weekly'
                   AND event_date BETWEEN (SELECT min(event_date) FROM cal_df) AND (SELECT max(event_date) FROM cal_df)""")
    con.execute("INSERT INTO econ_calendar SELECT * FROM cal_df")
    con.unregister("cal_df")
    return len(df)


def update_calendar(con, fetch=fetch_ff_week) -> int:
    frames = []
    for i, url in enumerate(FF_FEEDS):
        if i:
            time.sleep(2)
        try:
            frames.append(parse_ff_week(fetch(url)))
        except Exception as exc:  # one feed failing should not stop the other
            log.warning("calendar feed %s failed: %s", url, exc)
    df = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    n = write_calendar(con, df)
    log.info("econ_calendar: %d weekly-feed events written", n)
    return n


# ---------------------------------------------------------------- dividends

def yahoo_symbol(ticker: str) -> str:
    return YAHOO_TICKERS.get(ticker, f"{ticker}.L")


def _fetch_yahoo(ysym: str):
    import yfinance as yf
    t = yf.Ticker(ysym)
    return t.dividends, t.splits


def fetch_dividends(ysym: str, timeout: float = 30):
    """Returns (dividends, splits). Yahoo's dividend amounts are already adjusted for later splits
    and share consolidations; the splits table lets research undo that against unadjusted prices.
    Yahoo sometimes never answers, so each share gets a hard time limit."""
    from concurrent.futures import ThreadPoolExecutor
    ex = ThreadPoolExecutor(max_workers=1)
    try:
        return ex.submit(_fetch_yahoo, ysym).result(timeout=timeout)
    finally:
        ex.shutdown(wait=False, cancel_futures=True)


def write_splits(con, ticker: str, ysym: str, s) -> int:
    con.execute("DELETE FROM splits WHERE symbol = ?", [f"{ticker}.L"])
    if s is None or len(s) == 0:
        return 0
    idx = pd.DatetimeIndex(s.index)
    df = pd.DataFrame({"symbol": f"{ticker}.L", "yahoo_symbol": ysym, "split_date": [d.date() for d in idx],
                       "ratio": s.astype(float).values, "source": "yahoo"})
    con.register("spl_df", df)
    con.execute("INSERT INTO splits (symbol, yahoo_symbol, split_date, ratio, source) "
                "SELECT symbol, yahoo_symbol, split_date, ratio, source FROM spl_df")
    con.unregister("spl_df")
    return len(df)


def write_dividends(con, ticker: str, ysym: str, s: pd.Series) -> int:
    if s is None or len(s) == 0:
        return 0
    idx = pd.DatetimeIndex(s.index)
    df = pd.DataFrame({
        "symbol": f"{ticker}.L", "yahoo_symbol": ysym,
        "ex_date": [d.date() for d in idx], "amount": s.astype(float).values, "source": "yahoo",
    })
    con.register("div_df", df)
    con.execute("DELETE FROM dividends WHERE symbol = ?", [f"{ticker}.L"])
    con.execute("INSERT INTO dividends (symbol, yahoo_symbol, ex_date, amount, source) "
                "SELECT symbol, yahoo_symbol, ex_date, amount, source FROM div_df")
    con.unregister("div_df")
    return len(df)


def update_dividends(con, tickers=None, fetch=fetch_dividends, pause: float = 1.5) -> dict:
    tickers = tickers or list(UK_SHARES)
    got, empty, failed = 0, [], []
    for t in tickers:
        ysym = yahoo_symbol(t)
        res = None
        for attempt in range(3):             # Yahoo rate-limits bursts; back off and retry
            try:
                res = fetch(ysym)
                break
            except Exception as exc:
                log.warning("dividends %s (%s) attempt %d failed: %s", t, ysym, attempt + 1, exc)
                time.sleep(5 * (attempt + 1))
        if res is None:
            failed.append(t)
            continue
        log.info("dividends %s (%s) ok", t, ysym)
        divs, spl = res if isinstance(res, tuple) else (res, None)
        n = write_dividends(con, t, ysym, divs)
        write_splits(con, t, ysym, spl)
        if n:
            got += 1
        else:
            empty.append(t)          # usually a company that has since delisted
        time.sleep(pause)
    log.info("dividends: %d shares with history, %d empty %s, %d failed %s",
             got, len(empty), empty, len(failed), failed)
    return {"got": got, "empty": empty, "failed": failed}


# ---------------------------------------------------------------- ETF daily prices (Yahoo)

# Daily closes for the gold/silver ETFs, adjusted for dividends and splits. Silver miners (SIL, SILJ)
# are not on Dukascopy; the rest are here too because Yahoo's history is longer. Stored in `bars`
# with source = 'yahoo', timeframe = '1d', ts = the trading date (NYSE session).
YAHOO_DAILY = ["GDX", "GDXJ", "SIL", "SILJ", "GLD", "SLV"]


def _fetch_yahoo_history(ticker: str):
    import yfinance as yf
    return yf.Ticker(ticker).history(period="max", auto_adjust=True)


def fetch_yahoo_daily(ticker: str, timeout: float = 60):
    from concurrent.futures import ThreadPoolExecutor
    ex = ThreadPoolExecutor(max_workers=1)
    try:
        return ex.submit(_fetch_yahoo_history, ticker).result(timeout=timeout)
    finally:
        ex.shutdown(wait=False, cancel_futures=True)


def update_yahoo_daily(con, tickers=None, fetch=fetch_yahoo_daily) -> dict:
    from . import db
    out = {}
    for t in tickers or YAHOO_DAILY:
        try:
            h = fetch(t)
        except Exception as exc:
            log.warning("yahoo daily %s failed: %s", t, exc)
            continue
        if h is None or len(h) == 0:
            log.warning("yahoo daily %s: no data", t)
            continue
        idx = pd.DatetimeIndex(h.index)
        df = pd.DataFrame({"ts": pd.to_datetime([d.date() for d in idx]).tz_localize("UTC"),
                           "open": h["Open"].values, "high": h["High"].values, "low": h["Low"].values,
                           "close": h["Close"].values, "volume": h["Volume"].values})
        out[t] = db.write_bars(con, "yahoo", t, "1d", df, note="yfinance auto_adjust")
        time.sleep(1)
    log.info("yahoo daily: %s", out)
    return out


# ---------------------------------------------------------------- entry point

def main(argv=None):
    from . import db
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--only", choices=["calendar", "dividends", "etfs"])
    p.add_argument("--symbols", nargs="*", help="UK share tickers without .L (default: all)")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    con = db.connect()
    con.execute(SCHEMA)
    if args.only in (None, "calendar"):
        update_calendar(con)
    if args.only in (None, "etfs"):
        update_yahoo_daily(con)
    if args.only == "dividends":
        res = update_dividends(con, args.symbols)
        if res["got"] == 0:
            raise SystemExit("no dividend history fetched at all - Yahoo may be blocking; check the log")
    con.close()
    os._exit(0)   # don't wait for any Yahoo request that is still hanging in the background


if __name__ == "__main__":
    main()
