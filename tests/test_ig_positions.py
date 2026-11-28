"""Offline tests for the IG open-positions snapshot (no network)."""

from datetime import datetime, timezone

import pytest

from helios_data import db, ig_positions

SAMPLE = {"positions": [
    {"position": {"dealId": "DIAAAA1", "direction": "SELL", "size": 4, "level": 22.79, "stopLevel": 29.6,
                  "limitLevel": None, "createdDateUTC": "2026-11-30T08:01:02", "currency": "GBP"},
     "market": {"epic": "CC.D.VIX.USS.IP", "instrumentName": "Volatility Index", "bid": 22.5, "offer": 22.66}},
    {"position": {"dealId": "DIAAAA2", "direction": "BUY", "size": 0.01, "level": 3551.33, "stopLevel": 3335.43,
                  "trailingStep": 5, "createdDateUTC": "2026-11-25T00:00:01", "currency": "GBP"},
     "market": {"epic": "CS.D.USCGC.TODAY.IP", "instrumentName": "Spot Gold", "bid": 3600.1, "offer": 3600.4}},
]}


class FakeResp:
    def __init__(self, js):
        self.js = js

    def json(self):
        return self.js


class FakeIG:
    def __init__(self, js):
        self.js, self.calls = js, []

    def _req(self, method, path, version="1", **kw):
        self.calls.append((method, path, version))
        return FakeResp(self.js)


@pytest.fixture
def con(tmp_path, monkeypatch):
    monkeypatch.delenv("MOTHERDUCK_TOKEN", raising=False)
    monkeypatch.setenv("HELIOS_DB", str(tmp_path / "test.duckdb"))
    return db.connect()


def test_parse_positions():
    df = ig_positions.parse_positions(SAMPLE, datetime(2026, 11, 30, 20, 41, tzinfo=timezone.utc))
    assert list(df["deal_id"]) == ["DIAAAA1", "DIAAAA2"]
    assert df.loc[0, "stop_level"] == 29.6 and df.loc[0, "direction"] == "SELL"
    assert bool(df.loc[1, "trailing_stop"]) is True and bool(df.loc[0, "trailing_stop"]) is False
    assert str(df.loc[0, "created_utc"]) == "2026-11-30 08:01:02"


def test_snapshot_is_read_only_and_appends(con):
    ig = FakeIG(SAMPLE)
    assert ig_positions.snapshot(con, ig, datetime(2026, 11, 30, 20, 41, tzinfo=timezone.utc)) == 2
    assert ig.calls == [("GET", "/positions", "2")]          # one GET, nothing else
    ig_positions.snapshot(con, FakeIG({"positions": []}), datetime(2026, 12, 1, 20, 41, tzinfo=timezone.utc))
    rows = con.execute("SELECT snap_utc, count(deal_id) FROM ig_positions GROUP BY 1 ORDER BY 1").fetchall()
    assert [r[1] for r in rows] == [2, 0]                     # an empty snapshot is still recorded
