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


def test_crypto_keeps_weekends(con):
    def all_days(start, end):
        idx = pd.date_range(start, end, freq="h", tz="UTC", inclusive="left")
        c = 30000 + np.arange(len(idx), dtype=float)
        return pd.DataFrame({"ts": idx, "open": c, "high": c + 5, "low": c - 5, "close": c, "volume": 1.0})
    s, e = datetime(2024, 1, 1, tzinfo=timezone.utc), datetime(2024, 2, 1, tzinfo=timezone.utc)
    db.write_bars(con, "dukascopy", "BTCUSD", "1h", all_days(s, e))
    db.write_bars(con, "dukascopy", "EURUSD", "1h", all_days(s, e))
    n = dict(con.execute("SELECT symbol, count(*) FROM daily_session GROUP BY 1").fetchall())
    assert n["BTCUSD"] == 31          # every calendar day in January
    assert 22 <= n["EURUSD"] <= 24    # weekdays only


def test_ig_trade_parsing():
    from helios_data.ig_trades import parse_activity, parse_transactions, _num
    assert _num("+0.5") == 0.5 and _num("£2.67") == 2.67 and _num("-£1,060.00") == -1060.0 and _num("-") is None
    a = parse_activity([{"date": "2026-11-10T08:00:01", "epic": "IX.D.FTSE.DAILY.IP", "dealId": "D1",
                         "type": "POSITION", "status": "ACCEPTED", "description": "Position opened",
                         "channel": "WEB", "details": {"marketName": "FTSE 100", "direction": "BUY",
                                                       "size": 0.01, "level": 10851.4, "stopLevel": 10835.4,
                                                       "actions": [{"actionType": "POSITION_OPENED"}]}}])
    assert a.loc[0, "level"] == 10851.4 and a.loc[0, "stop_level"] == 10835.4 and a.loc[0, "direction"] == "BUY"
    t = parse_transactions([{"dateUtc": "2026-11-10T08:05:00", "openDateUtc": "2026-11-10T08:00:30",
                             "instrumentName": "FTSE 100", "reference": "R1", "transactionType": "DEAL",
                             "size": "+0.1", "openLevel": "10861.1", "closeLevel": "10853.3",
                             "profitAndLoss": "-£0.78", "currency": "£"}])
    assert t.loc[0, "pnl_gbp"] == -0.78 and t.loc[0, "size"] == 0.1


def test_calendar_and_dividends(con):
    from helios_data import events_ingest as ev
    con.execute(ev.SCHEMA)
    week = [
        {"title": "Non-Farm Employment Change", "country": "USD", "date": "2026-11-06T08:30:00-05:00",
         "impact": "High", "forecast": "120K", "previous": "95K"},
        {"title": "Bank Holiday", "country": "JPY", "date": "2026-11-03T00:00:00-05:00", "impact": "Holiday",
         "forecast": "", "previous": ""},
    ]
    n = ev.update_calendar(con, fetch=lambda url: week if "thisweek" in url else [])
    assert n == 2
    # re-running replaces rather than duplicates
    ev.update_calendar(con, fetch=lambda url: week if "thisweek" in url else [])
    rows = con.execute("SELECT event_date, event_time_utc, impact, forecast FROM econ_calendar "
                       "WHERE currency='USD'").fetchall()
    assert len(rows) == 1
    assert str(rows[0][1]) == "2026-11-06 13:30:00" and rows[0][2] == "high" and rows[0][3] == "120K"
    assert con.execute("SELECT count(*) FROM econ_calendar").fetchone()[0] == 2

    def fake_divs(ysym):
        if ysym == "GONE.L":
            return pd.Series(dtype=float)
        idx = pd.DatetimeIndex(["2025-02-20", "2025-08-14"]).tz_localize("Europe/London")
        spl = pd.Series([0.1], index=pd.DatetimeIndex(["2024-06-03"]).tz_localize("Europe/London"))
        return pd.Series([7.5, 8.0], index=idx), spl

    res = ev.update_dividends(con, ["BP", "BT", "GONE"], fetch=fake_divs, pause=0)
    ev.update_dividends(con, ["BP"], fetch=fake_divs, pause=0)
    assert res["got"] == 2 and res["empty"] == ["GONE"]
    assert con.execute("SELECT count(*) FROM dividends WHERE symbol='BP.L'").fetchone()[0] == 2
    assert con.execute("SELECT yahoo_symbol FROM dividends WHERE symbol='BT.L' LIMIT 1").fetchone()[0] == "BT-A.L"
    assert str(con.execute("SELECT min(ex_date) FROM dividends").fetchone()[0]) == "2025-02-20"
    assert con.execute("SELECT count(*), max(ratio) FROM splits WHERE symbol='BP.L'").fetchone() == (1, 0.1)


def test_ig_sentiment(con):
    from helios_data import ig_sentiment as s

    class FakeResp:
        def __init__(self, data):
            self.data = data

        def json(self):
            return self.data

    class FakeIG:
        def search(self, term):
            return [{"epic": "X.OTHER", "instrumentName": "Something else", "expiry": "DFB"},
                    {"epic": "X.FUT", "instrumentName": term + " Mar-27", "expiry": "MAR-27"},
                    {"epic": "X.DFB", "instrumentName": term + " Cash", "expiry": "DFB"}]

        def _req(self, method, path, version="1", **kw):
            if path.startswith("/markets/"):
                return FakeResp({"instrument": {"marketId": "M_" + path.split("/")[-1]}})
            ids = kw["params"]["marketIds"].split(",")
            return FakeResp({"clientSentiments": [
                {"marketId": m, "longPositionPercentage": 60.0, "shortPositionPercentage": 40.0} for m in ids]})

    assert s.pick_epic(FakeIG().search("US 500"), "US 500") == "X.DFB"
    df = s.update(con, FakeIG())
    assert len(df) == len(s.MARKETS)
    s.update(con, FakeIG())   # same day again replaces, not duplicates
    n, lp = con.execute("SELECT count(*), max(long_pct) FROM ig_sentiment").fetchone()
    assert n == len(s.MARKETS) and lp == 60.0


def test_yahoo_daily(con):
    from helios_data import events_ingest as ev
    idx = pd.DatetimeIndex(["2026-11-05", "2026-11-06"]).tz_localize("America/New_York")
    h = pd.DataFrame({"Open": [50.0, 51.0], "High": [52.0, 53.0], "Low": [49.0, 50.0],
                      "Close": [51.5, 52.5], "Volume": [1e6, 2e6]}, index=idx)
    res = ev.update_yahoo_daily(con, ["SIL"], fetch=lambda t: h)
    ev.update_yahoo_daily(con, ["SIL"], fetch=lambda t: h)   # rerun replaces
    assert res["SIL"] == 2
    rows = con.execute("SELECT ts::DATE, close FROM bars WHERE source='yahoo' AND symbol='SIL' ORDER BY ts").fetchall()
    assert [str(r[0]) for r in rows] == ["2026-11-05", "2026-11-06"] and rows[1][1] == 52.5
