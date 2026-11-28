"""Settings. Hard caps live here, in code, so a warehouse change can tighten but never loosen them."""

from __future__ import annotations

import os
from dataclasses import dataclass, field

MODES = ("dry", "demo")          # "live" is deliberately absent in version 0.1


@dataclass
class Settings:
    mode: str = "dry"
    # which systems this program trades; ProOrder keeps running the rest
    systems: tuple[str, ...] = ("H43 VIX",)
    gbp_per_r: float | None = None            # None = ref.risk_limits gbp_per_r_target
    # hard caps (code-level; the warehouse limits can only be stricter)
    max_risk_gbp_per_order: float = 400.0     # stake x stop distance: no single order may risk more than £400
    max_notional_gbp_per_order: float = 25000.0   # stake x price: guards against a wrong (too tight) stop
    max_orders_per_run: int = 10
    max_open_positions: int = 30
    min_stake: float = 0.01
    reentry_wait_days: int = 7                # H43: 5 sessions after a stop ~ 7 calendar days
    kill_file: str = "KILL"                   # a file with this name in the working folder stops everything
    tag: str = "HX"                           # prefix of every deal reference this program creates
    db_path: str | None = field(default_factory=lambda: os.getenv("HELIOS_EXEC_DB"))  # local file for tests

    def check(self) -> None:
        if self.mode not in MODES:
            raise SystemExit(f"Mode {self.mode!r} is not allowed in this version (allowed: {', '.join(MODES)}). "
                             "Live trading needs a reviewed later version.")
        if self.max_risk_gbp_per_order > 1000 or self.max_notional_gbp_per_order > 100000 or self.max_orders_per_run > 25:
            raise SystemExit("Hard caps can't be raised that far in this version (£1,000 risk, £100,000 notional, 25 orders).")
