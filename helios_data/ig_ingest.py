"""Add IG's own daily prices to the warehouse (the prices you actually trade on).

Credentials come from environment variables (GitHub secrets in the cloud,
or a .env file locally - never commit it):
    IG_API_KEY, IG_USERNAME, IG_PASSWORD, IG_ACC_TYPE (LIVE or DEMO)

    python -m helios_data.ig_ingest                  # update all symbols with an ig_epic
    python -m helios_data.ig_ingest --find "Spot Gold"   # list matching IG markets and their epics
"""

from __future__ import annotations

import argparse
import logging
import os
from datetime import datetime, timezone

import pandas as pd
import requests

from . import db
from .config import IG_FIRST_RUN_DAYS, INSTRUMENTS

log = logging.getLogger("ig")
SOURCE = "ig"
BASE = {"LIVE": "https://api.ig.com/gateway/deal", "DEMO": "https://demo-api.ig.com/gateway/deal"}


class IG:
    """Minimal read-only IG REST client."""

    def __init__(self):
        try:
            from dotenv import load_dotenv
            load_dotenv()
        except ImportError:
            pass
        missing = [k for k in ("IG_API_KEY", "IG_USERNAME", "IG_PASSWORD") if not os.getenv(k)]
        if missing:
            raise SystemExit(f"Missing IG settings: {', '.join(missing)}")
        self.base = BASE[os.getenv("IG_ACC_TYPE", "LIVE").upper()]
        self.s = requests.Session()
        self.s.headers.update({
            "X-IG-API-KEY": os.environ["IG_API_KEY"],
            "Content-Type": "application/json; charset=UTF-8",
            "Accept": "application/json; charset=UTF-8",
        })

    def _req(self, method, path, version="1", **kw):
        r = self.s.request(method, self.base + path, headers={"Version": version}, timeout=30, **kw)
        if not r.ok:
            raise RuntimeError(f"IG {method} {path} -> {r.status_code}: {r.text}")
        return r

    def login(self):
        r = self._req("POST", "/session", "2",
                      json={"identifier": os.environ["IG_USERNAME"], "password": os.environ["IG_PASSWORD"]})
        self.s.headers["CST"] = r.headers["CST"]
        self.s.headers["X-SECURITY-TOKEN"] = r.headers["X-SECURITY-TOKEN"]

    def logout(self):
        try:
            self._req("DELETE", "/session")
        except Exception:
            pass

    def search(self, term):
        return self._req("GET", "/markets", params={"searchTerm": term}).json()["markets"]

    def daily_prices(self, epic, n):
        # pageSize=0 turns off IG's paging (default pages are 20 bars, oldest first)
        return self._req("GET", f"/prices/{epic}", "3",
                         params={"resolution": "DAY", "max": n, "pageSize": 0}).json()


def prices_to_frame(resp: dict) -> pd.DataFrame:
    def mid(p):
        b, a = p.get("bid"), p.get("ask")
        if b is not None and a is not None:
            return (b + a) / 2
        return b if b is not None else a

    rows = [{
        "ts": p["snapshotTimeUTC"],
        "open": mid(p["openPrice"]), "high": mid(p["highPrice"]),
        "low": mid(p["lowPrice"]), "close": mid(p["closePrice"]),
        "volume": p.get("lastTradedVolume"),
    } for p in resp.get("prices", [])]
    df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"])
    if not df.empty:
        df["ts"] = pd.to_datetime(df["ts"], utc=True)
    return df


def points_needed(last, now) -> int:
    if last is None:
        return IG_FIRST_RUN_DAYS
    days = (now.replace(tzinfo=None) - last).days
    return max(3, min(days + 3, IG_FIRST_RUN_DAYS))


def update(con, ig: IG, symbol: str, now: datetime | None = None) -> int:
    epic = INSTRUMENTS[symbol].get("ig_epic")
    if not epic:
        return 0
    now = now or datetime.now(timezone.utc)
    n = points_needed(db.last_bar(con, SOURCE, symbol, "1d"), now)
    resp = ig.daily_prices(epic, n)
    allowance = resp.get("metadata", {}).get("allowance", {})
    log.info("%s %s: requested %d days, allowance left %s", symbol, epic, n, allowance.get("remainingAllowance"))
    return db.write_bars(con, SOURCE, symbol, "1d", prices_to_frame(resp), note=epic)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--symbols", nargs="*", default=[s for s, v in INSTRUMENTS.items() if v.get("ig_epic")])
    p.add_argument("--find", help="search IG markets by name and print epics")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")

    ig = IG()
    ig.login()
    try:
        if args.find:
            for m in ig.search(args.find):
                print(f"{m['epic']:<30} {m['instrumentName']:<40} {m.get('expiry')}")
            return
        con = db.connect()
        for symbol in args.symbols:
            log.info("DONE %s: %d bars written", symbol, update(con, ig, symbol))
    finally:
        ig.logout()


if __name__ == "__main__":
    main()
