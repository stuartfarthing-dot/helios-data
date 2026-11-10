"""Copy Stuart's own IG trades into the warehouse, so live fills can be compared with the backtests.

Read-only: this only *reads* the account's history; it never places, changes or closes anything.

Two IG history feeds are stored:
  ig_activity      every deal event (open, close, stop/limit change, rejection) with its level, size,
                   direction and stop - this is where entry and exit fills come from
  ig_transactions  every closed trade with its opening and closing level and profit/loss in £

Runs at the end of the nightly IG step (see ig_ingest.main) for the last few days; rows for the
fetched window are replaced, so re-running is harmless. To back-fill further:

    python -m helios_data.ig_trades --days 30
"""

from __future__ import annotations

import argparse
import json
import logging
import re
from datetime import datetime, timedelta, timezone

import pandas as pd

log = logging.getLogger("ig_trades")

SCHEMA = """
CREATE TABLE IF NOT EXISTS ig_activity (
  ts_utc TIMESTAMP, epic VARCHAR, market VARCHAR, deal_id VARCHAR, deal_ref VARCHAR,
  type VARCHAR, status VARCHAR, description VARCHAR, channel VARCHAR,
  direction VARCHAR, size DOUBLE, level DOUBLE, stop_level DOUBLE, limit_level DOUBLE,
  actions VARCHAR, raw JSON, loaded_at TIMESTAMP DEFAULT current_timestamp
);
CREATE TABLE IF NOT EXISTS ig_transactions (
  ts_utc TIMESTAMP, open_ts_utc TIMESTAMP, market VARCHAR, reference VARCHAR, transaction_type VARCHAR,
  size DOUBLE, open_level DOUBLE, close_level DOUBLE, pnl_gbp DOUBLE, currency VARCHAR,
  raw JSON, loaded_at TIMESTAMP DEFAULT current_timestamp
);
"""


def _num(x):
    """IG sends numbers as numbers or as text such as '+0.5', '£2.67', '-£1,060.00' or '-'."""
    if x is None or isinstance(x, (int, float)):
        return x
    s = re.sub(r"[^0-9.\-]", "", str(x))
    try:
        return float(s) if s not in ("", "-", ".") else None
    except ValueError:
        return None


def parse_activity(items: list[dict]) -> pd.DataFrame:
    rows = []
    for a in items:
        d = a.get("details") or {}
        rows.append({
            "ts_utc": a.get("date"), "epic": a.get("epic"), "market": d.get("marketName"),
            "deal_id": a.get("dealId"), "deal_ref": d.get("dealReference"),
            "type": a.get("type"), "status": a.get("status"), "description": a.get("description"),
            "channel": a.get("channel"), "direction": d.get("direction"), "size": _num(d.get("size")),
            "level": _num(d.get("level")), "stop_level": _num(d.get("stopLevel")),
            "limit_level": _num(d.get("limitLevel")),
            "actions": json.dumps(d.get("actions") or []), "raw": json.dumps(a),
        })
    df = pd.DataFrame(rows, columns=["ts_utc", "epic", "market", "deal_id", "deal_ref", "type", "status",
                                     "description", "channel", "direction", "size", "level", "stop_level",
                                     "limit_level", "actions", "raw"])
    if not df.empty:
        # activity dates are UTC without a zone marker
        df["ts_utc"] = pd.to_datetime(df["ts_utc"], utc=True).dt.tz_localize(None)
    return df


def parse_transactions(items: list[dict]) -> pd.DataFrame:
    rows = [{
        "ts_utc": t.get("dateUtc"), "open_ts_utc": t.get("openDateUtc"), "market": t.get("instrumentName"),
        "reference": t.get("reference"), "transaction_type": t.get("transactionType"),
        "size": _num(t.get("size")), "open_level": _num(t.get("openLevel")),
        "close_level": _num(t.get("closeLevel")), "pnl_gbp": _num(t.get("profitAndLoss")),
        "currency": t.get("currency"), "raw": json.dumps(t),
    } for t in items]
    df = pd.DataFrame(rows, columns=["ts_utc", "open_ts_utc", "market", "reference", "transaction_type", "size",
                                     "open_level", "close_level", "pnl_gbp", "currency", "raw"])
    for c in ("ts_utc", "open_ts_utc"):
        if not df.empty:
            df[c] = pd.to_datetime(df[c], utc=True, errors="coerce").dt.tz_localize(None)
    return df


def fetch_activity(ig, start: datetime, end: datetime) -> list[dict]:
    out, params = [], {"from": start.strftime("%Y-%m-%dT%H:%M:%S"), "to": end.strftime("%Y-%m-%dT%H:%M:%S"),
                       "detailed": "true", "pageSize": 50}
    path = "/history/activity"
    for _ in range(100):  # follow IG's paging links
        r = ig._req("GET", path, "3", params=params).json()
        out += r.get("activities", [])
        nxt = (r.get("metadata", {}).get("paging") or {}).get("next")
        if not nxt:
            break
        path, params = nxt.split("?")[0].replace("/gateway/deal", ""), dict(
            p.split("=", 1) for p in nxt.split("?", 1)[1].split("&"))
    return out


def fetch_transactions(ig, start: datetime, end: datetime) -> list[dict]:
    r = ig._req("GET", "/history/transactions", "2", params={
        "type": "ALL", "from": start.strftime("%Y-%m-%dT%H:%M:%S"), "to": end.strftime("%Y-%m-%dT%H:%M:%S"),
        "pageSize": 0}).json()
    return r.get("transactions", [])


def _replace(con, table: str, df: pd.DataFrame, start: datetime, end: datetime) -> int:
    con.execute(f"DELETE FROM {table} WHERE ts_utc >= ? AND ts_utc <= ?",
                [start.replace(tzinfo=None), end.replace(tzinfo=None)])
    if df.empty:
        return 0
    con.register("new_rows", df)
    cols = ", ".join(df.columns)
    con.execute(f"INSERT INTO {table} ({cols}) SELECT {cols} FROM new_rows")
    con.unregister("new_rows")
    return len(df)


def update_trades(con, ig, days: int = 7) -> tuple[int, int]:
    con.execute(SCHEMA)
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    n_act = _replace(con, "ig_activity", parse_activity(fetch_activity(ig, start, end)), start, end)
    n_tx = _replace(con, "ig_transactions", parse_transactions(fetch_transactions(ig, start, end)), start, end)
    log.info("IG trades: %d activity rows, %d transactions for the last %d days", n_act, n_tx, days)
    return n_act, n_tx


def main(argv=None):
    from . import db
    from .ig_ingest import IG
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--days", type=int, default=7)
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    ig = IG()
    ig.login()
    try:
        update_trades(db.connect(), ig, args.days)
    finally:
        ig.logout()


if __name__ == "__main__":
    main()
