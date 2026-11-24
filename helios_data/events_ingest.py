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

    python -m helios_data.events_ingest                 # calendar + ETF daily prices + GLD tonnes (nightly)
    python -m helios_data.events_ingest --only etfs     # ETF/futures prices and GLD tonnes only
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
CREATE TABLE IF NOT EXISTS gld_holdings (
  holding_date DATE, tonnes DOUBLE, ounces DOUBLE, nav_usd DOUBLE, source VARCHAR,
  loaded_at TIMESTAMP DEFAULT current_timestamp
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
# ^FTSE (official FTSE 100 close, London session) is the trading calendar and reference for uk100_cash (H-000).
YAHOO_DAILY = ["GDX", "GDXJ", "SIL", "SILJ", "GLD", "SLV", "^FTSE"]

# Futures (19 Nov 2026, Q10.2): real exchange volume for the CFD markets, which Dukascopy only gives as
# tick counts (a weak proxy: see 'Volume checks with real exchange volume'). Yahoo's "=F" series follow
# the front contract, so volume dips around each roll; compare volume with its own recent median and
# treat roll weeks with care. COMEX/NYMEX metals and energy, CME equity indices.
YAHOO_FUTURES = ["GC=F", "SI=F", "PL=F", "HG=F", "BZ=F", "CL=F", "ES=F", "NQ=F", "YM=F", "RTY=F"]

# ETFs as a "second crowd" (24 Nov 2026, Q16): what fund buyers do alongside the CFD markets.
# Broad US (SPY, QQQ, IWM), leveraged long/short (TQQQ, SQQQ, SPXU - retail fear and greed),
# country funds for the home markets (EWU UK, EWG Germany, EWJ Japan, EWH Hong Kong),
# credit (HYG junk, LQD investment grade - risk appetite), oil (USO) and copper miners (COPX).
YAHOO_ETFS = ["SPY", "QQQ", "IWM", "TQQQ", "SQQQ", "SPXU", "EWU", "EWG", "EWJ", "EWH",
              "HYG", "LQD", "USO", "COPX"]


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
    for t in tickers or (YAHOO_DAILY + YAHOO_ETFS + YAHOO_FUTURES):
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


# ---------------------------------------------------------------- GLD holdings

# Tonnes of gold held by SPDR Gold Shares (GLD), daily since Nov 2004: creations and redemptions show
# whether big fund money is buying or selling physical gold. Since late 2026 the trust publishes the
# archive as an Excel workbook from its API (the old CSV address now returns a PDF). Sheet "US GLD
# Historical Archive": Date (text, e.g. 23-Nov-2026), ..., Total Ounces of Gold in the Trust, Tonnes of
# Gold, Total Net Asset Value in the Trust. Holidays carry "US Holiday"; a day not yet final "AWAITED".
GLD_ARCHIVE_URL = "https://api.spdrgoldshares.com/api/v1/historical-archive?product=gld&exchange=NYSE&lang=en"


def fetch_gld_archive(url: str = GLD_ARCHIVE_URL, timeout: float = 60) -> bytes:
    r = requests.get(url, timeout=timeout, headers={"User-Agent": "Mozilla/5.0 (helios-data)"})
    r.raise_for_status()
    return r.content


def _num(v):
    try:
        return float(str(v).replace(",", "").replace("$", "").strip())
    except ValueError:
        return None


def _gld_rows(data) -> list[list[str]]:
    """Rows of cell text from the archive: an .xlsx workbook (bytes starting PK) or CSV text."""
    import csv
    import io
    if isinstance(data, bytes) and data[:2] == b"PK":
        sheets = pd.read_excel(io.BytesIO(data), sheet_name=None, header=None, dtype=str)
        name = next((k for k in sheets if "archive" in k.lower()), list(sheets)[-1])
        return sheets[name].fillna("").astype(str).values.tolist()
    if isinstance(data, bytes):
        if data[:4] == b"%PDF":
            raise ValueError("GLD archive: the address returned a PDF, not data")
        data = data.decode("utf-8-sig", errors="replace")
    # Bare carriage returns as line ends make csv fail; normalise them first.
    text = data.replace("\r\n", "\n").replace("\r", "\n")
    return list(csv.reader(io.StringIO(text)))


def parse_gld_archive(data) -> pd.DataFrame:
    rows = _gld_rows(data)

    # Header = first row with a "date" cell and a "tonnes" cell (date column anywhere; stray spaces or
    # a byte-order mark ignored).
    def norm(c):
        return str(c).replace("\ufeff", "").strip().lower()
    hdr_i = next((i for i, r in enumerate(rows)
                  if any(norm(c).startswith("date") for c in r)
                  and any("tonnes" in norm(c) for c in r)), None)
    if hdr_i is None:
        raise ValueError("GLD archive: no header row with Date and Tonnes; starts: %r" % rows[:3])
    hdr = [norm(c) for c in rows[hdr_i]]
    di = next(i for i, c in enumerate(hdr) if c.startswith("date"))
    ti = next(i for i, c in enumerate(hdr) if "tonnes" in c)
    # Total ounces in the trust, not "ounces of gold per share".
    oi = next((i for i, c in enumerate(hdr) if "ounces" in c and "per share" not in c), None)
    ni = next((i for i, c in enumerate(hdr) if "net asset value" in c and "/share" not in c
               and "per share" not in c and "ounces" not in c and "tonnes" not in c), None)
    out = []
    for r in rows[hdr_i + 1:]:
        if len(r) <= max(ti, di) or not str(r[di]).strip():
            continue
        d = pd.to_datetime(str(r[di]).strip(), format="mixed", dayfirst=True, errors="coerce")
        t = _num(r[ti])
        if pd.isna(d) or t is None:
            continue           # holiday, AWAITED or footer line
        out.append({"holding_date": d.date(), "tonnes": t,
                    "ounces": _num(r[oi]) if oi is not None and len(r) > oi else None,
                    "nav_usd": _num(r[ni]) if ni is not None and len(r) > ni else None})
    df = pd.DataFrame(out, columns=["holding_date", "tonnes", "ounces", "nav_usd"])
    return df.drop_duplicates("holding_date", keep="last").sort_values("holding_date")


def update_gld_holdings(con, fetch=fetch_gld_archive) -> int:
    """Replace gld_holdings with the full archive. Never raises: a failed download only logs."""
    try:
        df = parse_gld_archive(fetch())
    except Exception as exc:
        log.warning("GLD holdings failed: %s", exc)
        return 0
    if df.empty:
        log.warning("GLD holdings: archive parsed to 0 rows - left the table as it was")
        return 0
    df["source"] = "spdr_archive"
    con.execute("DELETE FROM gld_holdings")
    con.register("gld_df", df)
    con.execute("INSERT INTO gld_holdings (holding_date, tonnes, ounces, nav_usd, source) "
                "SELECT holding_date, tonnes, ounces, nav_usd, source FROM gld_df")
    con.unregister("gld_df")
    log.info("GLD holdings: %d days, last %s = %.1f t", len(df), df.holding_date.iloc[-1], df.tonnes.iloc[-1])
    return len(df)


# ---------------------------------------------------------------- entry point

def main(argv=None):
    from . import db
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--only", choices=["calendar", "dividends", "etfs"])
    p.add_argument("--symbols", nargs="*", help="UK share tickers without .L (default: all); with --only etfs, "
                   "Yahoo tickers (default: all ETFs, ^FTSE and futures)")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    con = db.connect()
    con.execute(SCHEMA)
    if args.only in (None, "calendar"):
        update_calendar(con)
    if args.only in (None, "etfs"):
        update_yahoo_daily(con, args.symbols if args.only == "etfs" else None)
        if not (args.only == "etfs" and args.symbols):
            update_gld_holdings(con)
    if args.only == "dividends":
        res = update_dividends(con, args.symbols)
        if res["got"] == 0:
            raise SystemExit("no dividend history fetched at all - Yahoo may be blocking; check the log")
    con.close()
    os._exit(0)   # don't wait for any Yahoo request that is still hanging in the background


if __name__ == "__main__":
    main()
