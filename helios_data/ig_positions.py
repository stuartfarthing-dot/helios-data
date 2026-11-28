"""Snapshot Stuart's open IG positions into the warehouse (table ig_positions).

Read-only: one GET /positions call; it never places, changes or closes anything.

Each run appends a snapshot (snap_utc), so the table is also a history of what was held when.
Run by the evening refresh workflow (before IG's 22:00 UK funding cut) and safe to run by hand:

    python -m helios_data.ig_positions
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

import pandas as pd

log = logging.getLogger("ig_positions")

SCHEMA = """
CREATE TABLE IF NOT EXISTS ig_positions (
  snap_utc TIMESTAMP, deal_id VARCHAR, epic VARCHAR, market VARCHAR, direction VARCHAR,
  size DOUBLE, level DOUBLE, stop_level DOUBLE, limit_level DOUBLE, trailing_stop BOOLEAN,
  created_utc TIMESTAMP, bid DOUBLE, offer DOUBLE, currency VARCHAR, raw JSON
);
"""

COLS = ["snap_utc", "deal_id", "epic", "market", "direction", "size", "level", "stop_level", "limit_level",
        "trailing_stop", "created_utc", "bid", "offer", "currency", "raw"]


def parse_positions(resp: dict, snap_utc: datetime) -> pd.DataFrame:
    rows = []
    for item in resp.get("positions", []):
        p, m = item.get("position") or {}, item.get("market") or {}
        rows.append({
            "snap_utc": snap_utc.replace(tzinfo=None), "deal_id": p.get("dealId"), "epic": m.get("epic"),
            "market": m.get("instrumentName"), "direction": p.get("direction"), "size": p.get("size"),
            "level": p.get("level"), "stop_level": p.get("stopLevel"), "limit_level": p.get("limitLevel"),
            "trailing_stop": bool(p.get("trailingStep") or p.get("trailingStopDistance")),
            "created_utc": p.get("createdDateUTC"), "bid": m.get("bid"), "offer": m.get("offer"),
            "currency": p.get("currency"), "raw": json.dumps(item),
        })
    df = pd.DataFrame(rows, columns=COLS)
    if not df.empty:
        df["created_utc"] = pd.to_datetime(df["created_utc"], utc=True, errors="coerce").dt.tz_localize(None)
    return df


def snapshot(con, ig, now: datetime | None = None) -> int:
    con.execute(SCHEMA)
    now = now or datetime.now(timezone.utc)
    df = parse_positions(ig._req("GET", "/positions", "2").json(), now)
    if df.empty:
        # record an empty snapshot too, so "nothing open" is distinguishable from "not checked"
        df = pd.DataFrame([{c: None for c in COLS} | {"snap_utc": now.replace(tzinfo=None)}], columns=COLS)
    con.register("new_rows", df)
    con.execute(f"INSERT INTO ig_positions ({', '.join(COLS)}) SELECT {', '.join(COLS)} FROM new_rows")
    con.unregister("new_rows")
    n = int(df["deal_id"].notna().sum())
    log.info("IG positions: %d open at %s", n, now.isoformat(timespec="seconds"))
    return n


def main(argv=None):
    from . import db
    from .ig_ingest import IG
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    ig = IG()
    ig.login()
    try:
        snapshot(db.connect(), ig)
    finally:
        ig.logout()


if __name__ == "__main__":
    main()
