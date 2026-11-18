"""Offline tests for the IBKR loader: a fake client stands in for IB Gateway."""

from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest

from helios_data import db, ibkr_ingest as ib


@pytest.fixture
def con(tmp_path, monkeypatch):
    monkeypatch.delenv("MOTHERDUCK_TOKEN", raising=False)
    monkeypatch.setenv("HELIOS_DB", str(tmp_path / "test.duckdb"))
    return db.connect()


def bar(d, close, volume=1000.0):
    return SimpleNamespace(date=d, open=close, high=close + 0.1, low=close - 0.1, close=close, volume=volume)


class FakeClient:
    def __init__(self, bars, found=True):
        self.bars, self.found, self.durations = bars, found, []

    def contract(self, ticker):
        return SimpleNamespace(symbol=ticker, conId=1, currency="GBP", exchange="LSE") if self.found else None

    def daily_bars(self, contract, duration):
        self.durations.append(duration)
        return self.bars


def test_candidates():
    assert ib.candidates("BP") == ["BP", "BP."]
    assert ib.candidates("BT") == ["BT.A"]
    assert ib.candidates("RDSB") == ["SHEL", "SHEL."]


def test_bars_to_frame_dates_and_bad_volume():
    df = ib.bars_to_frame([bar(date(2026, 11, 16), 1354.0, 5e6), bar(date(2026, 11, 17), 1320.8, -1)])
    assert len(df) == 1                                  # volume -1 = no data, dropped
    assert df["close"].iloc[0] == pytest.approx(1354.0)  # already pence: no conversion
    assert str(df["ts"].iloc[0]) == "2026-11-16 00:00:00+00:00"


def test_first_then_incremental_load(con):
    client = FakeClient([bar(date(2026, 11, d), 13.0 + d / 100) for d in (12, 13, 16, 17, 18)])
    n = ib.update(con, client, "HSBA", today=date(2026, 11, 18))
    assert n == 4                                        # today's (18th) incomplete bar dropped
    assert client.durations == ["5 Y"]
    rows = con.execute("SELECT count(*), max(ts)::DATE::VARCHAR, max(volume) FROM bars "
                       "WHERE source='ibkr' AND symbol='HSBA.L'").fetchone()
    assert rows == (4, "2026-11-17", 1000.0)
    ib.update(con, client, "HSBA", today=date(2026, 11, 20))
    assert client.durations[-1] == "8 D"                 # gap of 3 days + 5 margin
    assert con.execute("SELECT count(*) - count(DISTINCT ts) FROM bars WHERE source='ibkr'").fetchone()[0] == 0


def test_unknown_share_is_skipped(con):
    assert ib.update(con, FakeClient([], found=False), "SHP", today=date(2026, 11, 18)) == 0


def test_uk_breadth_view(con):
    import pandas as pd
    days = pd.to_datetime(["2026-11-16", "2026-11-17"]).tz_localize("UTC")
    for i in range(60):                    # 60 shares: 40 rise on the 17th, 20 fall
        last = 101.0 if i < 40 else 99.0
        df = pd.DataFrame({"ts": days, "open": [100.0, last], "high": [100.0, last], "low": [100.0, last],
                           "close": [100.0, last], "volume": [1000.0, 1000.0]})
        db.write_bars(con, "ibkr", f"S{i}.L", "1d", df)
    row = con.execute("SELECT shares, up, down, share_up, ad_line FROM uk_breadth "
                      "WHERE session_date = DATE '2026-11-17'").fetchone()
    assert row == (60, 40, 20, 0.667, 20)
    # the first day has no previous close for any share, so it is left out
    assert con.execute("SELECT count(*) FROM uk_breadth").fetchone()[0] == 1


def test_uk_breadth_long_joins_dukascopy_before_ibkr(con):
    import pandas as pd
    duk_days = pd.to_datetime(["2021-11-01", "2021-11-02"]).tz_localize("UTC")
    ib_days = pd.to_datetime(["2026-11-16", "2026-11-17"]).tz_localize("UTC")
    for i in range(55):
        mk = lambda days, last: pd.DataFrame({"ts": days, "open": [100.0, last], "high": [100.0, last],
                                               "low": [100.0, last], "close": [100.0, last], "volume": [1.0, 1.0]})
        db.write_bars(con, "dukascopy", f"S{i}.L", "1d", mk(duk_days, 101.0 if i < 11 else 99.0))
        db.write_bars(con, "ibkr", f"S{i}.L", "1d", mk(ib_days, 101.0))
    ftse = pd.DataFrame({"ts": list(duk_days) + list(ib_days), "open": 1.0, "high": 1.0, "low": 1.0,
                         "close": [100.0, 99.0, 100.0, 101.0], "volume": 0.0})
    db.write_bars(con, "yahoo", "^FTSE", "1d", ftse)
    rows = con.execute("SELECT session_date, source, share_up, ad_line FROM uk_breadth_long ORDER BY 1").fetchall()
    assert [(str(r[0]), r[1], r[2], r[3]) for r in rows] == [
        ("2021-11-02", "dukascopy", 0.2, -33), ("2026-11-17", "ibkr", 1.0, 22)]


class FakeIntraday:
    """Serves 5-minute bars a 'month' (here: one day of 3 bars) at a time, going back from the newest."""
    def __init__(self, days):
        self.days, self.calls = days, []

    def contract(self, ticker, currency="GBP"):
        return SimpleNamespace(symbol=ticker, conId=2, currency=currency, exchange="LSE")

    def intraday_bars(self, contract, end, duration):
        self.calls.append((end, duration))
        if not self.days:
            return []
        d = self.days.pop()                       # newest remaining day
        return [SimpleNamespace(date=datetime(d.year, d.month, d.day, 8, m, tzinfo=timezone.utc), open=1.0,
                                high=1.0, low=1.0, close=1.0, volume=100.0) for m in (0, 5, 10)]


def test_intraday_backfill_then_top_up(con):
    fake = FakeIntraday([date(2026, 11, 16), date(2026, 11, 17)])
    now = datetime(2026, 11, 18, 18, 0, tzinfo=timezone.utc)
    n = ib.update_intraday(con, fake, "ISF", now=now, pause=0)
    assert n == 6 and len(fake.calls) == 3            # two chunks with bars, then an empty one ends the backfill
    assert fake.calls[0][0] == "" and fake.calls[1][0] == "20261117-08:00:00"
    assert con.execute("SELECT count(*) FROM bars WHERE timeframe='5m' AND symbol='ISF.L'").fetchone()[0] == 6
    fake2 = FakeIntraday([date(2026, 11, 18)])
    ib.update_intraday(con, fake2, "ISF", now=now, pause=0)
    assert fake2.calls == [("", "3 D")]               # top-up: 1 day since the last bar + 2 margin
    assert con.execute("SELECT count(*) FROM bars WHERE timeframe='5m'").fetchone()[0] == 9
