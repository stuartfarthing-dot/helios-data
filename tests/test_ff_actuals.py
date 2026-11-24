from datetime import date

import duckdb

from helios_data import ff_actuals_ingest as fa

ITEM = {"source": "forexfactory", "title": "CPI y/y", "currency": "GBP", "datetimeUtc": "2026-11-12T07:00:00.000Z",
        "date": "2026-11-12", "impact": "high", "actual": "3.1%", "forecast": "3.0%", "previous": "2.9%"}


def test_parse_skips_status_records_and_maps_fields():
    df = fa.parse([ITEM, {"status": "unavailable", "message": "blocked"}])
    assert len(df) == 1
    row = df.iloc[0]
    assert row["event_date"] == date(2026, 11, 12) and row["source"] == "ff_actuals"
    assert (row["actual"], row["forecast"], row["currency"]) == ("3.1%", "3.0%", "GBP")


def test_rerun_replaces_window_and_respects_cap():
    con = duckdb.connect()
    calls = []

    def fake(token, a, b, cap):
        calls.append((a, b, cap))
        return [ITEM]

    fa.run(con, "t", date(2026, 11, 9), date(2026, 11, 15), cap_usd=1.0, fetch=fake)
    fa.run(con, "t", date(2026, 11, 9), date(2026, 11, 15), cap_usd=1.0, fetch=fake)
    n = con.execute("SELECT count(*) FROM econ_calendar WHERE source = 'ff_actuals'").fetchone()[0]
    assert n == 1                                   # second run replaced, no duplicates
    assert calls[0][2] == 1.0


def test_windows_cover_range_without_gaps():
    w = list(fa.windows(date(2025, 4, 7), date(2025, 8, 31), days=62))
    assert w[0][0] == date(2025, 4, 7) and w[-1][1] == date(2025, 8, 31)
    assert all((w[i + 1][0] - w[i][1]).days == 1 for i in range(len(w) - 1))
