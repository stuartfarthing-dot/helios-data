"""Australian rates from the Reserve Bank of Australia into the warehouse table rba_series (Q14.34).

For the Australian case study (Q14.32): the rates channel from data surprises to the ASX 200 and the AUD.
Two free CSV tables, no key needed:

  F1  money market, daily, published each morning: cash rate target (FIRMMCRTD), interbank overnight rate,
      bank bill rates 30/90/180 days (FIRMMBAB30D/90D/180D: the market's expected path for the cash rate)
      and OIS 1/3/6 months where published.
  F2  government bond yields, daily since May 2013 but published weekly (Fridays, up to 2 business days
      earlier): 2, 3, 5 and 10-year (FCMYGBAG2D ... 10D) and the indexed bond (FCMYGBAGID).

Both files carry a few title rows, then a "Series ID" row, then one row per date (dd-Mon-yyyy). Each run
reloads both tables in full (a few thousand rows each). A failed download only logs.

    python -m helios_data.rba_ingest
"""
from __future__ import annotations

import csv
import io
import logging
import os

import pandas as pd
import requests

log = logging.getLogger("rba")

TABLES = {
    "F1": "https://www.rba.gov.au/statistics/tables/csv/f1-data.csv",
    "F2": "https://www.rba.gov.au/statistics/tables/csv/f2-data.csv",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS rba_series (
  series_id VARCHAR, d DATE, value DOUBLE, rba_table VARCHAR, loaded_at TIMESTAMP DEFAULT current_timestamp
);
"""


def fetch(url: str, timeout: float = 60) -> str:
    r = requests.get(url, timeout=timeout, headers={"User-Agent": "Mozilla/5.0 (helios-data)"})
    r.raise_for_status()
    return r.content.decode("utf-8-sig", errors="replace")


def parse(text: str) -> pd.DataFrame:
    """Long table (series_id, d, value) from an RBA statistical-table CSV."""
    rows = list(csv.reader(io.StringIO(text.replace("\r\n", "\n").replace("\r", "\n"))))
    ids = next((r for r in rows if r and r[0].strip().lower() == "series id"), None)
    if ids is None:
        raise ValueError("RBA table: no 'Series ID' row")
    out = []
    for r in rows:
        if not r or not r[0].strip():
            continue
        d = pd.to_datetime(r[0].strip(), format="%d-%b-%Y", errors="coerce")
        if pd.isna(d):
            continue
        for j in range(1, min(len(r), len(ids))):
            v = r[j].strip()
            if not v or not ids[j].strip():
                continue
            try:
                out.append((ids[j].strip(), d.date(), float(v)))
            except ValueError:
                continue
    return pd.DataFrame(out, columns=["series_id", "d", "value"])


def update(con, fetcher=fetch) -> dict:
    con.execute(SCHEMA)
    res = {}
    for name, url in TABLES.items():
        try:
            df = parse(fetcher(url))
        except Exception as exc:
            log.warning("RBA %s failed: %s", name, exc)
            continue
        if df.empty:
            log.warning("RBA %s: parsed 0 rows - table left as it was", name)
            continue
        df["rba_table"] = name
        con.execute("DELETE FROM rba_series WHERE rba_table = ?", [name])
        con.register("rba_new", df)
        con.execute("INSERT INTO rba_series (series_id, d, value, rba_table) "
                    "SELECT series_id, d, value, rba_table FROM rba_new")
        con.unregister("rba_new")
        res[name] = len(df)
        log.info("RBA %s: %d values, %d series, last %s", name, len(df), df.series_id.nunique(), df.d.max())
    return res


def main():
    from . import db
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    con = db.connect()
    update(con)
    con.close()
    os._exit(0)


if __name__ == "__main__":
    main()
