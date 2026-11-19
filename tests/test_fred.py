from datetime import date

import duckdb
import pandas as pd

from helios_data import fred_ingest as fi


def fake(rows):
    def f(series_id, key, start):
        df = pd.DataFrame(rows)
        return df[df["d"] >= start] if start else df
    return f


def test_update_loads_and_reloads_recent_window():
    con = duckdb.connect()
    first = [{"d": date(2026, 1, d), "value": float(d)} for d in range(1, 11)]
    out = fi.update(con, "k", series=["T10Y2Y"], fetcher=fake(first), pause=0)
    assert out == {"T10Y2Y": 10}
    revised = [{"d": date(2026, 1, d), "value": float(d) * 10} for d in range(1, 13)]
    fi.update(con, "k", series=["T10Y2Y"], fetcher=fake(revised), pause=0)
    rows = con.execute("SELECT count(*), max(value) FROM fred_series WHERE series_id = 'T10Y2Y'").fetchone()
    assert rows == (12, 120.0)          # last 30 days re-fetched: revisions replace, no duplicates


def test_one_failing_series_does_not_stop_the_rest():
    con = duckdb.connect()
    def f(series_id, key, start):
        if series_id == "BAD":
            raise RuntimeError("boom")
        return pd.DataFrame([{"d": date(2026, 1, 1), "value": 1.0}])
    out = fi.update(con, "k", series=["BAD", "DGS10"], fetcher=f, pause=0)
    assert out == {"BAD": None, "DGS10": 1}
