"""H-000: is IG a valid proxy for the FTSE 100? Compare price sources against the official close.

The reference is the official FTSE 100 daily close: Yahoo's ^FTSE by default, or FTSE Russell's
own daily price history file if you pass it with --ftse-file. Each candidate series is lined up
with the reference on the London trading date and measured on:

  - days matched, missing (in the reference but not the candidate) and extra
  - close difference in index points (median and 95th percentile)
  - daily return correlation and tracking error
  - share of days whose return is within +/- BAND of the reference return
  - the 10 worst days, for tagging by cause (roll, ex-dividend, auction, holiday, data error)

Candidates:
  ig_close          IG daily bar close (IX.D.FTSE.DAILY.IP, mid). IG's daily bar ends in the evening,
                    not at the 16:35 cash close, so this is expected to drift on busy US afternoons.
  dukascopy_1635    Dukascopy UK 100 at 16:35 London, from minute bars (just after the closing auction).
  dukascopy_session Dukascopy daily_session close (end of the London day).

Note on dates: IG stamps daily bars at midnight London time, which the warehouse stores in UTC.
During British Summer Time that is 23:00 the previous day, so ts::DATE gives the wrong day.
Always convert to London time first, as this module does.

    python -m helios_data.source_compare                      # refresh ^FTSE from Yahoo, compare, save
    python -m helios_data.source_compare --ftse-file "Price-History_20251231_1200.xlsx"
    python -m helios_data.source_compare --start 2023-01-01 --no-refresh --no-save
"""

from __future__ import annotations

import argparse
import logging
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from . import db

log = logging.getLogger("source_compare")

# Proposed acceptance criteria (H-000). Agree these before reading the results.
CORR_MIN = 0.99      # daily return correlation with the reference
BAND = 0.0010        # +/- 0.10% daily return difference ...
SHARE_MIN = 0.95     # ... on at least 95% of days
WORST_N = 10

YAHOO_FTSE = "^FTSE"
LONDON = "Europe/London"

RESULTS_DDL = """
CREATE TABLE IF NOT EXISTS source_compare (
    run_at        TIMESTAMP,
    reference     VARCHAR,
    candidate     VARCHAR,
    first_day     DATE,
    last_day      DATE,
    matched       BIGINT,
    missing       BIGINT,
    extra         BIGINT,
    med_diff_pts  DOUBLE,
    p95_abs_pts   DOUBLE,
    ret_corr      DOUBLE,
    te_daily_pct  DOUBLE,
    share_in_band DOUBLE,
    verdict       VARCHAR
);
CREATE TABLE IF NOT EXISTS source_compare_worst (
    run_at        TIMESTAMP,
    reference     VARCHAR,
    candidate     VARCHAR,
    day           DATE,
    ref_ret_pct   DOUBLE,
    cand_ret_pct  DOUBLE,
    diff_pct      DOUBLE,
    cause         VARCHAR    -- to be tagged by hand: roll, ex-dividend, auction, holiday, data error
);
"""


# ---------------------------------------------------------------- series

def _series(df: pd.DataFrame) -> pd.Series:
    """date/close frame -> close Series indexed by date, weekdays only, sorted."""
    if df.empty:
        return pd.Series(dtype=float)
    s = pd.Series(df["close"].values, index=pd.to_datetime(df["d"]).dt.date, dtype=float)
    s = s[[d.weekday() < 5 for d in s.index]]
    return s[~s.index.duplicated(keep="last")].sort_index().dropna()


def reference(con, name: str = "yahoo") -> pd.Series:
    if name == "yahoo":
        q = "SELECT ts::DATE AS d, close FROM bars WHERE source='yahoo' AND symbol=? AND timeframe='1d'"
        return _series(con.execute(q, [YAHOO_FTSE]).fetchdf())
    if name == "ftse_russell":
        q = "SELECT ts::DATE AS d, close FROM bars WHERE source='ftse_russell' AND symbol='UKX' AND timeframe='1d'"
        return _series(con.execute(q).fetchdf())
    raise ValueError(f"unknown reference {name!r}")


def ig_close(con, symbol: str = "UK100") -> pd.Series:
    q = f"""SELECT CAST(timezone('{LONDON}', ts AT TIME ZONE 'UTC') AS DATE) AS d, close
            FROM bars WHERE source='ig' AND timeframe='1d' AND symbol=?"""
    return _series(con.execute(q, [symbol]).fetchdf())


def dukascopy_at(con, hhmm: str = "16:35", symbol: str = "UK100") -> pd.Series:
    """Last Dukascopy price at or before hhmm London time each day (minute bars, ts = bar open)."""
    q = f"""
        WITH m AS (
            SELECT CAST(timezone('{LONDON}', ts AT TIME ZONE 'UTC') AS TIMESTAMP) AS lt, ts, close
            FROM bars WHERE source='dukascopy' AND timeframe='1m' AND symbol=?)
        SELECT lt::DATE AS d, arg_max(close, ts) AS close FROM m
        WHERE strftime(lt, '%H:%M') >= '08:00' AND strftime(lt, '%H:%M') < ?
        GROUP BY 1"""
    return _series(con.execute(q, [symbol, hhmm]).fetchdf())


def dukascopy_session(con, symbol: str = "UK100") -> pd.Series:
    q = "SELECT session_date AS d, close FROM daily_session WHERE symbol=?"
    return _series(con.execute(q, [symbol]).fetchdf())


CANDIDATES = {
    "ig_close": ig_close,
    "dukascopy_1635": dukascopy_at,
    "dukascopy_session": dukascopy_session,
}


# ---------------------------------------------------------------- comparison

def compare(ref: pd.Series, cand: pd.Series, start=None, end=None,
            band: float = BAND, corr_min: float = CORR_MIN, share_min: float = SHARE_MIN,
            worst_n: int = WORST_N) -> tuple[dict, pd.DataFrame]:
    """Measure a candidate close series against the reference over the candidate's own date span."""
    if start is not None:
        ref, cand = ref[ref.index >= start], cand[cand.index >= start]
    if end is not None:
        ref, cand = ref[ref.index <= end], cand[cand.index <= end]
    empty = {"first_day": None, "last_day": None, "matched": 0, "missing": 0, "extra": 0,
             "med_diff_pts": np.nan, "p95_abs_pts": np.nan, "ret_corr": np.nan,
             "te_daily_pct": np.nan, "share_in_band": np.nan, "verdict": "no overlap"}
    if ref.empty or cand.empty:
        return empty, pd.DataFrame(columns=["day", "ref_ret_pct", "cand_ret_pct", "diff_pct"])

    lo, hi = max(ref.index.min(), cand.index.min()), min(ref.index.max(), cand.index.max())
    r = ref[(ref.index >= lo) & (ref.index <= hi)]
    c = cand[(cand.index >= lo) & (cand.index <= hi)]
    common = r.index.intersection(c.index)
    if len(common) < 3:
        return empty, pd.DataFrame(columns=["day", "ref_ret_pct", "cand_ret_pct", "diff_pct"])

    j = pd.DataFrame({"ref": r[common], "cand": c[common]}).sort_index()
    j["ref_ret"] = j["ref"].pct_change()
    j["cand_ret"] = j["cand"].pct_change()
    j["diff"] = j["cand_ret"] - j["ref_ret"]
    rets = j.dropna()

    m = {
        "first_day": common.min(),
        "last_day": common.max(),
        "matched": int(len(common)),
        "missing": int(len(r.index.difference(c.index))),
        "extra": int(len(c.index.difference(r.index))),
        "med_diff_pts": float((j["cand"] - j["ref"]).median()),
        "p95_abs_pts": float((j["cand"] - j["ref"]).abs().quantile(0.95)),
        "ret_corr": float(rets["ref_ret"].corr(rets["cand_ret"])),
        "te_daily_pct": float(rets["diff"].std() * 100),
        "share_in_band": float((rets["diff"].abs() <= band).mean()),
    }
    passes = m["ret_corr"] >= corr_min and m["share_in_band"] >= share_min
    m["verdict"] = "pass" if passes else "fail"

    worst = rets.reindex(rets["diff"].abs().sort_values(ascending=False).index).head(worst_n)
    worst = pd.DataFrame({
        "day": worst.index,
        "ref_ret_pct": worst["ref_ret"].values * 100,
        "cand_ret_pct": worst["cand_ret"].values * 100,
        "diff_pct": worst["diff"].values * 100,
    })
    return m, worst


# ---------------------------------------------------------------- reference loading

def refresh_yahoo(con) -> int:
    from .events_ingest import update_yahoo_daily
    return update_yahoo_daily(con, [YAHOO_FTSE]).get(YAHOO_FTSE, 0)


def load_ftse_file(con, path: str) -> int:
    """Load FTSE Russell's daily price history workbook as source 'ftse_russell', symbol 'UKX'.

    The layout of FTSE Russell files varies, so this looks for the first column that parses as
    dates and the first numeric column after it. Check the printed sample before trusting it.
    """
    raw = pd.read_excel(path, header=None)
    date_col = close_col = None
    for col in raw.columns:
        parsed = pd.to_datetime(raw[col], errors="coerce", dayfirst=True)
        if parsed.notna().mean() > 0.5:
            date_col = col
            break
    if date_col is None:
        raise SystemExit(f"No date column found in {path}")
    for col in raw.columns[raw.columns.get_loc(date_col) + 1:]:
        if pd.to_numeric(raw[col], errors="coerce").notna().mean() > 0.5:
            close_col = col
            break
    if close_col is None:
        raise SystemExit(f"No price column found in {path}")
    d = pd.to_datetime(raw[date_col], errors="coerce", dayfirst=True)
    v = pd.to_numeric(raw[close_col], errors="coerce")
    df = pd.DataFrame({"d": d, "close": v}).dropna()
    print(f"FTSE file: dates from column {date_col}, closes from column {close_col}; sample:")
    print(df.head(3).to_string(index=False))
    out = pd.DataFrame({"ts": df["d"].dt.tz_localize("UTC"), "open": df["close"], "high": df["close"],
                        "low": df["close"], "close": df["close"], "volume": np.nan})
    return db.write_bars(con, "ftse_russell", "UKX", "1d", out, note=f"file {path}")


# ---------------------------------------------------------------- run and report

def run(con, ref_name: str = "yahoo", start=None, end=None, save: bool = True,
        candidates: dict | None = None) -> pd.DataFrame:
    ref = reference(con, ref_name)
    if ref.empty:
        raise SystemExit(f"No reference data for {ref_name}. Run without --no-refresh, or pass --ftse-file.")
    run_at = datetime.now(timezone.utc).replace(tzinfo=None)
    rows, worst_all = [], []
    for name, fn in (candidates or CANDIDATES).items():
        m, worst = compare(ref, fn(con), start=start, end=end)
        rows.append({"run_at": run_at, "reference": ref_name, "candidate": name, **m})
        worst.insert(0, "candidate", name)
        worst_all.append(worst)
    res = pd.DataFrame(rows)
    worst = pd.concat(worst_all, ignore_index=True) if worst_all else pd.DataFrame()

    if save:
        con.execute(RESULTS_DDL)
        con.register("res_df", res)
        con.execute("INSERT INTO source_compare SELECT * FROM res_df")
        con.unregister("res_df")
        if not worst.empty:
            w = worst.assign(run_at=run_at, reference=ref_name, cause=None)[
                ["run_at", "reference", "candidate", "day", "ref_ret_pct", "cand_ret_pct", "diff_pct", "cause"]]
            con.register("worst_df", w)
            con.execute("INSERT INTO source_compare_worst SELECT * FROM worst_df")
            con.unregister("worst_df")
    print(report(res, worst))
    return res


def report(res: pd.DataFrame, worst: pd.DataFrame) -> str:
    cols = ["candidate", "first_day", "last_day", "matched", "missing", "extra", "med_diff_pts",
            "p95_abs_pts", "ret_corr", "te_daily_pct", "share_in_band", "verdict"]
    lines = [f"H-000 source comparison (reference: {res['reference'].iloc[0]})",
             f"Pass = return correlation >= {CORR_MIN} and >= {SHARE_MIN:.0%} of days within "
             f"+/-{BAND:.2%}", "", res[cols].round(4).to_string(index=False)]
    if not worst.empty:
        lines += ["", f"Worst {WORST_N} days per candidate (return difference, %):",
                  worst.round(3).to_string(index=False)]
    return "\n".join(lines)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--ftse-file", help="FTSE Russell daily price history .xlsx; used as the reference")
    p.add_argument("--start", help="first day to compare (YYYY-MM-DD)")
    p.add_argument("--end", help="last day to compare (YYYY-MM-DD)")
    p.add_argument("--no-refresh", action="store_true", help="don't download ^FTSE from Yahoo first")
    p.add_argument("--no-save", action="store_true", help="print only; don't write results to the warehouse")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")

    con = db.connect()
    ref_name = "yahoo"
    if args.ftse_file:
        load_ftse_file(con, args.ftse_file)
        ref_name = "ftse_russell"
    elif not args.no_refresh:
        refresh_yahoo(con)
    start = pd.Timestamp(args.start).date() if args.start else None
    end = pd.Timestamp(args.end).date() if args.end else None
    run(con, ref_name, start=start, end=end, save=not args.no_save)


if __name__ == "__main__":
    main()
