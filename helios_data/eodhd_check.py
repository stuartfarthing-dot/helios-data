"""One-off quality check of EODHD before subscribing (18 Nov 2026). Writes nothing to the warehouse.

Compares EODHD's free-plan data (1 year of end-of-day history) with what Helios already trusts:
  - LSE daily volume and close for HSBA, BP and Shell against IBKR's exchange data (source 'ibkr')
  - the FTSE 100 index close (FTSE.INDX) against the official close from Yahoo (^FTSE)
  - a sample of EODHD's ticker news with sentiment for HSBA

Uses about 10 of the free plan's 20 daily API calls. Needs EODHD_KEY in .env, and MOTHERDUCK_TOKEN
so the IBKR and Yahoo reference data can be read from the warehouse.

    python -m helios_data.eodhd_check            # first check (about 10 calls)
    python -m helios_data.eodhd_check --deep     # follow-up: index symbol and HSBC volume spikes (3 calls)
"""

from __future__ import annotations

import os
from datetime import date, timedelta

import numpy as np
import pandas as pd
import requests

from . import db

BASE = "https://eodhd.com/api"
# EODHD symbol -> Helios symbol of the IBKR reference series
SHARES = {"HSBA.LSE": "HSBA.L", "BP.LSE": "BP.L", "SHEL.LSE": "RDSB.L"}


def get(path: str, key: str, **params):
    r = requests.get(f"{BASE}/{path}", params={"api_token": key, "fmt": "json", **params}, timeout=30)
    if r.status_code != 200:
        raise SystemExit(f"EODHD {path} -> {r.status_code}: {r.text[:300]}")
    return r.json()


def eod(key: str, symbol: str, start: date) -> pd.DataFrame:
    rows = get(f"eod/{symbol}", key, **{"from": start.isoformat()})
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df["d"] = pd.to_datetime(df["date"]).dt.date
    return df.set_index("d")


def reference(con, sql: str, *args) -> pd.DataFrame:
    df = con.execute(sql, list(args)).fetchdf()
    df["d"] = pd.to_datetime(df["d"]).dt.date
    return df.set_index("d")


def compare_share(con, key, eodhd_sym, helios_sym, start) -> dict:
    e = eod(key, eodhd_sym, start)
    i = reference(con, "SELECT ts::DATE AS d, close, volume FROM bars WHERE source='ibkr' AND symbol=? "
                       "AND timeframe='1d' AND ts >= ?", helios_sym, start)
    j = e.join(i, how="inner", lsuffix="_e", rsuffix="_i")
    j = j[(j["volume_e"] > 0) & (j["volume_i"] > 0)]
    if j.empty:
        return {"share": eodhd_sym, "days_matched": 0}
    vol_ratio = j["volume_e"] / j["volume_i"]
    return {
        "share": eodhd_sym,
        "eodhd_days": len(e), "days_matched": len(j),
        "price_ratio_median": round(float((j["close_e"] / j["close_i"]).median()), 4),
        "price_days_off_1pct": int(((j["close_e"] / j["close_i"] - 1).abs() > 0.01).sum()),
        "volume_corr_log": round(float(np.log(j["volume_e"]).corr(np.log(j["volume_i"]))), 3),
        "volume_ratio_median": round(float(vol_ratio.median()), 3),
        "volume_days_off_10pct": int(((vol_ratio - 1).abs() > 0.10).sum()),
    }


def deep(con, key) -> None:
    """Follow-up: which index symbol works, and do EODHD's volume spikes fall on the same days as IBKR's?"""
    start = date.today() - timedelta(days=360)
    print("A. FTSE 100 index symbols")
    for sym in ("FTSE.INDX", "UKX.INDX"):
        e = eod(key, sym, start)
        if e.empty:
            print(f"   {sym}: no data")
        else:
            print(f"   {sym}: {len(e)} days, {e.index.min()} {e['close'].iloc[0]:.2f} to {e.index.max()} {e['close'].iloc[-1]:.2f}")

    print("\nB. HSBC volume: EODHD (all venues) against IBKR (LSE)")
    e = eod(key, "HSBA.LSE", start)["volume"].rename("eodhd")
    i = reference(con, "SELECT ts::DATE AS d, volume FROM bars WHERE source='ibkr' AND symbol='HSBA.L' "
                       "AND timeframe='1d' AND ts >= ?", start)["volume"].rename("ibkr")
    j = pd.concat([e, i], axis=1).dropna()
    j = j[(j > 0).all(axis=1)].sort_index()
    rel = j / j.rolling(20, min_periods=10).median().shift(1)          # volume against its recent normal
    lj = np.log(j)
    print(f"   days {len(j)}; log-volume correlation: same day {lj['eodhd'].corr(lj['ibkr']):.2f}, "
          f"EODHD one day later {lj['eodhd'].shift(-1).corr(lj['ibkr']):.2f}, "
          f"EODHD one day earlier {lj['eodhd'].shift(1).corr(lj['ibkr']):.2f}")
    print(f"   spike correlation (volume / 20-day median): {rel['eodhd'].corr(rel['ibkr']):.2f}")
    top_i = set(rel['ibkr'].nlargest(10).index)
    top_e = set(rel['eodhd'].nlargest(10).index)
    print(f"   IBKR's 10 biggest spike days also in EODHD's top 10: {len(top_i & top_e)}; "
          f"in EODHD's top 25: {len(top_i & set(rel['eodhd'].nlargest(25).index))}")
    show = rel.loc[sorted(top_i | top_e)].round(2)
    show["in_top10"] = ["both" if d in top_i and d in top_e else ("IBKR" if d in top_i else "EODHD") for d in show.index]
    print("   Spike days (volume as a multiple of its 20-day median):")
    print(show.to_string())


def main():
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass
    key = os.getenv("EODHD_KEY")
    if not key:
        raise SystemExit("EODHD_KEY is not set: add EODHD_KEY=<your token> to .env")
    con = db.connect()
    import sys
    if "--deep" in sys.argv[1:]:
        deep(con, key)
        print("\nDone. Paste this output back into the chat.")
        return
    start = date.today() - timedelta(days=360)

    print("1. LSE shares: EODHD against IBKR exchange data (1 year)")
    rows = [compare_share(con, key, e, h, start) for e, h in SHARES.items()]
    print(pd.DataFrame(rows).to_string(index=False))

    print("\n2. FTSE 100 index: EODHD FTSE.INDX against the official close (Yahoo ^FTSE)")
    e = eod(key, "FTSE.INDX", start)
    y = reference(con, "SELECT ts::DATE AS d, close FROM bars WHERE source='yahoo' AND symbol='^FTSE' AND ts >= ?", start)
    j = e.join(y, how="inner", lsuffix="_e", rsuffix="_y")
    if j.empty:
        print("   no overlapping days")
    else:
        diff = (j["close_e"] - j["close_y"]).abs()
        print(f"   days matched {len(j)} (EODHD {len(e)}, Yahoo {len(y)}); median gap {diff.median():.2f} pts; "
              f"days more than 1 pt apart: {(diff > 1).sum()}")

    print("\n3. News with sentiment: HSBA.LSE, latest 10 articles")
    news = get("news", key, s="HSBA.LSE", limit=10)
    if not news:
        print("   no articles returned")
    for a in news:
        s = a.get("sentiment") or {}
        print(f"   {a.get('date', '')[:10]}  polarity {s.get('polarity', 'n/a'):>6}  {a.get('title', '')[:90]}")
    print("\nDone. Paste this output back into the chat.")


if __name__ == "__main__":
    main()
