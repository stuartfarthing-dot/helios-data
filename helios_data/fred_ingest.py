"""Daily macro series from FRED (St. Louis Fed) into the warehouse table fred_series (Q14.9).

Adds the credit and funding-stress series the economic-forces work was missing (it used the VIX as a
stand-in for credit and liquidity), plus European credit and US rates for cross-checks. FRED's UK, euro-area
and Japanese government yields are monthly only; they are loaded too, for the slow layer.

Needs FRED_API_KEY (free, fred.stlouisfed.org): in .env for local runs, and as a GitHub secret for the
nightly job. Safe to re-run: each run reloads the last 30 days and anything newer.

    python -m helios_data.fred_ingest                 # update all series
    python -m helios_data.fred_ingest --full          # reload full history
"""
from __future__ import annotations

import argparse
import logging
import os
import time
from datetime import date, timedelta

import pandas as pd
import requests

log = logging.getLogger("fred")
URL = "https://api.stlouisfed.org/fred/series/observations"

# series id -> (description, frequency)
SERIES = {
    "BAMLH0A0HYM2": ("US high-yield corporate spread over Treasuries (OAS), %", "daily"),
    "BAMLC0A0CM": ("US investment-grade corporate spread (OAS), %", "daily"),
    "BAMLHE00EHYIOAS": ("Euro high-yield corporate spread (OAS), %", "daily"),
    "BAMLC0A4CBBB": ("US BBB corporate spread (OAS), %", "daily"),
    "T10Y2Y": ("US 10-year minus 2-year Treasury yield, %", "daily"),
    "T10Y3M": ("US 10-year minus 3-month Treasury yield, %", "daily"),
    "DGS10": ("US 10-year Treasury yield, %", "daily"),
    "DFII10": ("US 10-year real (TIPS) yield, %", "daily"),
    "T10YIE": ("US 10-year breakeven inflation, %", "daily"),
    "DTWEXBGS": ("US dollar broad trade-weighted index", "daily"),
    "STLFSI4": ("St. Louis Fed financial stress index", "weekly"),
    "NFCI": ("Chicago Fed national financial conditions index", "weekly"),
    "IRLTLT01GBM156N": ("UK 10-year government bond yield, %", "monthly"),
    "IRLTLT01DEM156N": ("Germany 10-year government bond yield, %", "monthly"),
    "IRLTLT01JPM156N": ("Japan 10-year government bond yield, %", "monthly"),
    # Short-term interest rates by currency (28 Nov 2026, carry test H45): OECD 3-month interbank rates and
    # call-money / overnight rates, monthly. Some 3-month series stop when a LIBOR rate was retired, so both
    # families are loaded and the research uses whichever is available for each month.
    "IR3TIB01USM156N": ("United States 3-month interbank rate, %", "monthly"),
    "IRSTCI01USM156N": ("United States call money / overnight rate, %", "monthly"),
    "IR3TIB01EZM156N": ("euro area 3-month interbank rate, %", "monthly"),
    "IRSTCI01EZM156N": ("euro area call money / overnight rate, %", "monthly"),
    "IR3TIB01GBM156N": ("United Kingdom 3-month interbank rate, %", "monthly"),
    "IRSTCI01GBM156N": ("United Kingdom call money / overnight rate, %", "monthly"),
    "IR3TIB01JPM156N": ("Japan 3-month interbank rate, %", "monthly"),
    "IRSTCI01JPM156N": ("Japan call money / overnight rate, %", "monthly"),
    "IR3TIB01AUM156N": ("Australia 3-month interbank rate, %", "monthly"),
    "IRSTCI01AUM156N": ("Australia call money / overnight rate, %", "monthly"),
    "IR3TIB01NZM156N": ("New Zealand 3-month interbank rate, %", "monthly"),
    "IRSTCI01NZM156N": ("New Zealand call money / overnight rate, %", "monthly"),
    "IR3TIB01CAM156N": ("Canada 3-month interbank rate, %", "monthly"),
    "IRSTCI01CAM156N": ("Canada call money / overnight rate, %", "monthly"),
    "IR3TIB01CHM156N": ("Switzerland 3-month interbank rate, %", "monthly"),
    "IRSTCI01CHM156N": ("Switzerland call money / overnight rate, %", "monthly"),
    "IR3TIB01NOM156N": ("Norway 3-month interbank rate, %", "monthly"),
    "IRSTCI01NOM156N": ("Norway call money / overnight rate, %", "monthly"),
    "IR3TIB01SEM156N": ("Sweden 3-month interbank rate, %", "monthly"),
    "IRSTCI01SEM156N": ("Sweden call money / overnight rate, %", "monthly"),
    "IR3TIB01MXM156N": ("Mexico 3-month interbank rate, %", "monthly"),
    "IRSTCI01MXM156N": ("Mexico call money / overnight rate, %", "monthly"),
    "IR3TIB01ZAM156N": ("South Africa 3-month interbank rate, %", "monthly"),
    "IRSTCI01ZAM156N": ("South Africa call money / overnight rate, %", "monthly"),
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS fred_series (
  series_id VARCHAR, d DATE, value DOUBLE, loaded_at TIMESTAMP DEFAULT current_timestamp,
  PRIMARY KEY (series_id, d)
);
"""


def fetch(series_id: str, key: str, start: date | None) -> pd.DataFrame:
    params = {"series_id": series_id, "api_key": key, "file_type": "json"}
    if start:
        params["observation_start"] = start.isoformat()
    r = requests.get(URL, params=params, timeout=60)
    r.raise_for_status()
    obs = r.json().get("observations", [])
    df = pd.DataFrame(obs, columns=["date", "value"]) if obs else pd.DataFrame(columns=["date", "value"])
    df["value"] = pd.to_numeric(df["value"], errors="coerce")       # FRED marks missing days with "."
    df = df.dropna(subset=["value"])
    return pd.DataFrame({"d": pd.to_datetime(df["date"]).dt.date, "value": df["value"].astype(float)})


def write(con, series_id: str, df: pd.DataFrame) -> int:
    if df.empty:
        return 0
    con.register("fred_new", df)
    con.execute("DELETE FROM fred_series WHERE series_id = ? AND d >= (SELECT min(d) FROM fred_new)", [series_id])
    con.execute("INSERT INTO fred_series (series_id, d, value) SELECT ?, d, value FROM fred_new", [series_id])
    con.unregister("fred_new")
    return len(df)


def update(con, key: str, full: bool = False, series=None, fetcher=fetch, pause: float = 0.6) -> dict:
    con.execute(SCHEMA)
    out = {}
    for sid in series or SERIES:
        last = None if full else con.execute("SELECT max(d) FROM fred_series WHERE series_id = ?", [sid]).fetchone()[0]
        start = (last - timedelta(days=30)) if last else None
        try:
            out[sid] = write(con, sid, fetcher(sid, key, start))
        except Exception as exc:                 # one failing series should not stop the rest
            log.warning("%s failed: %s", sid, exc)
            out[sid] = None
        if pause:
            time.sleep(pause)                    # FRED allows 120 requests a minute
    log.info("fred: %s", out)
    return out


def main(argv=None):
    from . import db
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--full", action="store_true", help="reload full history")
    p.add_argument("--series", nargs="*", help="FRED series ids (default: all)")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass
    key = os.getenv("FRED_API_KEY")
    if not key:
        raise SystemExit("FRED_API_KEY is not set (add it to .env locally, or as a GitHub secret).")
    con = db.connect()
    res = update(con, key, full=args.full, series=args.series)
    con.close()
    failed = [s for s, n in res.items() if n is None]
    if failed and len(failed) == len(res):
        raise SystemExit(f"every FRED series failed: {failed}")


if __name__ == "__main__":
    main()
