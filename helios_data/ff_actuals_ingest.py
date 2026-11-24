"""Economic-calendar ACTUAL figures from Forex Factory, via a pay-per-event Apify actor (board Q14.10 / Q14.29).

The free weekly feed (events_ingest, source 'ff_weekly') has forecasts but no actual figures, and the
Hugging Face archive (source 'ff_archive') stops on 7 Apr 2025. This fills the gap and keeps it current,
so the published-data "surprise" layer (actual against forecast) can join the economic-forces pull.

Actor: romajure/economic-calendar (Apify Store), Forex Factory source. Pay per event: about $0.002 per
event pushed, filtered by currency and impact BEFORE charging. Approved by Stuart on 24 Nov 2026 with a
budget of about $40 for the back-fill. Every run passes a hard spending cap (maxTotalChargeUsd).

Rows go into econ_calendar with source = 'ff_actuals'. Re-running a window replaces that window's rows.

Needs APIFY_TOKEN (GitHub secret, or .env locally). Never print the token.

    python -m helios_data.ff_actuals_ingest --test        # one week (9-15 Nov 2026): count and cost only
    python -m helios_data.ff_actuals_ingest --backfill    # 7 Apr 2025 to today, in 62-day windows
    python -m helios_data.ff_actuals_ingest               # nightly: the last 14 days
"""
from __future__ import annotations

import argparse
import logging
import os
from datetime import date, timedelta

import pandas as pd
import requests

log = logging.getLogger("ff_actuals")

ACTOR = "romajure~economic-calendar"
RUN_URL = f"https://api.apify.com/v2/acts/{ACTOR}/run-sync-get-dataset-items"
CURRENCIES = ["USD", "GBP", "EUR", "JPY", "AUD", "CNY"]      # the regions in the surprise layer
IMPACTS = ["high", "medium"]
PRICE_PER_EVENT = 0.002                                      # USD, from the actor's page (24 Nov 2026)
BACKFILL_START = date(2025, 4, 7)
WINDOW_DAYS = 62                                             # Forex Factory's largest page range
COLUMNS = ["event_date", "event_time_utc", "currency", "impact", "event", "actual", "forecast", "previous", "source"]


def fetch_window(token: str, start: date, end: date, cap_usd: float) -> list[dict]:
    body = {"sources": ["forexfactory"], "dateFrom": start.isoformat(), "dateTo": end.isoformat(),
            "currencies": CURRENCIES, "impact": IMPACTS, "includeDetails": False}
    r = requests.post(RUN_URL, params={"token": token, "maxTotalChargeUsd": cap_usd, "timeout": 290},
                      json=body, timeout=320)
    r.raise_for_status()
    return r.json()


def parse(items: list[dict]) -> pd.DataFrame:
    rows = []
    for e in items:
        if e.get("status") in ("unavailable", "empty") or not e.get("title"):
            continue                                    # status records from a failed source
        ts = pd.Timestamp(e["datetimeUtc"]) if e.get("datetimeUtc") else None
        if ts is not None and ts.tzinfo:
            ts = ts.tz_convert("UTC").tz_localize(None)
        day = ts.date() if ts is not None else pd.Timestamp(e["date"]).date()
        rows.append({"event_date": day, "event_time_utc": ts, "currency": e.get("currency"),
                     "impact": (e.get("impact") or "").lower(), "event": e.get("title"),
                     "actual": e.get("actual") or None, "forecast": e.get("forecast") or None,
                     "previous": e.get("previous") or None, "source": "ff_actuals"})
    return pd.DataFrame(rows, columns=COLUMNS)


def write(con, df: pd.DataFrame, start: date, end: date) -> int:
    con.execute("""CREATE TABLE IF NOT EXISTS econ_calendar (
      event_date DATE, event_time_utc TIMESTAMP, currency VARCHAR, impact VARCHAR, event VARCHAR,
      actual VARCHAR, forecast VARCHAR, previous VARCHAR, source VARCHAR)""")
    con.execute("DELETE FROM econ_calendar WHERE source = 'ff_actuals' AND event_date BETWEEN ? AND ?", [start, end])
    if df.empty:
        return 0
    con.register("ffa_df", df)
    con.execute("INSERT INTO econ_calendar SELECT * FROM ffa_df")
    con.unregister("ffa_df")
    return len(df)


def windows(start: date, end: date, days: int = WINDOW_DAYS):
    d = start
    while d <= end:
        e = min(d + timedelta(days=days - 1), end)
        yield d, e
        d = e + timedelta(days=1)


def run(con, token: str, start: date, end: date, cap_usd: float, fetch=fetch_window) -> dict:
    total, spent = 0, 0.0
    for a, b in windows(start, end):
        left = cap_usd - spent
        if left < 0.05:
            log.warning("spending cap reached before %s; stopping", a)
            break
        items = fetch(token, a, b, round(left, 2))
        df = parse(items)
        n = write(con, df, a, b)
        total += n
        spent += n * PRICE_PER_EVENT
        log.info("ff_actuals %s to %s: %d events (about $%.2f so far)", a, b, n, spent)
    return {"events": total, "approx_cost_usd": round(spent, 2)}


def main(argv=None):
    from . import db
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = p.add_mutually_exclusive_group()
    g.add_argument("--test", action="store_true", help="one week only (9-15 Nov 2026), to measure count and cost")
    g.add_argument("--backfill", action="store_true", help="7 Apr 2025 to today")
    p.add_argument("--cap", type=float, default=None, help="hard spending cap in USD for this run")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass
    token = os.getenv("APIFY_TOKEN")
    if not token:
        raise SystemExit("APIFY_TOKEN is not set (GitHub secret, or .env locally).")
    today = date.today()
    if args.test:
        start, end, cap = date(2026, 11, 9), date(2026, 11, 15), args.cap or 1.0
    elif args.backfill:
        start, end, cap = BACKFILL_START, today, args.cap or 40.0
    else:
        start, end, cap = today - timedelta(days=14), today, args.cap or 1.0
    con = db.connect()
    res = run(con, token, start, end, cap)
    con.close()
    print(f"ff_actuals {start} to {end}: {res['events']} events, about ${res['approx_cost_usd']:.2f}")
    if args.test and res["events"]:
        weeks = (today - BACKFILL_START).days / 7
        print(f"Estimated back-fill: {res['events']} events/week x {weeks:.0f} weeks = about "
              f"${res['events'] * weeks * PRICE_PER_EVENT:.2f}")


if __name__ == "__main__":
    main()
