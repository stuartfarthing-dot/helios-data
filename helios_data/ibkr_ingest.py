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

Stored in `bars` as source 'ibkr', symbol '<ticker>.L', timeframe '1d', ts = trading date (UTC midnight),
prices in pence (as IBKR's API returns them, matching the Dukascopy UK shares), volume = shares traded on the LSE.
The Helios ticker is kept even where the company has since changed its LSE symbol (e.g. RDSB.L holds
Shell's SHEL bars), so IBKR and Dukascopy rows pair up. Delisted companies have no IBKR data: they are
logged and skipped (their Dukascopy history remains).
IBKR allows 60 historical requests per 10 minutes, so a full first load (~110 shares) takes ~20 minutes.
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

    def connect(self):
        try:
            from ib_async.ib import StartupFetch
            # fetchFields=0: skip downloading positions, orders and trades at start-up (not needed, and slow
            # on a read-only Gateway connection)
            self.ib.connect(self.host, self.port, clientId=self.client_id, readonly=True, timeout=15,
                            fetchFields=StartupFetch(0))
        except Exception as exc:
            raise SystemExit(
                f"Could not reach IB Gateway at {self.host}:{self.port} ({exc}).\n"
                "Start IB Gateway, log in, and check Configure > Settings > API > Settings: "
                "'Enable ActiveX and Socket Clients' ticked and the port matches IBKR_PORT.") from exc
        self.ib.reqMarketDataType(3)   # use free delayed data where there is no live subscription

    def disconnect(self):
        self.ib.disconnect()

    def contract(self, ticker: str):
        from ib_async import Stock
        for sym in candidates(ticker):
            found = self.ib.qualifyContracts(Stock(sym, "LSE", "GBP"))
            if found and getattr(found[0], "conId", 0):
                return found[0]
        return None

    def daily_bars(self, contract, duration: str):
        return self.ib.reqHistoricalData(contract, endDateTime="", durationStr=duration, barSizeSetting="1 day",
                                         whatToShow="TRADES", useRTH=True, formatDate=1, timeout=60)


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
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")

    client = IBKR()
    client.connect()
    try:
        if args.test:
            test(client)
            return
        con = db.connect()
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
