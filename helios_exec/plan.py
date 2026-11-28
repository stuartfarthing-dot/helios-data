"""Turns targets (what each system should hold) and IG positions (what is held) into a list of actions.

Pure functions: no network, no database. Every rule about *what* to do lives here; risk.py decides
whether it is *allowed*; broker_ig.py is the only place that sends anything.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class Action:
    kind: str                     # open | close | amend | skip
    system_id: str
    epic: str
    direction: str = "BUY"
    size: float = 0.0
    stop_distance: float | None = None
    limit_distance: float | None = None
    stop_level: float | None = None
    limit_level: float | None = None
    deal_id: str | None = None
    sector: str = ""
    symbol: str = ""
    reason: str = ""
    extra: dict = field(default_factory=dict)


def num(x) -> float | None:
    """A number, or None for None/NaN (pandas gives NaN for missing values)."""
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    return None if f != f else f


def code(system_id: str) -> str:
    return hashlib.sha1(system_id.encode()).hexdigest()[:6].upper()


def deal_reference(tag: str, system_id: str, when: datetime) -> str:
    return f"{tag}{code(system_id)}{when:%y%m%d%H%M%S}"          # 2 + 6 + 12 = 20 characters (IG allows 30)


def stake(gbp_per_r: float, risk_1r_ig: float, min_size: float) -> tuple[float, str]:
    """£ per point so that one R (2 ATR in IG points) risks gbp_per_r. Rounded down to 0.01."""
    raw = gbp_per_r / risk_1r_ig
    s = int(raw * 100) / 100
    if s < min_size:
        return min_size, f"stake {raw:.4f} below IG minimum {min_size}: using {min_size} ({min_size * risk_1r_ig:.0f} £/R)"
    return s, ""


def build(targets: list[dict], held: list[dict], markets: dict[str, dict], gbp_per_r: float,
          now: datetime, tag: str = "HX", recently_stopped: set[str] | None = None,
          sizing: dict[str, dict] | None = None) -> list[Action]:
    """targets: rows of live.exec_targets; held: this program's open positions on IG, each with system_id.
    markets: epic -> broker.market() dict. recently_stopped: system_ids waiting out a re-entry pause.
    sizing: system_id -> {"fixed_stake": £/pt or None, "gbp_per_r": £ or None} (exec.sizing); default gbp_per_r."""
    recently_stopped = recently_stopped or set()
    sizing = sizing or {}
    by_system = {h["system_id"]: h for h in held}
    actions: list[Action] = []
    for t in targets:
        sid, epic = t["system_id"], t["epic"]
        h = by_system.get(sid)
        m = markets.get(epic) or {}
        base = dict(system_id=sid, epic=epic, direction=t["direction"], sector=t.get("sector") or "", symbol=t["symbol"])
        if not t["want_position"]:
            if h:
                actions.append(Action("close", **base, size=h["size"], deal_id=h["deal_id"],
                                      reason=f"rule says flat ({t['source_status']})"))
            continue
        if h is None:
            if not t["may_enter"]:
                actions.append(Action("skip", **base, reason="trade already running before this program held it; not joined late"))
                continue
            if sid in recently_stopped:
                actions.append(Action("skip", **base, reason="waiting after a stop before re-entry"))
                continue
            if m.get("status") != "TRADEABLE":
                actions.append(Action("skip", **base, reason=f"market not open ({m.get('status')}); try next run"))
                continue
            price = m["offer"] if t["direction"] == "BUY" else m["bid"]
            if t["stop_mode"] == "pct":
                stop_dist = round(float(t["stop_pct"]) * price, 2)
                limit_dist = None
            else:
                stop_dist = float(t["stop_distance_ig"])
                limit_dist = num(t.get("limit_distance_ig"))
            if m.get("min_stop") and stop_dist < m["min_stop"]:
                actions.append(Action("skip", **base, reason=f"stop {stop_dist} closer than IG's minimum {m['min_stop']}"))
                continue
            rule = sizing.get(sid) or {}
            fixed = num(rule.get("fixed_stake"))
            if fixed is not None:
                size, note = max(fixed, m.get("min_size", 0.01)), f"fixed stake £{fixed}/pt (exec.sizing)"
            else:
                size, note = stake(num(rule.get("gbp_per_r")) or gbp_per_r, stop_dist, m.get("min_size", 0.01))
            actions.append(Action("open", **base, size=size, stop_distance=stop_dist, limit_distance=limit_dist,
                                  reason=f"rule says enter ({t['source_status']})" + (f"; {note}" if note else ""),
                                  extra={"deal_reference": deal_reference(tag, sid, now), "expiry": m.get("expiry", "-"),
                                         "currency": m.get("currency", "GBP"), "risk_gbp": size * stop_dist,
                                         "price": price}))
            continue
        # held and wanted: keep; trail the stop up (longs) when the rule's stop has risen
        sbc = num(t.get("stop_below_close_ig"))
        if t["stop_mode"] == "distance" and sbc is not None and h.get("bid") is not None:
            new_stop = round(float(h["bid"]) - float(sbc), 2) if t["direction"] == "BUY" else None
            cur = h.get("stop_level")
            if new_stop is not None and (cur is None or new_stop > float(cur) + 1e-9) and new_stop < float(h["bid"]):
                actions.append(Action("amend", **base, deal_id=h["deal_id"], stop_level=new_stop,
                                      limit_level=h.get("limit_level"), reason=f"trail stop {cur} -> {new_stop}"))
    return actions
