"""Load Dukascopy history into the warehouse, then keep it up to date.

Each run picks up from the last bar already stored, so the first run
downloads the full history and later runs only fetch what's new.

    python -m helios_data.dukascopy_ingest                       # all active symbols, all timeframes
    python -m helios_data.dukascopy_ingest --symbols XAUUSD --timeframes 1d
    python -m helios_data.dukascopy_ingest --start 2024-01-01    # override start date
"""

from __future__ import annotations

import argparse
import logging
from datetime import datetime, timedelta, timezone

import pandas as pd

from . import db
from .config import ACTIVE, DUKASCOPY_TIMEFRAMES, INSTRUMENTS

log = logging.getLogger("dukascopy")

SOURCE = "dukascopy"

# Chunk size per request batch, so memory stays small on long histories.
CHUNK = {"1m": timedelta(days=30), "1h": timedelta(days=365), "1d": timedelta(days=3650)}


def _interval(timeframe: str):
    import dukascopy_python as dk
    return {"1m": dk.INTERVAL_MIN_1, "1h": dk.INTERVAL_HOUR_1, "1d": dk.INTERVAL_DAY_1}[timeframe]


def fetch_range(code: str, timeframe: str, start: datetime, end: datetime) -> pd.DataFrame:
    import dukascopy_python as dk
    df = dk.fetch(code, _interval(timeframe), dk.OFFER_SIDE_BID, start, end)
    if df is None or df.empty:
        return pd.DataFrame(columns=["ts", "open", "high", "low", "close", "volume"])
    df = df.reset_index().rename(columns={"timestamp": "ts"})
    return df[["ts", "open", "high", "low", "close", "volume"]]


def update(con, symbol: str, timeframe: str, start_override: str | None = None, now: datetime | None = None) -> int:
    inst = INSTRUMENTS[symbol]
    now = now or datetime.now(timezone.utc)
    last = db.last_bar(con, SOURCE, symbol, timeframe)
    if start_override:
        start = datetime.fromisoformat(start_override).replace(tzinfo=timezone.utc)
    elif last is not None:
        start = last.replace(tzinfo=timezone.utc)          # re-fetch the last bar: it may have been incomplete
    else:
        start = datetime.fromisoformat(inst["history_start"]).replace(tzinfo=timezone.utc)

    total = 0
    cursor = start
    while cursor < now:
        chunk_end = min(cursor + CHUNK[timeframe], now)
        df = fetch_range(inst["dukascopy"], timeframe, cursor, chunk_end)
        n = db.write_bars(con, SOURCE, symbol, timeframe, df, note=f"{cursor:%Y-%m-%d} to {chunk_end:%Y-%m-%d}")
        total += n
        log.info("%s %s %s -> %s: %d bars", symbol, timeframe, f"{cursor:%Y-%m-%d}", f"{chunk_end:%Y-%m-%d}", n)
        cursor = chunk_end
    return total


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--symbols", nargs="*", default=ACTIVE)
    p.add_argument("--timeframes", nargs="*", default=DUKASCOPY_TIMEFRAMES)
    p.add_argument("--start", help="YYYY-MM-DD: re-download from this date")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")

    con = db.connect()
    failures = []
    for symbol in args.symbols:
        wanted = INSTRUMENTS[symbol].get("timeframes", DUKASCOPY_TIMEFRAMES)
        for tf in [t for t in args.timeframes if t in wanted]:
            try:
                n = update(con, symbol, tf, args.start)
                log.info("DONE %s %s: %d bars written", symbol, tf, n)
            except Exception as e:  # keep going with the other markets
                log.exception("FAILED %s %s", symbol, tf)
                failures.append(f"{symbol} {tf}: {e}")
    if failures:
        raise SystemExit("Some downloads failed:\n" + "\n".join(failures))


if __name__ == "__main__":
    main()
