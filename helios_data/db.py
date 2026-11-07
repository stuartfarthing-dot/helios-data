"""Connection and schema for the Helios warehouse.

- In the cloud (GitHub Actions, or anywhere with MOTHERDUCK_TOKEN set):
  connects to the MotherDuck database 'helios'.
- Otherwise: uses a local DuckDB file (HELIOS_DB, default data/helios.duckdb),
  which is handy for testing.
"""

from __future__ import annotations

import os
from pathlib import Path

import duckdb
import pandas as pd

from .config import DATABASE_NAME

SCHEMA = """
CREATE TABLE IF NOT EXISTS bars (
    source     VARCHAR   NOT NULL,   -- 'dukascopy' or 'ig'
    symbol     VARCHAR   NOT NULL,   -- Helios symbol, e.g. 'XAUUSD', 'UK100'
    timeframe  VARCHAR   NOT NULL,   -- '1m', '1h', '1d'
    ts         TIMESTAMP NOT NULL,   -- bar open time, UTC
    open       DOUBLE,
    high       DOUBLE,
    low        DOUBLE,
    close      DOUBLE,
    volume     DOUBLE
);

CREATE TABLE IF NOT EXISTS ingest_log (
    run_at     TIMESTAMP DEFAULT current_timestamp,
    source     VARCHAR,
    symbol     VARCHAR,
    timeframe  VARCHAR,
    rows       BIGINT,
    first_ts   TIMESTAMP,
    last_ts    TIMESTAMP,
    note       VARCHAR
);

-- Convenience views: Dukascopy bars by timeframe
CREATE OR REPLACE VIEW daily  AS SELECT symbol, ts, open, high, low, close, volume FROM bars WHERE source = 'dukascopy' AND timeframe = '1d';
CREATE OR REPLACE VIEW hourly AS SELECT symbol, ts, open, high, low, close, volume FROM bars WHERE source = 'dukascopy' AND timeframe = '1h';
CREATE OR REPLACE VIEW minute AS SELECT symbol, ts, open, high, low, close, volume FROM bars WHERE source = 'dukascopy' AND timeframe = '1m';
CREATE OR REPLACE VIEW ig_daily AS SELECT symbol, ts, open, high, low, close, volume FROM bars WHERE source = 'ig' AND timeframe = '1d';

-- What is in the warehouse
CREATE OR REPLACE VIEW coverage AS
SELECT source, symbol, timeframe, count(*) AS bars, min(ts) AS first_bar, max(ts) AS last_bar
FROM bars GROUP BY ALL ORDER BY ALL;
"""


def connect() -> duckdb.DuckDBPyConnection:
    if os.getenv("MOTHERDUCK_TOKEN"):
        con = duckdb.connect("md:")
        con.execute(f"CREATE DATABASE IF NOT EXISTS {DATABASE_NAME}")
        con.execute(f"USE {DATABASE_NAME}")
    else:
        path = Path(os.getenv("HELIOS_DB", "data/helios.duckdb"))
        path.parent.mkdir(parents=True, exist_ok=True)
        con = duckdb.connect(str(path))
    con.execute(SCHEMA)
    return con


def last_bar(con, source: str, symbol: str, timeframe: str):
    row = con.execute(
        "SELECT max(ts) FROM bars WHERE source=? AND symbol=? AND timeframe=?",
        [source, symbol, timeframe],
    ).fetchone()
    return row[0]


def write_bars(con, source: str, symbol: str, timeframe: str, df: pd.DataFrame, note: str = "") -> int:
    """Insert bars, replacing any existing bars in the same time range.

    df needs columns: ts (UTC), open, high, low, close, volume.
    """
    if df is None or df.empty:
        con.execute(
            "INSERT INTO ingest_log (source, symbol, timeframe, rows, note) VALUES (?,?,?,?,?)",
            [source, symbol, timeframe, 0, note or "no new data"],
        )
        return 0

    df = df.copy()
    ts = pd.to_datetime(df["ts"], utc=True).dt.tz_convert("UTC").dt.tz_localize(None)
    df["ts"] = ts
    df = df.drop_duplicates(subset="ts", keep="last").sort_values("ts")
    df["source"], df["symbol"], df["timeframe"] = source, symbol, timeframe
    df = df[["source", "symbol", "timeframe", "ts", "open", "high", "low", "close", "volume"]]
    first, last = df["ts"].iloc[0], df["ts"].iloc[-1]

    con.execute("BEGIN TRANSACTION")
    try:
        con.execute(
            "DELETE FROM bars WHERE source=? AND symbol=? AND timeframe=? AND ts BETWEEN ? AND ?",
            [source, symbol, timeframe, first, last],
        )
        con.register("new_bars", df)
        con.execute("INSERT INTO bars SELECT * FROM new_bars")
        con.unregister("new_bars")
        con.execute(
            "INSERT INTO ingest_log (source, symbol, timeframe, rows, first_ts, last_ts, note) VALUES (?,?,?,?,?,?,?)",
            [source, symbol, timeframe, len(df), first, last, note],
        )
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    return len(df)
