"""Offline tests: the downloaders are replaced by fake data, so these run anywhere."""

from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest

from helios_data import db, dukascopy_ingest, ig_ingest, status


@pytest.fixture
def con(tmp_path, monkeypatch):
    monkeypatch.delenv("MOTHERDUCK_TOKEN", raising=False)
    monkeypatch.setenv("HELIOS_DB", str(tmp_path / "test.duckdb"))
    return db.connect()


def fake_bars(start, end, freq):
    idx = pd.date_range(start, end, freq=freq, tz="UTC", inclusive="left")
    idx = idx[idx.dayofweek < 5]
    rng = np.random.default_rng(len(idx))
    close = 1000 + rng.standard_normal(len(idx)).cumsum()
    return pd.DataFrame({"ts": idx, "open": close, "high": close + 1, "low": close - 1, "close": close, "volume": 1.0})


def test_full_then_incremental_load_has_no_duplicates(con, monkeypatch):
    freq = {"1d": "D", "1h": "h"}
    calls = []

    def fake_fetch(code, tf, start, end):
        calls.append((tf, start, end))
        return fake_bars(start, end, freq[tf])

    monkeypatch.setattr(dukascopy_ingest, "fetch_range", fake_fetch)
    t1 = datetime(2004, 6, 1, tzinfo=timezone.utc)
    n1 = dukascopy_ingest.update(con, "XAUUSD", "1d", now=t1)
    assert n1 > 300
    # first run starts at the configured history start
    assert calls[0][1] == datetime(2003, 1, 1, tzinfo=timezone.utc)

    t2 = datetime(2004, 7, 1, tzinfo=timezone.utc)
    dukascopy_ingest.update(con, "XAUUSD", "1d", now=t2)
    # second run starts from the last stored bar, not from 2003
    assert calls[-1][1].year == 2004 and calls[-1][1].month == 5

    rows, distinct = con.execute("SELECT count(*), count(DISTINCT ts) FROM daily WHERE symbol='XAUUSD'").fetchone()
    assert rows == distinct
    assert "duplicate bars: none" in status.report(con)


def test_chunks_cover_range_without_gaps(con, monkeypatch):
    monkeypatch.setattr(dukascopy_ingest, "fetch_range", lambda c, tf, s, e: fake_bars(s, e, "h"))
    dukascopy_ingest.update(con, "UK100", "1h", start_override="2020-01-01",
                            now=datetime(2022, 3, 1, tzinfo=timezone.utc))
    first, last, n = con.execute("SELECT min(ts), max(ts), count(*) FROM hourly WHERE symbol='UK100'").fetchone()
    assert first == datetime(2020, 1, 1)
    expected = len(fake_bars(datetime(2020, 1, 1, tzinfo=timezone.utc), datetime(2022, 3, 1, tzinfo=timezone.utc), "h"))
    assert n == expected


def test_ig_prices_parse_and_store(con):
    resp = {"prices": [
        {"snapshotTimeUTC": "2026-11-05T00:00:00", "openPrice": {"bid": 10, "ask": 12}, "highPrice": {"bid": 14, "ask": 16},
         "lowPrice": {"bid": 8, "ask": 10}, "closePrice": {"bid": 11, "ask": 13}, "lastTradedVolume": 5},
        {"snapshotTimeUTC": "2026-11-06T00:00:00", "openPrice": {"bid": 11, "ask": 13}, "highPrice": {"bid": 15, "ask": 17},
         "lowPrice": {"bid": 9, "ask": 11}, "closePrice": {"bid": 12, "ask": None}, "lastTradedVolume": 7},
    ], "metadata": {"allowance": {"remainingAllowance": 9990}}}

    class FakeIG:
        def daily_prices(self, epic, n):
            assert n == ig_ingest.IG_FIRST_RUN_DAYS
            return resp

    assert ig_ingest.update(con, FakeIG(), "UK100") == 2
    o, c = con.execute("SELECT open, close FROM ig_daily ORDER BY ts LIMIT 1").fetchone()
    assert (o, c) == (11, 12)            # mid prices
    assert con.execute("SELECT close FROM ig_daily ORDER BY ts DESC LIMIT 1").fetchone()[0] == 12  # bid only


def test_points_needed_after_first_run():
    last = datetime(2026, 11, 1)
    assert ig_ingest.points_needed(last, datetime(2026, 11, 6, tzinfo=timezone.utc)) == 8


def test_sessions_and_research_views(con):
    # 300 weekdays of hourly bars for a New York-session market and a Sydney-session market
    for sym in ("XAUUSD", "AUS200"):
        df = fake_bars(datetime(2020, 1, 1, tzinfo=timezone.utc), datetime(2021, 6, 1, tzinfo=timezone.utc), "h")
        db.write_bars(con, "dukascopy", sym, "1h", df)
    days = dict(con.execute("SELECT symbol, count(*) FROM daily_session GROUP BY 1").fetchall())
    assert 330 < days["XAUUSD"] < 400 and 330 < days["AUS200"] < 400
    assert con.execute("SELECT count(*) FROM daily_session WHERE dayofweek(session_date) IN (0, 6)").fetchone()[0] == 0
    # research layers build and the latest state exists for both markets
    assert con.execute("SELECT count(DISTINCT symbol) FROM state_daily").fetchone()[0] == 2
    assert con.execute("SELECT count(*) FROM market_now").fetchone()[0] == 2
    assert con.execute("SELECT count(*) FROM instruments").fetchone()[0] == len(dukascopy_ingest.INSTRUMENTS)


def test_minute_bars_only_where_configured(con, monkeypatch):
    fetched = []
    monkeypatch.setattr(dukascopy_ingest, "update", lambda c, s, tf, start=None: fetched.append((s, tf)) or 0)
    monkeypatch.setattr(dukascopy_ingest.db, "connect", lambda: con)
    dukascopy_ingest.main(["--symbols", "XAUUSD", "US30"])
    assert ("XAUUSD", "1m") in fetched and ("US30", "1m") not in fetched and ("US30", "1h") in fetched
