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

from .config import DATABASE_NAME, INSTRUMENTS

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

-- Clean trading days built from hourly bars (no weekend stubs, except for 24/7 markets such as crypto).
-- Each market's trading day follows its own calendar (see the instruments table):
-- e.g. Gold and US indices end their day at 17:00 New York, the FTSE uses the London date.
CREATE OR REPLACE VIEW daily_session AS
WITH h AS (
  SELECT h.symbol, h.ts, h.open, h.high, h.low, h.close, h.volume, i.weekends,
    CAST(timezone(i.session_tz, h.ts AT TIME ZONE 'UTC') + to_hours(i.session_shift) AS DATE) AS session_date,
    -- the session in progress right now (its day is not finished, so it is left out)
    CAST(timezone(i.session_tz, current_timestamp) + to_hours(i.session_shift) AS DATE) AS current_session
  FROM hourly h JOIN instruments i USING (symbol)
)
SELECT symbol, session_date, arg_min(open, ts) AS open, max(high) AS high, min(low) AS low,
       arg_max(close, ts) AS close, sum(volume) AS volume, count(*) AS hours
FROM h
WHERE session_date < current_session   -- completed sessions only
GROUP BY symbol, session_date, weekends
HAVING weekends OR dayofweek(session_date) BETWEEN 1 AND 5;

-- What is in the warehouse
CREATE OR REPLACE VIEW coverage AS
SELECT source, symbol, timeframe, count(*) AS bars, min(ts) AS first_bar, max(ts) AS last_bar
FROM bars GROUP BY ALL ORDER BY ALL;
"""

# Research layers (market state model). Kept here so the warehouse can be rebuilt from code.
ANALYTICS = """
-- Layer 1 (long-term trend state) and layer 4 (Bollinger stretch), one row per market per day
CREATE OR REPLACE VIEW state_daily AS
WITH d AS (
  SELECT symbol, session_date, open, high, low, close,
         avg(close)  OVER w100 AS ma100,
         count(*)    OVER w100 AS n100,
         avg(close)  OVER w20  AS ma20,
         stddev_samp(close) OVER w20 AS sd20,
         ln(close / lag(close) OVER (PARTITION BY symbol ORDER BY session_date)) AS r
  FROM daily_session
  WINDOW w100 AS (PARTITION BY symbol ORDER BY session_date ROWS 99 PRECEDING),
         w20  AS (PARTITION BY symbol ORDER BY session_date ROWS 19 PRECEDING)
), s AS (
  SELECT *,
    (ma100 / lag(ma100, 20) OVER (PARTITION BY symbol ORDER BY session_date) - 1) * 100 AS ma100_slope_pct,
    stddev_samp(r) OVER (PARTITION BY symbol ORDER BY session_date ROWS 99 PRECEDING) * sqrt(20) * 100 AS vol20_pct
  FROM d
)
SELECT symbol, session_date, open, high, low, close, ma100, ma20, sd20,
  ma100_slope_pct, vol20_pct,
  ma100_slope_pct / vol20_pct AS trend_score,
  CASE WHEN ma100_slope_pct / vol20_pct >=  0.45 THEN  2
       WHEN ma100_slope_pct / vol20_pct >=  0.15 THEN  1
       WHEN ma100_slope_pct / vol20_pct >  -0.15 THEN  0
       WHEN ma100_slope_pct / vol20_pct >  -0.45 THEN -1
       ELSE -2 END AS trend_state,
  (close - ma20) / sd20 AS bb_z,
  r AS log_ret
FROM s
WHERE n100 = 100 AND ma100_slope_pct IS NOT NULL;

CREATE OR REPLACE VIEW state_monthly AS
SELECT symbol, date_trunc('month', session_date)::DATE AS month,
       arg_max(trend_score, session_date) AS trend_score,
       arg_max(trend_state, session_date) AS trend_state,
       arg_max(close, session_date) AS month_close
FROM state_daily GROUP BY ALL;

-- Layer 3: volatility ranked against the previous 252 trading days
CREATE OR REPLACE VIEW vol_state AS
WITH v AS (
  SELECT symbol, session_date, vol20_pct,
    list(vol20_pct) OVER (PARTITION BY symbol ORDER BY session_date ROWS 251 PRECEDING) AS hist
  FROM state_daily
)
SELECT symbol, session_date, vol20_pct,
  len(list_filter(hist, x -> x < vol20_pct))::DOUBLE / len(hist) AS vol_pctile,
  CASE WHEN len(hist) < 252 THEN NULL
       WHEN len(list_filter(hist, x -> x < vol20_pct))::DOUBLE / len(hist) < 1/3 THEN 'Calm'
       WHEN len(list_filter(hist, x -> x < vol20_pct))::DOUBLE / len(hist) < 2/3 THEN 'Normal'
       ELSE 'Volatile' END AS vol_state
FROM v;

-- H1a signals (uptrend + close at or below the lower Bollinger band) and the latest state per market.
-- Prices are rounded to suit each market's price level (FX needs 5 decimals, indices 1).
CREATE OR REPLACE VIEW h1_signals AS
WITH b AS (
  SELECT s.*, lag(s.close) OVER w AS pc FROM state_daily s WINDOW w AS (PARTITION BY symbol ORDER BY session_date)
), a AS (
  SELECT *, avg(greatest(high - low, abs(high - pc), abs(low - pc))) OVER (PARTITION BY symbol ORDER BY session_date ROWS 13 PRECEDING) AS atr14,
         CASE WHEN close < 10 THEN 5 WHEN close < 1000 THEN 3 ELSE 1 END AS dp
  FROM b
)
SELECT a.symbol, a.session_date, a.open, a.high, a.low, a.close, a.trend_state, round(a.bb_z, 2) AS bb_z,
  round(a.ma20 - 2 * a.sd20, a.dp) AS lower_band, round(a.ma20, a.dp) AS middle_band,
  round(a.atr14, a.dp) AS atr14, v.vol_state,
  a.trend_state > 0 AND a.bb_z <= -2 AS setup,
  CASE WHEN a.trend_state > 0 AND a.bb_z <= -2 THEN round(2 * a.atr14, a.dp) END AS stop_distance,
  a.session_date >= DATE '2026-11-10' AS forward_test
FROM a LEFT JOIN vol_state v USING (symbol, session_date);

CREATE OR REPLACE VIEW market_now AS
SELECT * FROM h1_signals QUALIFY row_number() OVER (PARTITION BY symbol ORDER BY session_date DESC) = 1;

-- Recent H1a trades on the alert markets, followed day by day with each market's exit rule:
--   B  = sell at the first close above the middle band, max 20 sessions
--   C2 = trailing stop 2 x ATR (at signal) below the highest high since entry, max 100 sessions
-- Both start with a stop at entry - 2 x ATR; a gap through the stop fills at the open.
-- 'skipped' = the setup came while an earlier trade in the same market was still open (one trade at a time).
CREATE OR REPLACE VIEW live_trades AS
WITH alert_markets(symbol, alert_exit) AS (VALUES
  ('UK100','B'), ('US500','C2'), ('NAS100','C2'), ('US30','C2'), ('GER40','C2'),
  ('XAUUSD','C2'), ('XAGUSD','C2'), ('COPPER','C2'), ('BRENT','C2'), ('XPTUSD','C2'),
  ('BTCUSD','C2'), ('ETHUSD','C2'),
  ('RUT','C2'), ('JPN225','C2'), ('HK50','C2')   -- added 11 Nov 2026 (H21)
), b AS (
  SELECT s.*, lag(s.close) OVER (PARTITION BY s.symbol ORDER BY s.session_date) AS pc
  FROM state_daily s JOIN alert_markets USING (symbol)
), d AS (
  SELECT symbol, session_date, open, high, low, close, ma20 AS middle_band,
         trend_state > 0 AND bb_z <= -2 AS setup,
         avg(greatest(high - low, abs(high - pc), abs(low - pc))) OVER (PARTITION BY symbol ORDER BY session_date ROWS 13 PRECEDING) AS atr14,
         row_number() OVER (PARTITION BY symbol ORDER BY session_date) AS i
  FROM b
), last_i AS (SELECT symbol, max(i) AS mi FROM d GROUP BY symbol),
s AS (   -- setups in roughly the last 7 months, with entry at the next session's open
  SELECT d.symbol, am.alert_exit AS exit_rule, d.session_date AS signal_date, d.i AS sig_i,
         n.session_date AS entry_date, n.open AS entry, d.atr14 AS atr
  FROM d JOIN last_i USING (symbol) JOIN alert_markets am USING (symbol)
  LEFT JOIN d n ON n.symbol = d.symbol AND n.i = d.i + 1
  WHERE d.setup AND d.i >= last_i.mi - 150
), p AS (  -- every session from entry onwards
  SELECT s.*, x.i - s.sig_i AS k, x.session_date, x.open, x.high, x.low, x.close, x.middle_band,
         max(x.high) OVER (PARTITION BY s.symbol, s.signal_date ORDER BY x.i ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING) AS hh_prev,
         max(x.high) OVER (PARTITION BY s.symbol, s.signal_date ORDER BY x.i) AS hh_now
  FROM s JOIN d x ON x.symbol = s.symbol AND x.i > s.sig_i
), q AS (
  SELECT *,
    CASE WHEN exit_rule = 'C2' THEN greatest(entry - 2 * atr, coalesce(hh_prev, entry) - 2 * atr) ELSE entry - 2 * atr END AS stop_today,
    CASE WHEN exit_rule = 'C2' THEN greatest(entry - 2 * atr, hh_now - 2 * atr) ELSE entry - 2 * atr END AS stop_next
  FROM p
), e AS (
  SELECT *,
    CASE WHEN low <= stop_today THEN 'stop'
         WHEN exit_rule = 'B' AND close > middle_band THEN 'middle band'
         WHEN k >= CASE WHEN exit_rule = 'B' THEN 20 ELSE 100 END THEN 'time limit' END AS how,
    CASE WHEN low <= stop_today THEN least(open, stop_today) ELSE close END AS px
  FROM q
), t AS (
  SELECT symbol, exit_rule, signal_date, entry_date, entry, atr,
    min(session_date) FILTER (WHERE how IS NOT NULL) AS exit_date,
    arg_min(how, session_date) FILTER (WHERE how IS NOT NULL) AS exit_how,
    arg_min(px, session_date) FILTER (WHERE how IS NOT NULL) AS exit_price,
    max(session_date) AS last_date, arg_max(close, session_date) AS last_close,
    arg_max(stop_next, session_date) AS stop_next, arg_max(middle_band, session_date) AS middle_band_now,
    max(k) AS sessions_held
  FROM e GROUP BY ALL
  UNION ALL   -- signal from the latest session: entry is at the next open
  SELECT symbol, exit_rule, signal_date, NULL, NULL, atr, NULL, NULL, NULL, NULL, NULL, NULL, NULL, 0 FROM s WHERE entry_date IS NULL
)
SELECT t1.symbol, t1.exit_rule, t1.signal_date, t1.entry_date, t1.entry, t1.atr, round(2 * t1.atr, 6) AS risk_1r,
  CASE WHEN t1.entry_date IS NULL THEN 'new - enter at next open'
       WHEN t1.exit_date IS NULL THEN 'open' ELSE 'closed' END AS status,
  t1.exit_date, t1.exit_how, t1.exit_price,
  round(((coalesce(t1.exit_price, t1.last_close)) - t1.entry) / (2 * t1.atr), 2) AS r_now,
  CASE WHEN t1.exit_date IS NULL THEN t1.stop_next END AS stop_for_next_session,
  CASE WHEN t1.exit_date IS NULL AND t1.exit_rule = 'B' THEN t1.middle_band_now END AS middle_band_now,
  t1.sessions_held,
  EXISTS (SELECT 1 FROM t t0 WHERE t0.symbol = t1.symbol AND t0.signal_date < t1.signal_date
          AND t0.entry_date IS NOT NULL AND coalesce(t0.exit_date, DATE '9999-12-31') > t1.signal_date) AS skipped
FROM t t1;
"""


def refresh_instruments(con) -> None:
    """Rewrite the instruments table from config.py so the warehouse matches the code."""
    rows = [
        (sym, v["name"], v["asset_class"], v["session_tz"], int(v["session_shift"]), bool(v.get("weekends", False)),
         v.get("spread"), v["dukascopy"], v.get("ig_epic"), v.get("sector"))
        for sym, v in INSTRUMENTS.items()
    ]
    df = pd.DataFrame(rows, columns=["symbol", "name", "asset_class", "session_tz", "session_shift", "weekends", "spread", "dukascopy", "ig_epic", "sector"])
    con.register("instr_df", df)
    con.execute("CREATE OR REPLACE TABLE instruments AS SELECT * FROM instr_df")
    con.unregister("instr_df")


def connect() -> duckdb.DuckDBPyConnection:
    if os.getenv("MOTHERDUCK_TOKEN"):
        con = duckdb.connect("md:")
        con.execute(f"CREATE DATABASE IF NOT EXISTS {DATABASE_NAME}")
        con.execute(f"USE {DATABASE_NAME}")
    else:
        path = Path(os.getenv("HELIOS_DB", "data/helios.duckdb"))
        path.parent.mkdir(parents=True, exist_ok=True)
        con = duckdb.connect(str(path))
    refresh_instruments(con)
    con.execute(SCHEMA)
    con.execute(ANALYTICS)
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
