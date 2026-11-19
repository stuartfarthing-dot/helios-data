"""Offline tests for the IBKR paper-trading planner (no Gateway, no warehouse)."""

from datetime import date, datetime
from types import SimpleNamespace

import pandas as pd
import pytest

from helios_data import db, ibkr_trade as tr

TODAY = date(2026, 11, 18)


def make_bars(symbol, closes, start="2026-09-01", high_add=1.0):
    days = pd.bdate_range(start, periods=len(closes))
    return pd.DataFrame({"symbol": symbol, "session_date": days, "open": closes,
                         "high": [c + high_add for c in closes], "low": [c - 1 for c in closes], "close": closes})


def no_trades():
    return pd.DataFrame(columns=["trade_id", "system", "symbol", "exit_rule", "signal_date", "status", "qty",
                                 "risk_1r", "entry_fill", "stop_price"])


def size(symbol, r):
    return 2.0


def test_units_for():
    assert tr.units_for(250, 100, 1.0, 50) == 2              # 2 x 100 = 200 <= 250
    assert tr.units_for(250, 1000, 1.0, 50) == 1             # never below the minimum
    assert tr.units_for(250, 1, 1.0, 50) == 50               # capped
    assert tr.units_for(250, 0, 1.0, 50) == 0


def test_enter_only_on_latest_session_and_one_position():
    bars = make_bars("US500", [100.0] * 30)
    last = bars["session_date"].max()
    signals = pd.DataFrame([
        {"system": "H1a", "symbol": "US500", "signal_date": last, "close": 100.0, "atr14": 5.0},
        {"system": "H1a", "symbol": "US500", "signal_date": last - pd.Timedelta(days=3), "close": 99.0,
         "atr14": 5.0},                                         # old: its entry day has passed
        {"system": "H14", "symbol": "US500", "signal_date": last, "close": 100.0, "atr14": 5.0},
    ])
    acts = tr.plan(signals, bars, no_trades(), size, today=last.date())
    enters = [a for a in acts if a.kind == "ENTER"]
    assert {(a.system, a.symbol) for a in enters} == {("H1a", "US500"), ("H14", "US500")}
    h1 = next(a for a in enters if a.system == "H1a")
    assert h1.stop == 90.0 and h1.qty == 2.0 and h1.exit_rule == "C2"
    assert h1.trade_id == f"H1a:US500:{last.date()}"
    assert next(a for a in enters if a.system == "H14").exit_rule == "B"


def test_uk100_h1a_uses_exit_b_and_unmapped_markets_skip():
    bars = pd.concat([make_bars("UK100", [100.0] * 30), make_bars("BTCUSD", [100.0] * 30)])
    last = bars["session_date"].max()
    signals = pd.DataFrame([
        {"system": "H1a", "symbol": "UK100", "signal_date": last, "close": 100.0, "atr14": 5.0},
        {"system": "H1a", "symbol": "BTCUSD", "signal_date": last, "close": 100.0, "atr14": 5.0},
    ])
    acts = {a.symbol: a for a in tr.plan(signals, bars, no_trades(), size, today=last.date())}
    assert acts["UK100"].kind == "ENTER" and acts["UK100"].exit_rule == "B"
    assert acts["BTCUSD"].kind == "SKIP"


def test_busy_market_and_stale_data_skip():
    bars = make_bars("XAUUSD", [100.0] * 30)
    last = bars["session_date"].max()
    trades = pd.DataFrame([{"trade_id": "H18:XAUUSD:2026-09-20", "system": "H18", "symbol": "XAUUSD",
                            "exit_rule": "C2", "signal_date": pd.Timestamp("2026-10-12"), "status": "open",
                            "qty": 1.0, "risk_1r": 10.0, "entry_fill": 100.0, "stop_price": 90.0}])
    sig = pd.DataFrame([{"system": "H18", "symbol": "XAUUSD", "signal_date": last, "close": 100.0, "atr14": 5}])
    acts = tr.plan(sig, bars, trades, size, today=last.date())
    assert any(a.kind == "SKIP" and "already" in a.reason for a in acts)
    stale = tr.plan(sig, bars, no_trades(), size, today=last.date() + pd.Timedelta(days=10))
    assert stale[0].kind == "SKIP" and "stale" in stale[0].reason


def test_c2_trails_from_highest_high_and_never_lowers():
    closes = [100.0] * 25 + [110.0, 120.0, 115.0]
    bars = make_bars("US500", closes)
    sig_day = bars["session_date"].iloc[24]
    trade = {"trade_id": "t", "system": "H1a", "symbol": "US500", "exit_rule": "C2", "signal_date": sig_day,
             "status": "open", "qty": 1.0, "risk_1r": 10.0, "entry_fill": 100.0, "stop_price": 90.0}
    acts = tr.plan(pd.DataFrame(columns=["system", "symbol", "signal_date", "close", "atr14"]), bars,
                   pd.DataFrame([trade]), size, today=TODAY)
    assert len(acts) == 1 and acts[0].kind == "MOVE_STOP" and acts[0].stop == 111.0   # 121 high - 10
    trade["stop_price"] = 115.0                                                         # already higher
    acts = tr.plan(pd.DataFrame(columns=["system", "symbol", "signal_date", "close", "atr14"]), bars,
                   pd.DataFrame([trade]), size, today=TODAY)
    assert acts == []


def test_b_exits_above_middle_band_and_resets_stop_to_fill():
    closes = [100.0] * 25 + [90.0, 92.0]
    bars = make_bars("UK100", closes)
    sig_day = bars["session_date"].iloc[24]
    trade = {"trade_id": "t", "system": "H14", "symbol": "UK100", "exit_rule": "B", "signal_date": sig_day,
             "status": "open", "qty": 1.0, "risk_1r": 10.0, "entry_fill": 95.0, "stop_price": 90.0}
    empty = pd.DataFrame(columns=["system", "symbol", "signal_date", "close", "atr14"])
    acts = tr.plan(empty, bars, pd.DataFrame([trade]), size, today=TODAY)
    assert acts[0].kind == "MOVE_STOP" and acts[0].stop == 85.0                         # fill - 1R
    bars.loc[bars.index[-1], "close"] = 101.0                                           # closes above the band
    acts = tr.plan(empty, bars, pd.DataFrame([trade]), size, today=TODAY)
    assert acts[0].kind == "EXIT" and "middle band" in acts[0].reason


def test_b_time_limit():
    bars = make_bars("UK100", [100.0] * 25 + [90.0] * 20)
    trade = {"trade_id": "t", "system": "H14", "symbol": "UK100", "exit_rule": "B",
             "signal_date": bars["session_date"].iloc[24], "status": "open", "qty": 1.0, "risk_1r": 10.0,
             "entry_fill": 95.0, "stop_price": 85.0}
    acts = tr.plan(pd.DataFrame(columns=["system", "symbol", "signal_date", "close", "atr14"]), bars,
                   pd.DataFrame([trade]), size, today=TODAY)
    assert acts[0].kind == "EXIT" and "time limit" in acts[0].reason


@pytest.fixture
def con(tmp_path, monkeypatch):
    monkeypatch.delenv("MOTHERDUCK_TOKEN", raising=False)
    monkeypatch.setenv("HELIOS_DB", str(tmp_path / "test.duckdb"))
    c = db.connect()
    c.execute(tr.TRADES_DDL)
    return c


def fill(ref, price, shares=1.0):
    return SimpleNamespace(execution=SimpleNamespace(orderRef=ref, price=price, shares=shares,
                                                     time=datetime(2026, 11, 18, 8, 0)))


def test_record_and_apply_fills(con):
    a = tr.Action("ENTER", "H1a", "US500", "H1a:US500:2026-11-17", signal_date=date(2026, 11, 17), qty=2,
                  stop=90.0, atr=5.0, signal_close=100.0, exit_rule="C2", reason="test")
    tr.record(con, a, 11, "DU123", "IBUS500")
    notes = tr.apply_fills(con, [fill("helios:H1a:US500:2026-11-17:entry", 101.0, 2),
                                 fill("other:thing", 5.0)])
    assert notes and "entry filled" in notes[0]
    row = con.execute("SELECT status, entry_fill FROM paper_trades").fetchone()
    assert row == ("open", 101.0)
    tr.apply_fills(con, [fill("helios:H1a:US500:2026-11-17:entry", 101.0, 2)])        # repeat: no change
    tr.apply_fills(con, [fill("helios:H1a:US500:2026-11-17:stop", 91.0, 2)])
    assert con.execute("SELECT status, exit_fill, exit_how FROM paper_trades").fetchone() == ("closed", 91.0, "stop")
    assert con.execute("SELECT count(*) FROM paper_orders").fetchone()[0] == 1


def test_entry_deadlines():
    # US/metals: same day 19:30 New York (00:30 UTC next day in November); Europe: next weekday's cash open
    assert tr.entry_deadline("US500", date(2026, 11, 17)) == pd.Timestamp("2026-11-18 00:30", tz="UTC")
    assert tr.entry_deadline("UK100", date(2026, 11, 20)) == pd.Timestamp("2026-11-23 08:00", tz="UTC")  # Fri->Mon
    assert tr.entry_deadline("JPN225", date(2026, 11, 18)) == pd.Timestamp("2026-11-18 07:30", tz="UTC")
    assert tr.entry_deadline("BTCUSD", date(2026, 11, 18)) is None


def test_late_data_never_enters():
    bars = make_bars("US500", [100.0] * 30)
    last = bars["session_date"].max()
    sig = pd.DataFrame([{"system": "H1a", "symbol": "US500", "signal_date": last, "close": 100.0, "atr14": 5}])
    early = tr.entry_deadline("US500", last) - pd.Timedelta(minutes=30)
    late = tr.entry_deadline("US500", last) + pd.Timedelta(minutes=1)
    assert tr.plan(sig, bars, no_trades(), size, today=last.date(), now=early)[0].kind == "ENTER"
    skipped = tr.plan(sig, bars, no_trades(), size, today=last.date(), now=late)[0]
    assert skipped.kind == "SKIP" and "window closed" in skipped.reason


def test_exit_not_sent_twice():
    bars = make_bars("UK100", [100.0] * 25 + [90.0] * 20)
    trade = {"trade_id": "t", "system": "H14", "symbol": "UK100", "exit_rule": "B",
             "signal_date": bars["session_date"].iloc[24], "status": "open", "qty": 1.0, "risk_1r": 10.0,
             "entry_fill": 95.0, "stop_price": 85.0, "exit_how": "exit sent: time limit (20 sessions)"}
    acts = tr.plan(pd.DataFrame(columns=["system", "symbol", "signal_date", "close", "atr14"]), bars,
                   pd.DataFrame([trade]), size, today=TODAY)
    assert [a.kind for a in acts] == ["WARN"]
