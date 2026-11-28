"""Pre-trade checks. Closing and tightening stops are always allowed; only new risk is checked."""

from __future__ import annotations

from .config import Settings
from .plan import Action


def check(actions: list[Action], held: list[dict], limits: dict[str, float], settings: Settings,
          realised_today_gbp: float, gbp_per_r: float) -> list[Action]:
    """Returns the actions with blocked opens turned into skips (reason says why)."""
    open_r = {"total": float(len(held))}
    by_sector: dict[str, float] = {}
    by_market: dict[str, float] = {}
    for h in held:
        by_sector[h.get("sector", "")] = by_sector.get(h.get("sector", ""), 0) + 1
        by_market[h.get("symbol", "")] = by_market.get(h.get("symbol", ""), 0) + 1
    daily_limit_r = limits.get("daily_loss_limit_r", -6.0)
    lost_r = realised_today_gbp / gbp_per_r if gbp_per_r else 0.0
    orders = 0
    out: list[Action] = []
    for a in actions:
        if a.kind in ("close", "amend"):
            orders += 1
            out.append(a)
            continue
        if a.kind != "open":
            out.append(a)
            continue
        why = None
        if orders >= settings.max_orders_per_run:
            why = f"order cap per run ({settings.max_orders_per_run}) reached"
        elif a.stop_distance is None or a.size * a.stop_distance > settings.max_risk_gbp_per_order:
            why = (f"risk £{a.size * (a.stop_distance or 0):.0f} above the hard cap £{settings.max_risk_gbp_per_order:.0f}"
                   if a.stop_distance is not None else "no stop distance: an order without a stop is never sent")
        elif a.size * float(a.extra.get("price") or 0) > settings.max_notional_gbp_per_order:
            why = f"exposure £{a.size * float(a.extra['price']):,.0f} above the hard cap £{settings.max_notional_gbp_per_order:,.0f}"
        elif len(held) + sum(1 for x in out if x.kind == "open") >= settings.max_open_positions:
            why = f"open-position cap {settings.max_open_positions} reached"
        elif lost_r <= daily_limit_r:
            why = f"daily loss limit reached ({lost_r:.1f}R vs {daily_limit_r}R): no new entries today"
        elif open_r["total"] + 1 > limits.get("max_open_r_total", 20):
            why = f"total open risk would exceed {limits.get('max_open_r_total')}R"
        elif by_sector.get(a.sector, 0) + 1 > limits.get("max_open_r_sector", 8):
            why = f"{a.sector} would exceed {limits.get('max_open_r_sector')}R"
        elif by_market.get(a.symbol, 0) + 1 > limits.get("max_open_r_market", 4):
            why = f"{a.symbol} would exceed {limits.get('max_open_r_market')}R"
        if why:
            out.append(Action("skip", a.system_id, a.epic, a.direction, a.size, sector=a.sector, symbol=a.symbol,
                              reason="blocked by risk check: " + why))
            continue
        orders += 1
        open_r["total"] += 1
        by_sector[a.sector] = by_sector.get(a.sector, 0) + 1
        by_market[a.symbol] = by_market.get(a.symbol, 0) + 1
        out.append(a)
    return out
