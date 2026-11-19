"""Load London Stock Exchange daily bars, with real exchange volume, from Interactive Brokers.

Why: H-000 (18 Nov 2026) found that IG's and Dukascopy's "volume" is a count of their own price
updates, not shares traded (HSBC, May-Nov 2026: daily correlation with real LSE volume 0.24).
IBKR supplies the exchange's own daily bars, so this is the source for participation, breadth
and closing-auction work.

Needs IB Gateway (or Trader Workstation) running on this PC and logged in, with API access on:
    IB Gateway: Configure > Settings > API > Settings > tick "Enable ActiveX and Socket Clients",
    tick "Read-Only API". Live Gateway listens on port 4001 (TWS: 7496).
Settings come from .env (all optional): IBKR_HOST (127.0.0.1), IBKR_PORT (4001), IBKR_CLIENT_ID (17).

    python -m helios_data.ibkr_ingest --test               # one share (HSBA): checks access, writes nothing
    python -m helios_data.ibkr_ingest                      # every share in config.UK_SHARES, 5 years first time
    python -m helios_data.ibkr_ingest --symbols HSBA BP    # just these
    python -m helios_data.ibkr_ingest --intraday           # 5-minute bars for INTRADAY (two years first time)
    python -m helios_data.ibkr_ingest --intraday --symbols ISF SGLN
    python -m helios_data.ibkr_ingest --intraday --backfill-years 5   # extend 5-minute history back to 5 years

Stored in `bars` as source 'ibkr', symbol '<ticker>.L', timeframe '1d', ts = trading date (UTC midnight),
prices in pence (as IBKR's API returns them, matching the Dukascopy UK shares), volume = shares traded on the LSE.
The Helios ticker is kept even where the company has since changed its LSE symbol (e.g. RDSB.L holds
Shell's SHEL bars), so IBKR and Dukascopy rows pair up. Delisted companies have no IBKR data: they are
logged and skipped (their Dukascopy history remains).
IBKR allows 60 historical requests per 10 minutes, so a full first load (~110 shares) takes ~20 minutes.

Intraday (18 Nov 2026, short-term testing regime): 5-minute bars with real volume, regular hours only, for the
ETFs/ETCs and FTSE heavyweights in INTRADAY. Stored as timeframe '5m', ts = bar start in UTC, prices as quoted
(pence for GBP lines; CSPX in US dollars). The first run backfills month by month for INTRADAY_YEARS (about
24 requests per instrument, ~1 hour for the full set); later runs fetch only the days since the last bar.
"""

from __future__ import annotations

import argparse
import logging
import os
import time
from datetime import date, datetime, timezone

import pandas as pd

from . import db
from .config import UK_SHARES

log = logging.getLogger("ibkr")
SOURCE = "ibkr"
FIRST_RUN = "5 Y"
PAUSE_SECONDS = 11          # stays under IBKR's 60 requests per 10 minutes

# 5-minute set: Helios ticker -> currency of the LSE line (H-001 ETFs/ETCs + FTSE heavyweights)
INTRADAY = {
    "ISF": "GBP", "CSPX": "USD", "SGLN": "GBP",
    "AZN": "GBP", "RDSB": "GBP", "HSBA": "GBP", "ULVR": "GBP", "BP": "GBP", "RIO": "GBP", "GSK": "GBP",
    "REL": "GBP", "BATS": "GBP", "LSE": "GBP", "DGE": "GBP", "BARC": "GBP", "LLOY": "GBP", "NG": "GBP",
    "RR": "GBP",
}
ETFS = {"ISF", "CSPX", "SGLN"}   # London-listed funds sit on IBKR's LSEETF exchange, not LSE
INTRADAY_YEARS = 2
INTRADAY_CHUNK = "1 M"
RETRIES, RETRY_WAIT = 3, 120       # IBKR's servers go quiet in maintenance windows (about 23:00 and 04:00 UK)
STALL_MINUTES = 20                 # no bars at all for this long: stop the run instead of waiting forever
BACKOFF_AFTER = 3                  # this many empty answers in a row: pause RETRY_WAIT before the next request


class Stalled(SystemExit):
    """Raised when IBKR has returned no data for STALL_MINUTES. A SystemExit, so the per-share
    'one bad share should not stop the run' handlers do not swallow it."""

# Helios ticker -> current LSE symbol at IBKR, where they differ. Anything not listed is tried as-is,
# then with a trailing dot (IBKR writes two-letter LSE tickers such as BP and RR as "BP." and "RR.").
IBKR_SYMBOL = {
    "BT": "BT.A", "LSE": "LSEG", "RB": "RKT", "RBS": "NWG", "RDSB": "SHEL", "ADN": "ABDN",
}


def candidates(ticker: str) -> list[str]:
    first = IBKR_SYMBOL.get(ticker, ticker)
    out = [first]
    if not first.endswith(".") and "." not in first:
        out.append(first + ".")
    return out


def bars_to_frame(bars) -> pd.DataFrame:
    """ib_async BarData list -> bars frame (ts at UTC midnight of the trading date).

    The TWS API returns LSE share prices in pence already (checked on HSBA, 18 Nov 2026: about 1,350 in
    mid-November, the same scale as the Dukascopy UK shares), even though the contract currency reads GBP.
    So no conversion is applied.
    """
    rows = [{"ts": b.date, "open": b.open, "high": b.high, "low": b.low, "close": b.close,
             "volume": float(b.volume) if b.volume is not None else None} for b in bars or []]
    df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"])
    if df.empty:
        return df
    df["ts"] = pd.to_datetime([d.date() if isinstance(d, datetime) else d for d in df["ts"]]).tz_localize("UTC")
    df = df[df["volume"].fillna(0) >= 0]         # IBKR marks "no volume" as -1
    return df


def duration_for(last, today: date) -> str:
    """How much history to request: everything on the first run, otherwise the gap plus a margin."""
    if last is None:
        return FIRST_RUN
    days = (today - last.date()).days
    return f"{max(5, days + 5)} D"


def intraday_frame(bars) -> pd.DataFrame:
    """5-minute BarData -> bars frame, ts = bar start in UTC."""
    rows = [{"ts": b.date, "open": b.open, "high": b.high, "low": b.low, "close": b.close,
             "volume": float(b.volume) if b.volume is not None else None} for b in bars or []]
    df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"])
    if df.empty:
        return df
    df["ts"] = pd.to_datetime(df["ts"], utc=True)
    df = df[df["volume"].fillna(0) >= 0]
    return df


def intraday_duration(last, now: datetime) -> str:
    """Top-up request size: the days since the last stored bar plus a margin (IBKR caps 5-minute requests)."""
    days = (now - pd.Timestamp(last).tz_localize("UTC").to_pydatetime()).days if last is not None else 30
    return f"{min(30, max(2, days + 2))} D"


class IBKR:
    """Read-only connection to a running IB Gateway / TWS."""

    def __init__(self):
        try:
            from dotenv import load_dotenv
            load_dotenv()
        except ImportError:
            pass
        from ib_async import IB
        self.ib = IB()
        self.host = os.getenv("IBKR_HOST", "127.0.0.1")
        self.port = int(os.getenv("IBKR_PORT", "4001"))
        self.client_id = int(os.getenv("IBKR_CLIENT_ID", "17"))
        self.stall_seconds = STALL_MINUTES * 60
        self.last_data = time.monotonic()   # time of the last non-empty answer
        self.empty_run = 0                  # empty answers in a row

    def _track(self, bars, what: str):
        """Note whether IBKR answered; back off after a run of empty answers; stop if it has gone quiet."""
        if bars:
            self.last_data, self.empty_run = time.monotonic(), 0
            return bars
        self.empty_run += 1
        quiet = time.monotonic() - self.last_data
        if quiet >= self.stall_seconds:
            raise Stalled(
                f"STALLED: IBKR has returned no data for {quiet / 60:.0f} minutes (last request: {what}). "
                "Stopping so the scheduled tasks are not blocked. Everything loaded so far is saved; "
                "re-run the same command later and it carries on from where it stopped.")
        if self.empty_run >= BACKOFF_AFTER:
            log.warning("%d empty answers in a row - pausing %ds to let IBKR recover", self.empty_run, RETRY_WAIT)
            time.sleep(RETRY_WAIT)
            self.empty_run = 0
        return bars

    def connect(self):
        try:
            from ib_async.ib import StartupFetch
            # fetchFields=0: skip downloading positions, orders and trades at start-up (not needed, and slow
            # on a read-only Gateway connection)
            self.ib.connect(self.host, self.port, clientId=self.client_id, readonly=True, timeout=15,
                            fetchFields=StartupFetch(0))
        except Exception as exc:
            raise SystemExit(
                f"Could not reach IB Gateway at {self.host}:{self.port} with client ID {self.client_id} ({exc}).\n"
                "If another Helios run is still connected with the same client ID, stop it or wait for it.\n"
                "Start IB Gateway, log in, and check Configure > Settings > API > Settings: "
                "'Enable ActiveX and Socket Clients' ticked and the port matches IBKR_PORT.") from exc
        self.ib.reqMarketDataType(3)   # use free delayed data where there is no live subscription

    def disconnect(self):
        self.ib.disconnect()

    def contract(self, ticker: str, currency: str = "GBP"):
        from ib_async import Stock
        if ticker in ETFS:                         # funds: one exact lookup on LSEETF
            found = self.ib.qualifyContracts(Stock(ticker, "LSEETF", currency))
            return found[0] if found and getattr(found[0], "conId", 0) else None
        for sym in candidates(ticker):
            found = self.ib.qualifyContracts(Stock(sym, "LSE", currency))
            if found and getattr(found[0], "conId", 0):
                return found[0]
        return None

    def daily_bars(self, contract, duration: str):
        bars = self.ib.reqHistoricalData(contract, endDateTime="", durationStr=duration, barSizeSetting="1 day",
                                         whatToShow="TRADES", useRTH=True, formatDate=1, timeout=60)
        return self._track(bars, f"{contract.symbol} daily")


    def intraday_bars(self, contract, end, duration: str):
        """5-minute TRADES bars, regular hours, ending at `end` ('' = now); dates come back in UTC."""
        bars = self.ib.reqHistoricalData(contract, endDateTime=end, durationStr=duration, barSizeSetting="5 mins",
                                         whatToShow="TRADES", useRTH=True, formatDate=2, timeout=120)
        return self._track(bars, f"{contract.symbol} 5m to {end or 'now'}")


def update(con, client, ticker: str, today: date | None = None) -> int:
    today = today or datetime.now(timezone.utc).date()
    contract = client.contract(ticker)
    if contract is None:
        log.warning("%s: no LSE contract at IBKR (delisted or renamed) - skipped", ticker)
        return 0
    duration = duration_for(db.last_bar(con, SOURCE, f"{ticker}.L", "1d"), today)
    df = bars_to_frame(client.daily_bars(contract, duration))
    if not df.empty:
        df = df[df["ts"].dt.date < today]      # today's bar is incomplete
    return db.write_bars(con, SOURCE, f"{ticker}.L", "1d", df, note=f"{contract.symbol} conId {contract.conId}")


def update_intraday(con, client, ticker: str, currency: str = "GBP", now: datetime | None = None,
                    pause: float = PAUSE_SECONDS) -> int:
    """Backfill (first run) or top up the 5-minute bars of one instrument. Returns bars written."""
    now = now or datetime.now(timezone.utc)
    contract = client.contract(ticker, currency)
    if contract is None:
        log.warning("%s: no London %s contract at IBKR - skipped", ticker, currency)
        return 0
    symbol, note = f"{ticker}.L", f"{contract.symbol} conId {contract.conId} 5m"
    last = db.last_bar(con, SOURCE, symbol, "5m")
    if last is not None:
        df = intraday_frame(client.intraday_bars(contract, "", intraday_duration(last, now)))
        return db.write_bars(con, SOURCE, symbol, "5m", df, note=note)
    # first run: walk back a month at a time until INTRADAY_YEARS is covered or IBKR has nothing older
    start = pd.Timestamp(now) - pd.DateOffset(years=INTRADAY_YEARS)
    end, total = "", 0
    for _ in range(INTRADAY_YEARS * 12 + 2):
        df = intraday_frame(client.intraday_bars(contract, end, INTRADAY_CHUNK))
        if df.empty:
            break
        total += db.write_bars(con, SOURCE, symbol, "5m", df, note=note)
        earliest = df["ts"].min()
        if earliest <= start:
            break
        end = earliest.strftime("%Y%m%d-%H:%M:%S")        # UTC end time for the next (older) chunk
        if pause:
            time.sleep(pause)
    return total


def backfill_intraday(con, client, ticker: str, currency: str = "GBP", years: int = 5,
                      now: datetime | None = None, pause: float = PAUSE_SECONDS) -> int:
    """Extend existing 5-minute history further back, month by month, until `years` is covered or IBKR has
    nothing older. Safe to re-run: it starts from the earliest bar already stored."""
    now = now or datetime.now(timezone.utc)
    contract = client.contract(ticker, currency)
    if contract is None:
        log.warning("%s: no London %s contract at IBKR - skipped", ticker, currency)
        return 0
    symbol, note = f"{ticker}.L", f"{contract.symbol} conId {contract.conId} 5m backfill"
    first = con.execute("SELECT min(ts) FROM bars WHERE source = ? AND symbol = ? AND timeframe = '5m'",
                        [SOURCE, symbol]).fetchone()[0]
    if first is None:
        return update_intraday(con, client, ticker, currency, now=now, pause=pause)
    target = pd.Timestamp(now).tz_convert("UTC") - pd.DateOffset(years=years)
    end, total = pd.Timestamp(first).tz_localize("UTC"), 0
    while end > target:
        df = pd.DataFrame()
        for attempt in range(RETRIES + 1):          # an empty answer is often a timeout, not the end of data
            df = intraday_frame(client.intraday_bars(contract, end.strftime("%Y%m%d-%H:%M:%S"), INTRADAY_CHUNK))
            df = df[df["ts"] < end] if not df.empty else df
            if not df.empty:
                break
            if attempt < RETRIES and pause:
                log.info("%s 5m: no answer for the month before %s - retrying in %ds", ticker, end.date(),
                         RETRY_WAIT)
                time.sleep(RETRY_WAIT)
        if df.empty:
            log.info("%s 5m: IBKR has nothing before %s (after %d tries)", ticker, end.date(), RETRIES + 1)
            break
        total += db.write_bars(con, SOURCE, symbol, "5m", df, note=note)
        end = df["ts"].min()
        log.info("%s 5m: back to %s (%d bars so far)", ticker, pd.Timestamp(end).date(), total)
        if pause:
            time.sleep(pause)
    return total


def test(client) -> None:
    """Check access on one share without writing anything."""
    contract = client.contract("HSBA")
    if contract is None:
        raise SystemExit("HSBA not found at IBKR - the API connection works but contract lookup failed.")
    print(f"Contract: {contract.symbol} on {contract.exchange}, {contract.currency}, conId {contract.conId}")
    try:
        bars = client.daily_bars(contract, "1 M")
    except Exception as exc:
        raise SystemExit(f"Historical request failed: {exc}") from exc
    df = bars_to_frame(bars)
    if df.empty:
        raise SystemExit("No bars came back. Most likely the account has no LSE market data permission for the "
                         "API: check IBKR's message in the Gateway log, and consider the LSE Level I "
                         "subscription (about GBP 5 a month, non-professional).")
    print(f"OK: {len(df)} daily bars, {df['ts'].min().date()} to {df['ts'].max().date()}")
    print(df.tail(5).to_string(index=False))


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--symbols", nargs="*", default=list(UK_SHARES), help="Helios UK tickers (default: all UK_SHARES)")
    p.add_argument("--test", action="store_true", help="check access with one share; write nothing")
    p.add_argument("--intraday", action="store_true", help="5-minute bars for INTRADAY instead of daily bars")
    p.add_argument("--backfill-years", type=int, metavar="N",
                   help="with --intraday: extend the 5-minute history back to N years (e.g. 5)")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")

    client = IBKR()
    client.connect()
    try:
        if args.test:
            test(client)
            return
        con = db.connect()
        if args.intraday:
            wanted = args.symbols if args.symbols != list(UK_SHARES) else list(INTRADAY)
            for i, ticker in enumerate(wanted):
                if i:
                    time.sleep(PAUSE_SECONDS)
                try:
                    if args.backfill_years:
                        n = backfill_intraday(con, client, ticker, INTRADAY.get(ticker, "GBP"), args.backfill_years)
                    else:
                        n = update_intraday(con, client, ticker, INTRADAY.get(ticker, "GBP"))
                    log.info("%s 5m: %d bars written (%d/%d)", ticker, n, i + 1, len(wanted))
                except Exception as exc:
                    log.warning("%s 5m: failed (%s)", ticker, exc)
            log.info("DONE intraday")
            return
        done, skipped = 0, []
        for i, ticker in enumerate(args.symbols):
            if i:
                time.sleep(PAUSE_SECONDS)
            try:
                n = update(con, client, ticker)
            except Exception as exc:          # one bad share should not stop the run
                log.warning("%s: failed (%s)", ticker, exc)
                n = 0
            if n:
                done += 1
                log.info("%s: %d bars written (%d/%d)", ticker, n, i + 1, len(args.symbols))
            else:
                skipped.append(ticker)
        log.info("DONE: %d shares updated; nothing new or not found: %s", done, " ".join(skipped) or "none")
    finally:
        client.disconnect()


if __name__ == "__main__":
    main()
