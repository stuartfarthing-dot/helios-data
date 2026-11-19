"""Helios paper trading on Interactive Brokers: H1a, H14 and H18 on IBKR CFDs, run once a night.

What it does, in order:
  1. reconcile - reads IBKR's fills since the last run and updates the trade book (`paper_trades`):
                 entries filled, stops hit, exits done.
  2. plan      - reads the same warehouse views the research and the IG alerts use (h1_signals,
                 h14_signals_paper, h18_signals, daily_session) and works out tonight's actions:
                   ENTER     new signal on the last completed session -> buy at the next open, stop 2 x ATR
                   MOVE_STOP C2: stop = max(fill - 1R, highest high since entry - 1R), never lowered
                             (B: the stop stays at fill - 1R; first move replaces the provisional stop)
                   EXIT      B: first close above the 20-day middle band, or 20 sessions
                             C2: 100 sessions
  3. execute   - only with --send: places the orders on the PAPER account and logs them (`paper_orders`).

Rules are the pre-registered ProRealTime ones (strategies/prorealtime/Helios_H1a_daily.txt, ..._H14_...,
..._H18_...): one position per system per market; H1a uses exit B on the FTSE and C2 elsewhere.
H1a's extra slices (H28: B/C2/+1R) are not traded here yet - one slice per signal, as pre-registered.

Safety:
  * dry run by default: prints the plan, places nothing, writes nothing.
  * --send refuses to run unless every account on the connection is a paper account (IDs start "DU").
    There is no live mode in this version.
  * a file called STOP_TRADING in the working folder blocks --send.
  * every order carries an orderRef "helios:<system>:<symbol>:<signal date>:<leg>" so it can be traced.

    python -m helios_data.ibkr_trade --offline          # plan from the warehouse only (no Gateway needed)
    python -m helios_data.ibkr_trade                    # dry run with contract and FX checks on the Gateway
    python -m helios_data.ibkr_trade --check-contracts  # which Helios markets exist at IBKR, and their sizes
    python -m helios_data.ibkr_trade --send             # place tonight's orders on the paper account
    python -m helios_data.ibkr_trade --send --refresh   # what the hourly scheduled task runs

Timing: each market's day ends at a different time (US and metals 17:00 New York, Tokyo 16:00, Europe at local
midnight...), and an entry must go in before that market's next open (ENTRY_DEADLINE). So the scheduled task runs
every hour: it refreshes the Dukascopy hourly bars, enters only signals still inside their window, and logs the
rest as "entry window closed". Running it more often than needed is harmless: a market with a trade (or a sent
entry) is skipped, and an exit is never sent twice.

Settings (.env, all optional): IBKR_HOST, IBKR_PORT (paper Gateway: 4002), IBKR_TRADE_CLIENT_ID (27 - keep it
fixed: IBKR only lets the client that placed an order modify it), HELIOS_RISK_GBP (risk per trade, default 250),
HELIOS_MAX_UNITS (cap per order, default 50).
"""

from __future__ import annotations

import argparse
import logging
import math
import os
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from . import db

log = logging.getLogger("trade")

# ---------------------------------------------------------------------------------------------------------
# Markets and systems
# ---------------------------------------------------------------------------------------------------------

# Helios symbol -> IBKR contracts to try, in order: (secType, symbol, exchange, currency).
# IBKR's index CFDs are GBP/USD/EUR... 1 per point per unit; metals are per troy ounce.
MARKETS: dict[str, list[tuple[str, str, str, str]]] = {
    "UK100": [("CFD", "IBGB100", "SMART", "GBP")],
    "US500": [("CFD", "IBUS500", "SMART", "USD")],
    "NAS100": [("CFD", "IBUST100", "SMART", "USD")],
    "US30": [("CFD", "IBUS30", "SMART", "USD")],
    "GER40": [("CFD", "IBDE40", "SMART", "EUR")],
    "AUS200": [("CFD", "IBAU200", "SMART", "AUD")],
    "JPN225": [("CFD", "IBJP225", "SMART", "JPY")],
    "HK50": [("CFD", "IBHK50", "SMART", "HKD")],
    "ESP35": [("CFD", "IBES35", "SMART", "EUR")],
    "FRA40": [("CFD", "IBFR40", "SMART", "EUR")],
    "XAUUSD": [("CMDTY", "XAUUSD", "SMART", "USD"), ("CFD", "XAUUSD", "SMART", "USD")],
    "XAGUSD": [("CMDTY", "XAGUSD", "SMART", "USD"), ("CFD", "XAGUSD", "SMART", "USD")],
    "XPTUSD": [("CMDTY", "XPTUSD", "SMART", "USD"), ("CFD", "XPTUSD", "SMART", "USD")],
}
# Alert markets with no IBKR equivalent in this version: BRENT, COPPER (futures only), BTCUSD, ETHUSD, RUT.


@dataclass(frozen=True)
class System:
    name: str
    markets: tuple[str, ...]
    exit_rule: dict[str, str] | str      # "B" / "C2", or per market

    def rule(self, symbol: str) -> str:
        return self.exit_rule.get(symbol, "C2") if isinstance(self.exit_rule, dict) else self.exit_rule


H1A_MARKETS = ("UK100", "US500", "NAS100", "US30", "GER40", "XAUUSD", "XAGUSD", "COPPER", "BRENT", "XPTUSD",
               "BTCUSD", "ETHUSD", "RUT", "JPN225", "HK50", "ESP35", "FRA40")      # = live_trades alert markets
H14_MARKETS = ("UK100", "US500", "NAS100", "US30", "GER40", "AUS200", "XAUUSD", "XAGUSD", "XPTUSD", "ESP35", "FRA40",
               "JPN225")   # Japan 225: paper only (19 Nov 2026), not in ProRealTime

SYSTEMS = {
    "H1a": System("H1a", H1A_MARKETS, {"UK100": "B"}),
    "H14": System("H14", H14_MARKETS, "B"),
    "H18": System("H18", ("XAUUSD", "XAGUSD"), "C2"),
}
MAX_HOLD = {"B": 20, "C2": 100}

# Latest time an entry may still be sent for a signal on session D: the next session's open, so a late data
# update never turns into a late entry. (tz, hour, minute, days after D: 0 = same day, 1 = next weekday)
# US indices and metals: the Helios day ends 17:00 New York and the market reopens 18:00. These trade nearly
# 24 hours, so entries are allowed until 19:30 New York (90 minutes after the reopen; Stuart, 19 Nov 2026) -
# one late or skipped hourly run must not lose the trade. The fill vs the reopen price is in the trade log.
# Japan 225 and Hang Seng: the Helios day ends 16:00 Tokyo / 17:00 HK, then the evening session starts.
# Europe and Australia: the Helios day is the local date; the deadline is the next cash open.
NY = ("America/New_York", 19, 30, 0)
ENTRY_DEADLINE = {
    "US500": NY, "NAS100": NY, "US30": NY, "XAUUSD": NY, "XAGUSD": NY, "XPTUSD": NY,
    "JPN225": ("Asia/Tokyo", 16, 30, 0), "HK50": ("Asia/Hong_Kong", 17, 15, 0),
    "UK100": ("Europe/London", 8, 0, 1), "GER40": ("Europe/Berlin", 8, 0, 1), "FRA40": ("Europe/Paris", 8, 0, 1),
    "ESP35": ("Europe/Madrid", 8, 0, 1), "AUS200": ("Australia/Sydney", 10, 0, 1),
}


def entry_deadline(symbol: str, signal_date) -> pd.Timestamp | None:
    """UTC time after which a signal on signal_date may no longer be entered."""
    spec = ENTRY_DEADLINE.get(symbol)
    if spec is None:
        return None
    tz, hh, mm, nxt = spec
    d = pd.Timestamp(signal_date).normalize()
    if nxt:
        d = d + pd.offsets.BDay(1)
    return pd.Timestamp(d.year, d.month, d.day, hh, mm).tz_localize(tz).tz_convert("UTC")

SIGNALS_SQL = """
SELECT 'H1a' AS system, symbol, session_date AS signal_date, close, atr14 FROM h1_signals
 WHERE setup AND session_date >= ?
UNION ALL
SELECT 'H14', symbol, signal_date, close, atr14 FROM h14_signals_paper WHERE signal_date >= ?
UNION ALL
SELECT 'H18', symbol, signal_date, close, atr14 FROM h18_signals WHERE signal_date >= ?
"""

TRADES_DDL = """
CREATE TABLE IF NOT EXISTS paper_trades (
  trade_id VARCHAR PRIMARY KEY,          -- <system>:<symbol>:<signal date>
  system VARCHAR, symbol VARCHAR, ib_symbol VARCHAR, exit_rule VARCHAR,
  signal_date DATE, signal_close DOUBLE, atr DOUBLE, risk_1r DOUBLE, qty DOUBLE,
  status VARCHAR,                         -- pending (entry sent) / open / closed / cancelled
  stop_price DOUBLE, entry_fill DOUBLE, entry_time TIMESTAMP,
  exit_fill DOUBLE, exit_time TIMESTAMP, exit_how VARCHAR,
  account VARCHAR, created_at TIMESTAMP DEFAULT current_timestamp, updated_at TIMESTAMP);
CREATE TABLE IF NOT EXISTS paper_orders (
  ts TIMESTAMP DEFAULT current_timestamp, trade_id VARCHAR, action VARCHAR, detail VARCHAR,
  order_id BIGINT, account VARCHAR);
"""


def tradable(system: System) -> list[str]:
    return [m for m in system.markets if m in MARKETS]


# ---------------------------------------------------------------------------------------------------------
# Plan (pure: data frames in, actions out - tested offline)
# ---------------------------------------------------------------------------------------------------------

@dataclass
class Action:
    kind: str                 # ENTER / MOVE_STOP / EXIT / SKIP / WARN
    system: str
    symbol: str
    trade_id: str = ""
    signal_date: date | None = None
    qty: float = 0
    stop: float | None = None
    atr: float | None = None
    signal_close: float | None = None
    exit_rule: str = ""
    reason: str = ""
    extra: dict = field(default_factory=dict)


def trade_id(system: str, symbol: str, signal_date) -> str:
    return f"{system}:{symbol}:{pd.Timestamp(signal_date).date()}"


def middle_band(closes: pd.Series) -> float | None:
    return float(closes.tail(20).mean()) if len(closes) >= 20 else None


def plan(signals: pd.DataFrame, bars: pd.DataFrame, trades: pd.DataFrame, size_fn, today: date,
         max_age_days: int = 5, now: pd.Timestamp | None = None) -> list[Action]:
    """Tonight's actions.

    signals: system, symbol, signal_date, close, atr14 (recent signals from the views)
    bars:    symbol, session_date, open, high, low, close (completed sessions, enough for a 20-day average)
    trades:  rows of paper_trades with status pending/open
    size_fn: (symbol, risk_1r) -> units
    """
    out: list[Action] = []
    bars = bars.sort_values(["symbol", "session_date"])
    last_session = bars.groupby("symbol")["session_date"].max().to_dict()
    live = trades[trades["status"].isin(["pending", "open"])] if not trades.empty else trades

    # --- manage what is already on
    for t in live.itertuples():
        if t.status == "pending":
            out.append(Action("WARN", t.system, t.symbol, t.trade_id,
                              reason="entry order sent, not filled yet (waiting for the market to open?)"))
            continue
        if isinstance(getattr(t, "exit_how", None), str) and t.exit_how.startswith("exit sent"):
            out.append(Action("WARN", t.system, t.symbol, t.trade_id,
                              reason="exit order already sent, no fill recorded yet"))
            continue
        b = bars[(bars["symbol"] == t.symbol)]
        held = b[b["session_date"] > pd.Timestamp(t.signal_date)]
        k = len(held)                                   # sessions since the signal (entry day = 1)
        if k == 0:
            continue
        r = t.risk_1r
        if t.exit_rule == "B":
            mid = middle_band(b["close"])
            last_close = float(held["close"].iloc[-1])
            if mid is not None and last_close > mid:
                out.append(Action("EXIT", t.system, t.symbol, t.trade_id, qty=t.qty, exit_rule="B",
                                  reason=f"close {last_close:g} above the middle band {mid:.2f}"))
                continue
            if k >= MAX_HOLD["B"]:
                out.append(Action("EXIT", t.system, t.symbol, t.trade_id, qty=t.qty, exit_rule="B",
                                  reason=f"time limit ({k} sessions)"))
                continue
            new_stop = t.entry_fill - r
        else:
            if k >= MAX_HOLD["C2"]:
                out.append(Action("EXIT", t.system, t.symbol, t.trade_id, qty=t.qty, exit_rule="C2",
                                  reason=f"time limit ({k} sessions)"))
                continue
            new_stop = max(t.entry_fill - r, float(held["high"].max()) - r)
        if t.stop_price is None or pd.isna(t.stop_price) or abs(new_stop - t.stop_price) > 1e-9 * max(1, new_stop):
            if t.exit_rule == "C2" and t.stop_price is not None and not pd.isna(t.stop_price) \
                    and new_stop < t.stop_price and k > 1:
                continue                                # a trailing stop is never lowered after day one
            out.append(Action("MOVE_STOP", t.system, t.symbol, t.trade_id, qty=t.qty, stop=new_stop,
                              exit_rule=t.exit_rule, reason=f"was {t.stop_price}, {k} sessions held"))

    # --- new entries
    busy = {(t.system, t.symbol) for t in live.itertuples()} if not live.empty else set()
    for s in signals.sort_values("signal_date").itertuples():
        system = SYSTEMS[s.system]
        if s.symbol not in system.markets:
            continue
        last = last_session.get(s.symbol)
        if last is None or pd.Timestamp(s.signal_date) != pd.Timestamp(last):
            continue                                    # an old signal: its entry day has passed
        tid = trade_id(s.system, s.symbol, s.signal_date)
        if s.symbol not in MARKETS:
            out.append(Action("SKIP", s.system, s.symbol, tid, signal_date=s.signal_date,
                              reason="no IBKR market for this symbol"))
            continue
        if (today - pd.Timestamp(last).date()).days > max_age_days:
            out.append(Action("SKIP", s.system, s.symbol, tid, signal_date=s.signal_date,
                              reason=f"data stale (last session {pd.Timestamp(last).date()})"))
            continue
        deadline = entry_deadline(s.symbol, s.signal_date)
        if now is not None and deadline is not None and now >= deadline:
            out.append(Action("SKIP", s.system, s.symbol, tid, signal_date=s.signal_date,
                              reason=f"entry window closed at {deadline:%d %b %H:%M} UTC (data came in late)"))
            continue
        if (s.system, s.symbol) in busy:
            out.append(Action("SKIP", s.system, s.symbol, tid, signal_date=s.signal_date,
                              reason="already in a trade (one position per system per market)"))
            continue
        r = 2 * float(s.atr14)
        qty = size_fn(s.symbol, r)
        out.append(Action("ENTER", s.system, s.symbol, tid, signal_date=pd.Timestamp(s.signal_date).date(),
                          qty=qty, stop=float(s.close) - r, atr=float(s.atr14), signal_close=float(s.close),
                          exit_rule=system.rule(s.symbol),
                          reason=f"signal close {float(s.close):g}, 1R = {r:g}; provisional stop, reset to "
                                 f"fill - 1R tomorrow"))
        busy.add((s.system, s.symbol))
    return out


def units_for(risk_gbp: float, risk_1r: float, fx_to_gbp: float, max_units: float, min_units: float = 1) -> float:
    """Units so that a 1R loss is about risk_gbp. Never below the minimum: then the risk is larger (shown)."""
    per_unit = risk_1r * fx_to_gbp
    if per_unit <= 0:
        return 0
    return float(min(max_units, max(min_units, math.floor(risk_gbp / per_unit))))


# ---------------------------------------------------------------------------------------------------------
# Warehouse
# ---------------------------------------------------------------------------------------------------------

def load_inputs(con, today: date):
    con.execute(TRADES_DDL)
    since = today - timedelta(days=14)
    signals = con.execute(SIGNALS_SQL, [since, since, since]).df()
    trades = con.execute("SELECT * FROM paper_trades WHERE status IN ('pending','open')").df()
    first = min([today - timedelta(days=60)] +
                [pd.Timestamp(d).date() - timedelta(days=40) for d in trades.get("signal_date", [])])
    symbols = sorted(set(MARKETS) | set(signals["symbol"]))
    bars = con.execute(
        "SELECT symbol, session_date, open, high, low, close FROM daily_session "
        "WHERE session_date >= ? AND symbol IN (SELECT unnest(?))", [first, symbols]).df()
    return signals, bars, trades


# ---------------------------------------------------------------------------------------------------------
# Gateway
# ---------------------------------------------------------------------------------------------------------

FX_FALLBACK = {"GBP": 1.0, "USD": 0.76, "EUR": 0.85, "AUD": 0.50, "JPY": 0.0050, "HKD": 0.098}


class Broker:
    def __init__(self, send: bool):
        try:
            from dotenv import load_dotenv
            load_dotenv()
        except ImportError:
            pass
        from ib_async import IB
        self.ib = IB()
        self.send = send
        self.host = os.getenv("IBKR_HOST", "127.0.0.1")
        self.port = int(os.getenv("IBKR_PORT", "4002"))
        self.client_id = int(os.getenv("IBKR_TRADE_CLIENT_ID", "27"))
        self._contracts: dict[str, object] = {}
        self._details: dict[str, object] = {}
        self._fx: dict[str, float] = {}

    def connect(self):
        self.refused = False

        def on_error(req_id, code, msg, *rest):
            if code == 321 and "not allowed" in str(msg):
                self.refused = True
        self.ib.errorEvent += on_error
        try:
            self.ib.connect(self.host, self.port, clientId=self.client_id, readonly=not self.send, timeout=20)
        except Exception as exc:
            raise SystemExit(f"Could not reach IB Gateway at {self.host}:{self.port} ({exc}). "
                             "Start the PAPER Gateway and check IBKR_PORT (paper = 4002).") from exc
        self.ib.reqMarketDataType(3)
        self.accounts = list(self.ib.managedAccounts())
        if self.send and self.refused:
            self.ib.disconnect()
            raise SystemExit("Refusing to continue: the Gateway answered 'Trading through this Gateway is not "
                             "allowed', so it is in Read-Only mode. In IB Gateway: Configure > Settings > API > "
                             "Settings, untick 'Read-Only API', click Apply then OK (restart the Gateway if it "
                             "persists).")
        if self.send and (not self.accounts or not all(a.startswith("DU") for a in self.accounts)):
            self.ib.disconnect()
            raise SystemExit(f"Refusing to send orders: accounts {self.accounts} are not all paper (DU...). "
                             "This Gateway is logged in to the live account: log out and log in again with "
                             "'Paper Trading' selected on the IB Gateway login screen.")
        return self

    def disconnect(self):
        self.ib.disconnect()

    @property
    def account(self) -> str:
        return self.accounts[0] if self.accounts else ""

    def contract(self, symbol: str):
        if symbol in self._contracts:
            return self._contracts[symbol]
        from ib_async import Contract
        found = None
        for sec, sym, exch, cur in MARKETS.get(symbol, []):
            try:
                q = self._wait(self.ib.qualifyContractsAsync(
                    Contract(secType=sec, symbol=sym, exchange=exch, currency=cur)), f"{symbol} contract") or []
            except Exception:
                q = []
            q = [c for c in q if c is not None and getattr(c, "conId", 0)]
            if q:
                found = q[0]
                break
        self._contracts[symbol] = found
        return found

    def details(self, symbol: str):
        if symbol not in self._details:
            c = self.contract(symbol)
            d = (self._wait(self.ib.reqContractDetailsAsync(c), f"{symbol} details") or []) if c is not None else []
            self._details[symbol] = d[0] if d else None
        return self._details[symbol]

    def tick(self, symbol: str) -> float:
        d = self.details(symbol)
        return float(getattr(d, "minTick", 0) or 0.01) if d else 0.01

    def min_size(self, symbol: str) -> float:
        d = self.details(symbol)
        v = getattr(d, "minSize", None) if d else None
        return float(v) if v and v < 1e9 else 1.0

    def fx_to_gbp(self, currency: str) -> float:
        if currency == "GBP":
            return 1.0
        if currency not in self._fx:
            from ib_async import Forex
            rate = None
            # IBKR quotes each pair one way only (GBPUSD but EURGBP), so try both
            for name, invert in (("GBP" + currency, True), (currency + "GBP", False)):
                try:
                    pair = Forex(name)
                    q = self._wait(self.ib.qualifyContractsAsync(pair), f"FX {name}", 10) or []
                    q = [c for c in q if c is not None and getattr(c, "conId", 0)]
                    if not q:
                        continue
                    bars = self._wait(self.ib.reqHistoricalDataAsync(q[0], "", "3 D", "1 day", "MIDPOINT", True),
                                      f"FX {name} price", 20)
                    if bars:
                        px = float(bars[-1].close)
                        rate = 1.0 / px if invert else px
                        break
                except Exception as exc:
                    log.debug("FX %s: %s", name, exc)
            if rate is None:
                log.warning("FX GBP/%s not available - using a fixed rate", currency)
            self._fx[currency] = rate or FX_FALLBACK.get(currency, 1.0)
        return self._fx[currency]

    def round_price(self, symbol: str, price: float) -> float:
        t = self.tick(symbol)
        return round(round(price / t) * t, 10)

    # --- orders
    def _wait(self, coro, what: str, timeout: float = 20):
        """Run an IBKR request with a time limit (some requests never answer on a fresh Gateway)."""
        import asyncio
        try:
            return self.ib.run(asyncio.wait_for(coro, timeout))
        except asyncio.TimeoutError:
            log.warning("%s: no answer from IBKR in %gs - using what the connection already has", what, timeout)
            return None

    def open_orders(self) -> dict[str, object]:
        self._wait(self.ib.reqAllOpenOrdersAsync(), "open orders")
        return {t.order.orderRef: t for t in self.ib.openTrades() if (t.order.orderRef or "").startswith("helios:")}

    def fills(self) -> list:
        got = self._wait(self.ib.reqExecutionsAsync(), "executions")
        got = got if got is not None else self.ib.fills()
        return [f for f in got if (f.execution.orderRef or "").startswith("helios:")]

    def positions(self) -> dict[int, float]:
        got = self._wait(self.ib.reqPositionsAsync(), "positions")
        got = got if got is not None else self.ib.positions()
        return {p.contract.conId: float(p.position) for p in got}

    def enter(self, a: Action) -> int:
        from ib_async import MarketOrder, StopOrder
        c = self.contract(a.symbol)
        ref = f"helios:{a.trade_id}"
        parent = MarketOrder("BUY", a.qty, orderRef=ref + ":entry", tif="DAY", transmit=False,
                             account=self.account)
        parent.orderId = self.ib.client.getReqId()
        stop = StopOrder("SELL", a.qty, self.round_price(a.symbol, a.stop), orderRef=ref + ":stop", tif="GTC",
                         parentId=parent.orderId, transmit=True, account=self.account)
        self.ib.placeOrder(c, parent)
        self.ib.placeOrder(c, stop)
        self.ib.sleep(1)
        return parent.orderId

    def move_stop(self, a: Action, orders: dict) -> int | None:
        t = orders.get(f"helios:{a.trade_id}:stop")
        if t is None:
            return None
        t.order.auxPrice = self.round_price(a.symbol, a.stop)
        self.ib.placeOrder(t.contract, t.order)
        return t.order.orderId

    def exit(self, a: Action, orders: dict) -> int:
        from ib_async import MarketOrder
        t = orders.get(f"helios:{a.trade_id}:stop")
        if t is not None:
            self.ib.cancelOrder(t.order)
            self.ib.sleep(2)
        o = MarketOrder("SELL", a.qty, orderRef=f"helios:{a.trade_id}:exit", tif="DAY", account=self.account)
        self.ib.placeOrder(self.contract(a.symbol), o)
        self.ib.sleep(1)
        return o.orderId


# ---------------------------------------------------------------------------------------------------------
# Reconcile: fills -> trade book
# ---------------------------------------------------------------------------------------------------------

def apply_fills(con, fills: list) -> list[str]:
    """Update paper_trades from IBKR executions (matched by orderRef). Returns what changed."""
    notes = []
    by_leg: dict[tuple[str, str], list] = {}
    for f in fills:
        parts = f.execution.orderRef.split(":")           # helios:<sys>:<sym>:<date>:<leg>
        if len(parts) != 5:
            continue
        by_leg.setdefault((":".join(parts[1:4]), parts[4]), []).append(f)
    for (tid, leg), fs in by_leg.items():
        qty = sum(float(f.execution.shares) for f in fs)
        px = sum(float(f.execution.price) * float(f.execution.shares) for f in fs) / qty
        when = max(f.execution.time for f in fs)
        when = pd.Timestamp(when).tz_convert("UTC").tz_localize(None) if pd.Timestamp(when).tzinfo else when
        if leg == "entry":
            n = con.execute("UPDATE paper_trades SET status='open', entry_fill=?, entry_time=?, "
                            "updated_at=current_timestamp WHERE trade_id=? AND status='pending' RETURNING 1",
                            [px, when, tid]).fetchall()
            if n:
                notes.append(f"{tid}: entry filled {qty:g} @ {px:g}")
        elif leg in ("stop", "exit"):
            how = "stop" if leg == "stop" else "rule exit"
            n = con.execute("UPDATE paper_trades SET status='closed', exit_fill=?, exit_time=?, exit_how=?, "
                            "updated_at=current_timestamp WHERE trade_id=? AND status IN ('open','pending') "
                            "RETURNING 1", [px, when, how, tid]).fetchall()
            if n:
                notes.append(f"{tid}: closed by {how} {qty:g} @ {px:g}")
    return notes


def record(con, a: Action, order_id, account: str, ib_symbol: str = ""):
    con.execute("INSERT INTO paper_orders (trade_id, action, detail, order_id, account) VALUES (?,?,?,?,?)",
                [a.trade_id, a.kind, f"qty {a.qty:g} stop {a.stop} - {a.reason}", order_id, account])
    if a.kind == "ENTER":
        con.execute("INSERT INTO paper_trades (trade_id, system, symbol, ib_symbol, exit_rule, signal_date, "
                    "signal_close, atr, risk_1r, qty, status, stop_price, account, updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?, 'pending', ?, ?, current_timestamp)",
                    [a.trade_id, a.system, a.symbol, ib_symbol, a.exit_rule, a.signal_date, a.signal_close,
                     a.atr, 2 * a.atr, a.qty, a.stop, account])
    elif a.kind == "MOVE_STOP":
        con.execute("UPDATE paper_trades SET stop_price=?, updated_at=current_timestamp WHERE trade_id=?",
                    [a.stop, a.trade_id])
    elif a.kind == "EXIT":
        con.execute("UPDATE paper_trades SET exit_how=?, updated_at=current_timestamp WHERE trade_id=?",
                    [f"exit sent: {a.reason}", a.trade_id])


# ---------------------------------------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------------------------------------

def show(actions: list[Action], sizing: dict) -> str:
    if not actions:
        return "Nothing to do tonight: no new signals, no stops to move, no exits."
    lines = []
    for a in sorted(actions, key=lambda x: ("ENTER MOVE_STOP EXIT SKIP WARN".split().index(x.kind), x.system,
                                             x.symbol)):
        extra = ""
        if a.kind == "ENTER":
            s = sizing.get(a.trade_id, {})
            extra = (f" qty {a.qty:g} {s.get('ib', '')} stop {a.stop:.2f}"
                     f" ({a.exit_rule}; 1R loss about GBP {s.get('risk_gbp', float('nan')):.0f})")
        elif a.kind == "MOVE_STOP":
            extra = f" stop -> {a.stop:.2f}"
        elif a.kind == "EXIT":
            extra = f" sell {a.qty:g} at market"
        lines.append(f"{a.kind:<9} {a.system:<4} {a.symbol:<7}{extra}  [{a.reason}]")
    return "\n".join(lines)


def check_contracts(broker: Broker):
    print(f"Accounts: {broker.accounts}")
    if not all(a.startswith("DU") for a in broker.accounts):
        print("WARNING: this Gateway is logged in to a LIVE account (paper account IDs start DU). --send will refuse.")
    for sym in MARKETS:
        c = broker.contract(sym)
        if c is None:
            print(f"{sym:<7} NOT FOUND at IBKR")
            continue
        print(f"{sym:<7} {c.secType} {c.symbol} {c.currency} conId {c.conId}  tick {broker.tick(sym)}  "
              f"min size {broker.min_size(sym)}  GBP per {c.currency} {broker.fx_to_gbp(c.currency):.4f}")


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = p.add_mutually_exclusive_group()
    g.add_argument("--send", action="store_true", help="place the orders on the PAPER account")
    g.add_argument("--offline", action="store_true", help="plan from the warehouse only; no Gateway")
    g.add_argument("--check-contracts", action="store_true", help="list the IBKR contract for each market")
    p.add_argument("--refresh", action="store_true",
                   help="first download the latest Dukascopy hourly bars for the traded markets")
    p.add_argument("--any-time", action="store_true",
                   help="ignore the entry deadlines (testing only: entries may then be late)")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")

    risk_gbp = float(os.getenv("HELIOS_RISK_GBP", "250"))
    max_units = float(os.getenv("HELIOS_MAX_UNITS", "50"))
    today = datetime.now(timezone.utc).date()

    if args.send and Path("STOP_TRADING").exists():
        raise SystemExit("STOP_TRADING file present - no orders sent. Delete it to resume.")

    if args.refresh and not args.check_contracts:
        from . import dukascopy_ingest
        try:
            dukascopy_ingest.main(["--symbols", *MARKETS, "--timeframes", "1h"])
        except SystemExit as exc:                      # a failed market is reported; plan with what we have
            log.warning("refresh incomplete: %s", exc)

    broker = None if args.offline else Broker(send=args.send).connect()
    try:
        if args.check_contracts:
            check_contracts(broker)
            return
        log.info("reading the warehouse (signals, prices, trade book)...")
        con = db.connect()
        con.execute(TRADES_DDL)

        if broker is not None:
            notes = apply_fills(con, broker.fills()) if args.send else []
            for n in notes:
                log.info(n)

        signals, bars, trades = load_inputs(con, today)
        log.info("%d recent signals, %d open trades - sizing with IBKR contracts and FX...", len(signals), len(trades))
        sizing: dict[str, dict] = {}

        def size_fn(symbol, r):
            if broker is None:
                fx, ib_sym, mn = FX_FALLBACK.get(MARKETS[symbol][0][3], 1.0), MARKETS[symbol][0][1], 1.0
            else:
                c = broker.contract(symbol)
                if c is None:
                    return 0
                fx, ib_sym, mn = broker.fx_to_gbp(c.currency), c.symbol, broker.min_size(symbol)
            q = units_for(risk_gbp, r, fx, max_units, mn)
            sizing[(symbol, r)] = {"ib": ib_sym, "risk_gbp": q * r * fx}
            return q

        now = None if args.any_time else pd.Timestamp.now(tz="UTC")
        actions = plan(signals, bars, trades, size_fn, today, now=now)
        for a in actions:                                  # key sizing notes by trade id for display
            if a.kind == "ENTER":
                sizing[a.trade_id] = sizing.get((a.symbol, 2 * a.atr), {})
                if a.qty <= 0:
                    a.kind, a.reason = "SKIP", "no IBKR contract found (run --check-contracts)"

        mode = "SEND (paper)" if args.send else ("dry run, offline" if args.offline else "dry run")
        print(f"Helios IBKR plan for the next session - {mode} - risk per trade GBP {risk_gbp:g}")
        print(show(actions, sizing))

        if not args.send:
            print("\nDry run: nothing sent, nothing recorded.")
            return

        orders = broker.open_orders()
        for a in actions:
            try:
                if a.kind == "ENTER":
                    oid = broker.enter(a)
                    record(con, a, oid, broker.account, broker.contract(a.symbol).symbol)
                elif a.kind == "MOVE_STOP":
                    oid = broker.move_stop(a, orders)
                    if oid is None:
                        log.warning("%s: no working stop order found - not moved", a.trade_id)
                        continue
                    record(con, a, oid, broker.account)
                elif a.kind == "EXIT":
                    oid = broker.exit(a, orders)
                    record(con, a, oid, broker.account)
                else:
                    continue
                log.info("%s %s sent (order %s)", a.kind, a.trade_id, oid)
            except Exception as exc:                       # one bad order should not stop the rest
                log.warning("%s %s failed: %s", a.kind, a.trade_id, exc)
        log.info("DONE")
    finally:
        if broker is not None:
            broker.disconnect()


if __name__ == "__main__":
    main()
