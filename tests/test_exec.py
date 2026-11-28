"""Offline tests for the execution program: a fake IG broker and a local warehouse file. Nothing leaves the machine."""

from datetime import datetime, timedelta

import duckdb
import pytest

from helios_exec import plan, risk, warehouse as wh
from helios_exec.__main__ import run
from helios_exec.broker_ig import IGBroker
from helios_exec.config import Settings

TARGET_COLS = ("system_id VARCHAR, symbol VARCHAR, sector VARCHAR, epic VARCHAR, direction VARCHAR, exit_rule VARCHAR, "
               "want_position BOOLEAN, may_enter BOOLEAN, source_status VARCHAR, stop_mode VARCHAR, stop_distance_ig DOUBLE, "
               "limit_distance_ig DOUBLE, stop_below_close_ig DOUBLE, limit_above_close_ig DOUBLE, risk_1r_ig DOUBLE, "
               "stop_pct DOUBLE, signal_date DATE, as_of DATE")


def make_db(path, enabled=True):
    con = duckdb.connect(str(path))
    for s in ("exec", "live", "ref"):
        con.execute(f"CREATE SCHEMA {s}")
    con.execute("CREATE TABLE exec.control (enabled BOOLEAN, note VARCHAR, updated_utc TIMESTAMP)")
    con.execute("INSERT INTO exec.control VALUES (?, 'test', now())", [enabled])
    con.execute("CREATE TABLE exec.runs (run_id VARCHAR, started_utc TIMESTAMP, finished_utc TIMESTAMP, mode VARCHAR, account VARCHAR, "
                "targets INTEGER, orders INTEGER, status VARCHAR, note VARCHAR)")
    con.execute("CREATE TABLE exec.orders (run_id VARCHAR, ts_utc TIMESTAMP, mode VARCHAR, system_id VARCHAR, epic VARCHAR, action VARCHAR, "
                "direction VARCHAR, size DOUBLE, stop_distance DOUBLE, stop_level DOUBLE, limit_distance DOUBLE, limit_level DOUBLE, "
                "deal_reference VARCHAR, deal_id VARCHAR, status VARCHAR, level DOUBLE, reason VARCHAR)")
    con.execute("CREATE TABLE exec.holdings (deal_id VARCHAR, system_id VARCHAR, epic VARCHAR, direction VARCHAR, size DOUBLE, "
                "open_level DOUBLE, opened_utc TIMESTAMP, mode VARCHAR, closed_utc TIMESTAMP, close_reason VARCHAR, last_stop DOUBLE)")
    con.execute(f"CREATE TABLE live.exec_targets ({TARGET_COLS})")
    con.execute("CREATE TABLE ref.risk_limits (limit_name VARCHAR, value DOUBLE)")
    con.execute("INSERT INTO ref.risk_limits VALUES ('gbp_per_r_target', 250), ('max_open_r_total', 20), "
                "('max_open_r_sector', 8), ('max_open_r_market', 4), ('daily_loss_limit_r', -6)")
    con.execute("CREATE TABLE ref.sectors (symbol VARCHAR, sector VARCHAR)")
    con.execute("INSERT INTO ref.sectors VALUES ('VIX', 'Volatility'), ('US500', 'US indices')")
    return con


def set_h43(con, calm: bool):
    con.execute("DELETE FROM live.exec_targets WHERE system_id = 'H43 VIX'")
    con.execute("INSERT INTO live.exec_targets VALUES ('H43 VIX', 'VIX', 'Volatility', 'CC.D.VIX.USS.IP', 'SELL', 'stress exit', "
                "?, ?, ?, 'pct', NULL, NULL, NULL, NULL, NULL, 0.30, DATE '2026-11-27', DATE '2026-11-27')",
                [calm, calm, "H43 calm" if calm else "H43 stress"])


class FakeIG:
    base = "https://demo-api.ig.com/gateway/deal"


class FakeBroker:
    """Stands in for IGBroker: keeps positions in memory and records every call."""
    account = "IG DEMO (fake)"

    def __init__(self, status="TRADEABLE", bid=22.70, offer=22.86):
        self.status, self.bid, self.offer = status, bid, offer
        self.pos, self.calls, self.n = {}, [], 0

    def market(self, epic):
        return {"epic": epic, "status": self.status, "bid": self.bid, "offer": self.offer, "expiry": "DEC-26",
                "currency": "GBP", "min_size": 0.5, "min_stop": 0.5}

    def positions(self):
        return [dict(p) for p in self.pos.values()]

    def open(self, epic, direction, size, stop_distance, limit_distance, deal_reference, expiry, currency):
        self.calls.append(("open", epic, direction, size, stop_distance, expiry))
        self.n += 1
        did = f"DEAL{self.n}"
        lvl = self.bid if direction == "SELL" else self.offer
        stop = lvl + stop_distance if direction == "SELL" else lvl - stop_distance
        self.pos[did] = {"deal_id": did, "deal_reference": deal_reference, "epic": epic, "direction": direction, "size": size,
                         "level": lvl, "stop_level": stop, "limit_level": None, "bid": self.bid, "offer": self.offer}
        return {"status": "ACCEPTED", "deal_id": did, "level": lvl, "reason": "SUCCESS"}

    def close(self, deal_id, direction, size):
        self.calls.append(("close", deal_id))
        self.pos.pop(deal_id)
        return {"status": "ACCEPTED", "deal_id": deal_id, "level": self.offer if direction == "SELL" else self.bid, "reason": "SUCCESS"}

    def amend(self, deal_id, stop_level, limit_level):
        self.calls.append(("amend", deal_id, stop_level))
        self.pos[deal_id]["stop_level"] = stop_level
        return {"status": "ACCEPTED", "deal_id": deal_id, "level": None, "reason": "SUCCESS"}


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return make_db(tmp_path / "w.duckdb")


def test_live_mode_is_refused():
    with pytest.raises(SystemExit):
        Settings(mode="live").check()


def test_broker_refuses_live_environment():
    class LiveIG:
        base = "https://api.ig.com/gateway/deal"
    with pytest.raises(SystemExit):
        IGBroker(LiveIG(), allow_orders=True)
    b = IGBroker(LiveIG(), allow_orders=False)           # read-only use is fine
    with pytest.raises(RuntimeError):
        b.open("X", "BUY", 1, 10, None, "HX1", "DFB", "GBP")


def test_dry_run_places_nothing(db):
    set_h43(db, calm=True)
    b = FakeBroker()
    out = run(Settings(mode="dry"), broker=b, con=db)
    assert [c[0] for c in b.calls] == []
    rows = db.execute("SELECT action, status, direction, size, stop_distance FROM exec.orders").fetchall()
    assert rows[0][:3] == ("open", "DRY RUN", "SELL")
    assert rows[0][4] == pytest.approx(0.30 * 22.70, abs=0.01)
    assert rows[0][3] == int(250 / rows[0][4] * 100) / 100           # £250 per R, rounded down


def test_demo_open_then_close_on_stress(db):
    set_h43(db, calm=True)
    b = FakeBroker()
    run(Settings(mode="demo"), broker=b, con=db)
    assert b.calls[0][:3] == ("open", "CC.D.VIX.USS.IP", "SELL") and b.calls[0][5] == "DEC-26"
    assert db.execute("SELECT count(*) FROM exec.holdings WHERE closed_utc IS NULL").fetchone()[0] == 1
    run(Settings(mode="demo"), broker=b, con=db)                      # still calm: hold, no new order
    assert len(b.calls) == 1
    set_h43(db, calm=False)
    b.offer = 21.0                                                     # VIX fell: the short made money
    run(Settings(mode="demo"), broker=b, con=db)
    assert b.calls[-1][0] == "close" and not b.pos
    pnl = db.execute("SELECT level FROM exec.orders WHERE action = 'pnl'").fetchone()[0]
    assert pnl == pytest.approx((21.0 - 22.70) * -b.calls[0][3])


def test_stopped_position_waits_before_reentry(db):
    set_h43(db, calm=True)
    b = FakeBroker()
    run(Settings(mode="demo"), broker=b, con=db)
    run(Settings(mode="demo"), broker=b, con=db)                      # records the last stop seen
    b.pos.clear()                                                      # IG stopped it out
    run(Settings(mode="demo"), broker=b, con=db)
    assert db.execute("SELECT close_reason FROM exec.holdings").fetchone()[0].startswith("gone")
    assert [c[0] for c in b.calls] == ["open"]                          # no re-entry yet
    assert db.execute("SELECT count(*) FROM exec.orders WHERE reason LIKE 'waiting after a stop%'").fetchone()[0] == 1
    loss = db.execute("SELECT level FROM exec.orders WHERE action = 'pnl'").fetchone()[0]
    assert loss < 0


def test_kill_file_and_disabled_switch(db, tmp_path):
    set_h43(db, calm=True)
    b = FakeBroker()
    (tmp_path / "KILL").write_text("stop")
    assert run(Settings(mode="demo"), broker=b, con=db)["status"] == "killed"
    (tmp_path / "KILL").unlink()
    db.execute("UPDATE exec.control SET enabled = FALSE")
    assert run(Settings(mode="demo"), broker=b, con=db)["status"] == "disabled"
    assert b.calls == []


def test_market_closed_is_skipped(db):
    set_h43(db, calm=True)
    b = FakeBroker(status="EDITS_ONLY")
    run(Settings(mode="demo"), broker=b, con=db)
    assert b.calls == []
    assert "market not open" in db.execute("SELECT reason FROM exec.orders").fetchone()[0]


def test_flatten_closes_everything(db):
    set_h43(db, calm=True)
    b = FakeBroker()
    run(Settings(mode="demo"), broker=b, con=db)
    run(Settings(mode="demo"), broker=b, con=db, flatten=True)
    assert not b.pos and b.calls[-1][0] == "close"


def test_trailing_stop_only_moves_up():
    t = [{"system_id": "H1a C2 US500", "symbol": "US500", "sector": "US indices", "epic": "IX.D.SPTRD.DAILY.IP",
          "direction": "BUY", "want_position": True, "may_enter": False, "source_status": "open", "stop_mode": "distance",
          "stop_distance_ig": 350, "limit_distance_ig": None, "stop_below_close_ig": 300, "stop_pct": None}]
    held = [{"system_id": "H1a C2 US500", "deal_id": "D1", "size": 0.7, "bid": 7000, "stop_level": 6650, "limit_level": None}]
    a = plan.build(t, held, {}, 250, datetime(2026, 11, 30))
    assert a[0].kind == "amend" and a[0].stop_level == 6700
    held[0]["stop_level"] = 6750
    assert plan.build(t, held, {}, 250, datetime(2026, 11, 30)) == []      # never lowered


def test_running_trade_is_not_joined_late():
    t = [{"system_id": "H14 XAUUSD", "symbol": "XAUUSD", "sector": "Precious metals", "epic": "CS.D.USCGC.TODAY.IP",
          "direction": "BUY", "want_position": True, "may_enter": False, "source_status": "open", "stop_mode": "distance",
          "stop_distance_ig": 214, "limit_distance_ig": None, "stop_below_close_ig": 310, "stop_pct": None}]
    a = plan.build(t, [], {}, 250, datetime(2026, 11, 30))
    assert a[0].kind == "skip" and "not joined late" in a[0].reason


def test_risk_limits_block_new_entries_only():
    s = Settings(mode="demo")
    lim = {"max_open_r_total": 20, "max_open_r_sector": 8, "max_open_r_market": 4, "daily_loss_limit_r": -6}
    held = [{"sector": "US indices", "symbol": "US500"}] * 4
    opens = [plan.Action("open", "X", "E", size=1, sector="US indices", symbol="US500"),
             plan.Action("close", "Y", "E", deal_id="D")]
    opens[0].stop_distance = 100
    out = risk.check(opens, held, lim, s, realised_today_gbp=0, gbp_per_r=250)
    assert out[0].kind == "skip" and "US500 would exceed" in out[0].reason and out[1].kind == "close"
    out = risk.check([plan.Action("open", "X", "E", size=1, stop_distance=100, sector="Energy", symbol="BRENT")], [], lim, s,
                     realised_today_gbp=-1600, gbp_per_r=250)
    assert "daily loss limit" in out[0].reason
    out = risk.check([plan.Action("open", "X", "E", size=9, stop_distance=100, sector="Energy", symbol="BRENT")], [], lim, s, 0, 250)
    assert "above the hard cap" in out[0].reason                       # £900 at risk
    out = risk.check([plan.Action("open", "X", "E", size=50, stop_distance=1, sector="Energy", symbol="BRENT",
                                  extra={"price": 7000})], [], lim, s, 0, 250)
    assert "exposure" in out[0].reason                                 # tiny stop, huge position
    out = risk.check([plan.Action("open", "X", "E", size=1, sector="Energy", symbol="BRENT")], [], lim, s, 0, 250)
    assert "without a stop" in out[0].reason


def test_stake_rounding_and_minimum():
    assert plan.stake(250, 351.1, 0.01)[0] == 0.71
    s, note = plan.stake(250, 40000, 0.5)
    assert s == 0.5 and "below IG minimum" in note
    assert len(plan.deal_reference("HX", "H43 VIX", datetime(2026, 11, 30, 5, 1, 2))) <= 30


def test_demo_session_only_knows_the_demo_url(monkeypatch):
    from helios_exec.broker_ig import DemoIG
    for k in ("HX_IG_API_KEY", "HX_IG_USERNAME", "HX_IG_PASSWORD"):
        monkeypatch.setenv(k, "x")
    assert DemoIG().base == "https://demo-api.ig.com/gateway/deal"
