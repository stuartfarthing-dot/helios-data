"""Reads targets and limits from the warehouse; writes the program's own journal (schema exec)."""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone

import duckdb
import pandas as pd


def connect(db_path: str | None = None) -> duckdb.DuckDBPyConnection:
    if db_path:
        return duckdb.connect(db_path)
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass
    if not os.getenv("MOTHERDUCK_TOKEN"):
        raise SystemExit("MOTHERDUCK_TOKEN not set (put it in .env)")
    con = duckdb.connect("md:")
    con.execute("USE helios")
    return con


def now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)


def enabled(con) -> tuple[bool, str]:
    row = con.execute("SELECT enabled, note FROM exec.control ORDER BY updated_utc DESC LIMIT 1").fetchone()
    return (bool(row[0]), row[1] or "") if row else (False, "no exec.control row")


def targets(con, systems: tuple[str, ...]) -> pd.DataFrame:
    df = con.execute("SELECT * FROM live.exec_targets").df()
    return df[df["system_id"].isin(systems)].reset_index(drop=True)


def limits(con) -> dict[str, float]:
    return {r[0]: float(r[1]) for r in con.execute("SELECT limit_name, value FROM ref.risk_limits").fetchall()}


def sizing(con) -> dict[str, dict]:
    """Per-system sizing decisions (exec.sizing): a fixed £/pt stake or a £ per R, set by Stuart at each step."""
    try:
        rows = con.execute("SELECT system_id, fixed_stake, gbp_per_r FROM exec.sizing").fetchall()
    except duckdb.CatalogException:
        return {}
    return {r[0]: {"fixed_stake": r[1], "gbp_per_r": r[2]} for r in rows}


def sectors(con) -> dict[str, str]:
    return dict(con.execute("SELECT symbol, sector FROM ref.sectors").fetchall())


def holdings(con, mode: str) -> pd.DataFrame:
    return con.execute("SELECT * FROM exec.holdings WHERE mode = ? AND closed_utc IS NULL", [mode]).df()


def last_close(con, system_id: str, mode: str):
    return con.execute("SELECT max(closed_utc), arg_max(close_reason, closed_utc) FROM exec.holdings "
                       "WHERE system_id = ? AND mode = ? AND closed_utc IS NOT NULL", [system_id, mode]).fetchone()


def realised_today(con, mode: str) -> float:
    """£ realised today by this program (closed deals' P&L recorded in exec.orders as 'pnl')."""
    row = con.execute("SELECT coalesce(sum(level), 0) FROM exec.orders WHERE mode = ? AND action = 'pnl' "
                      "AND CAST(ts_utc AS DATE) = current_date", [mode]).fetchone()
    return float(row[0])


def start_run(con, mode: str, account: str) -> str:
    run_id = f"{now():%Y%m%d%H%M%S}-{uuid.uuid4().hex[:6]}"
    con.execute("INSERT INTO exec.runs (run_id, started_utc, mode, account, status) VALUES (?, ?, ?, ?, 'running')",
                [run_id, now(), mode, account])
    return run_id


def finish_run(con, run_id: str, n_targets: int, n_orders: int, status: str, note: str = "") -> None:
    con.execute("UPDATE exec.runs SET finished_utc = ?, targets = ?, orders = ?, status = ?, note = ? WHERE run_id = ?",
                [now(), n_targets, n_orders, status, note[:2000], run_id])


def log_order(con, run_id: str, mode: str, **o) -> None:
    cols = ["system_id", "epic", "action", "direction", "size", "stop_distance", "stop_level", "limit_distance",
            "limit_level", "deal_reference", "deal_id", "status", "level", "reason"]
    con.execute(f"INSERT INTO exec.orders (run_id, ts_utc, mode, {', '.join(cols)}) VALUES (?, ?, ?, {', '.join('?' * len(cols))})",
                [run_id, now(), mode] + [o.get(c) for c in cols])


def record_open(con, mode: str, deal_id: str, system_id: str, epic: str, direction: str, size: float, level) -> None:
    con.execute("INSERT INTO exec.holdings (deal_id, system_id, epic, direction, size, open_level, opened_utc, mode) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)", [deal_id, system_id, epic, direction, size, level, now(), mode])


def record_close(con, mode: str, deal_id: str, reason: str) -> None:
    con.execute("UPDATE exec.holdings SET closed_utc = ?, close_reason = ? WHERE deal_id = ? AND mode = ? AND closed_utc IS NULL",
                [now(), reason, deal_id, mode])


def record_seen(con, mode: str, deal_id: str, stop_level) -> None:
    """Remember the last stop seen on IG, so a position that disappears can be valued at its stop."""
    con.execute("UPDATE exec.holdings SET last_stop = ? WHERE deal_id = ? AND mode = ? AND closed_utc IS NULL",
                [stop_level, deal_id, mode])
