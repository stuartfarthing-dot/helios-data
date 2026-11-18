"""Offline tests for the IBKR loader: a fake client stands in for IB Gateway."""

from datetime import date, datetime
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
