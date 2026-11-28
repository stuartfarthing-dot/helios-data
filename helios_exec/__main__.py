"""Run Stuart's execution program once.

    python -m helios_exec                      # dry run: reads everything, places nothing, logs what it would do
    python -m helios_exec --mode demo          # trade the IG DEMO account (needs exec.control.enabled = TRUE)
    python -m helios_exec --mode demo --flatten  # close every position this program opened on demo
    python -m helios_exec --offline            # dry run without logging in to IG (wiring check)

Credentials come from .env: HX_IG_API_KEY / HX_IG_USERNAME / HX_IG_PASSWORD for the IG DEMO account (kept apart
from the IG_* live read-only settings the data job uses) and MOTHERDUCK_TOKEN. Put a file called KILL in the working folder to stop the program from doing anything.
"""

from __future__ import annotations

import argparse
import logging
import os
from datetime import timedelta

from . import plan, risk
from . import warehouse as wh
from .config import Settings

log = logging.getLogger("exec")


def run(settings: Settings, broker=None, con=None, flatten: bool = False) -> dict:
    settings.check()
    if os.path.exists(settings.kill_file):
        log.warning("KILL file present: doing nothing")
        return {"status": "killed"}
    con = con or wh.connect(settings.db_path)
    is_on, note = wh.enabled(con)
    if settings.mode == "demo" and not is_on:
        log.warning("exec.control.enabled is FALSE (%s): doing nothing", note)
        return {"status": "disabled"}
    account = broker.account if broker else "offline"
    run_id = wh.start_run(con, settings.mode, account)
    n_orders = 0
    try:
        limits = wh.limits(con)
        gbp_per_r = settings.gbp_per_r or limits.get("gbp_per_r_target", 250.0)
        sectors = wh.sectors(con)
        targets = wh.targets(con, settings.systems)
        tmap = {r["system_id"]: r for r in targets.to_dict("records")}

        # what this program holds: its journal, checked against IG
        journal = wh.holdings(con, settings.mode).to_dict("records")
        on_ig = {p["deal_id"]: p for p in (broker.positions() if broker else [])}
        held = []
        for j in journal:
            p = on_ig.get(j["deal_id"]) if broker else {"bid": None, "offer": None, "stop_level": None, "limit_level": None}
            if broker and p is None:
                stop = plan.num(j.get("last_stop"))
                pnl = (stop - j["open_level"]) * j["size"] * (1 if j["direction"] == "BUY" else -1) if stop is not None else None
                wh.record_close(con, settings.mode, j["deal_id"], "gone from IG (stop/limit hit or closed by hand)")
                wh.log_order(con, run_id, settings.mode, system_id=j["system_id"], epic=j["epic"], action="pnl",
                             deal_id=j["deal_id"], status="estimated at last stop" if pnl is not None else "unknown",
                             level=pnl, reason="position no longer on IG")
                continue
            if broker:
                wh.record_seen(con, settings.mode, j["deal_id"], p.get("stop_level"))
            t = tmap.get(j["system_id"], {})
            held.append({**j, **{k: p.get(k) for k in ("bid", "offer", "stop_level", "limit_level")},
                         "sector": sectors.get(t.get("symbol", ""), ""), "symbol": t.get("symbol", "")})
        tagged = [p for p in on_ig.values() if (p.get("deal_reference") or "").startswith(settings.tag)]
        orphans = [p for p in tagged if p["deal_id"] not in {j["deal_id"] for j in journal}]
        for o in orphans:
            log.warning("IG position %s has this program's tag but is not in the journal: left alone", o["deal_id"])

        if flatten:
            actions = [plan.Action("close", h["system_id"], h["epic"], h["direction"], h["size"], deal_id=h["deal_id"],
                                   reason="flatten requested") for h in held]
        else:
            cutoff = wh.now() - timedelta(days=settings.reentry_wait_days)
            stopped = set()
            for sid in tmap:
                when, why = wh.last_close(con, sid, settings.mode) or (None, None)
                if when and when >= cutoff and (why or "").startswith("gone"):
                    stopped.add(sid)
            need = {t["epic"] for t in tmap.values() if t["want_position"] and t["may_enter"]
                    and t["system_id"] not in {h["system_id"] for h in held}}
            markets = {e: broker.market(e) for e in need} if broker else {}
            actions = plan.build(list(tmap.values()), held, markets, gbp_per_r, wh.now(), settings.tag, stopped,
                                 wh.sizing(con))
            actions = risk.check(actions, held, limits, settings, wh.realised_today(con, settings.mode), gbp_per_r)

        for a in actions:
            row = dict(system_id=a.system_id, epic=a.epic, action=a.kind, direction=a.direction, size=a.size,
                       stop_distance=a.stop_distance, stop_level=a.stop_level, limit_distance=a.limit_distance,
                       limit_level=a.limit_level, deal_id=a.deal_id, reason=a.reason,
                       deal_reference=a.extra.get("deal_reference"))
            if a.kind == "skip" or settings.mode == "dry" or broker is None:
                wh.log_order(con, run_id, settings.mode, status="SKIPPED" if a.kind == "skip" else "DRY RUN", **row)
                log.info("%s %s: %s", a.kind.upper(), a.system_id, a.reason)
                continue
            if a.kind == "open":
                c = broker.open(a.epic, a.direction, a.size, a.stop_distance, a.limit_distance,
                                a.extra["deal_reference"], a.extra["expiry"], a.extra["currency"])
                if c["status"] == "ACCEPTED":
                    wh.record_open(con, settings.mode, c["deal_id"], a.system_id, a.epic, a.direction, a.size, c["level"])
            elif a.kind == "close":
                c = broker.close(a.deal_id, a.direction, a.size)
                if c["status"] == "ACCEPTED":
                    j = next((h for h in held if h["deal_id"] == a.deal_id), None)
                    wh.record_close(con, settings.mode, a.deal_id, a.reason)
                    if j and c.get("level") is not None:
                        pnl = (c["level"] - j["open_level"]) * j["size"] * (1 if j["direction"] == "BUY" else -1)
                        wh.log_order(con, run_id, settings.mode, system_id=a.system_id, epic=a.epic, action="pnl",
                                     deal_id=a.deal_id, status="from fill", level=pnl, reason=a.reason)
            else:
                c = broker.amend(a.deal_id, a.stop_level, a.limit_level)
            n_orders += 1
            wh.log_order(con, run_id, settings.mode, status=c["status"], level=c.get("level"),
                         **{**row, "deal_id": c.get("deal_id") or a.deal_id, "reason": f"{a.reason} | IG: {c.get('reason')}"})
            log.info("%s %s -> %s (%s)", a.kind.upper(), a.system_id, c["status"], c.get("reason"))
        wh.finish_run(con, run_id, len(tmap), n_orders, "ok", f"{len(actions)} actions, {len(orphans)} orphans")
        return {"status": "ok", "run_id": run_id, "actions": actions, "orders": n_orders}
    except Exception as e:
        wh.finish_run(con, run_id, 0, n_orders, "error", repr(e))
        raise


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--mode", default="dry", choices=["dry", "demo"])
    p.add_argument("--systems", nargs="*", help="system_ids to trade (default: H43 VIX)")
    p.add_argument("--gbp-per-r", type=float)
    p.add_argument("--offline", action="store_true", help="dry run without IG (no prices, no positions)")
    p.add_argument("--flatten", action="store_true", help="close every position this program holds")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    s = Settings(mode=args.mode, gbp_per_r=args.gbp_per_r)
    if args.systems:
        s.systems = tuple(args.systems)
    broker = None
    ig = None
    if not args.offline:
        from .broker_ig import DemoIG, IGBroker
        ig = DemoIG()                      # always the IG DEMO environment in this version
        ig.login()
        broker = IGBroker(ig, allow_orders=(args.mode == "demo"))
        log.info("Connected to %s", broker.account)
    try:
        out = run(s, broker=broker, flatten=args.flatten)
        log.info("run %s: %s", out.get("run_id"), out.get("status"))
    finally:
        if ig:
            ig.logout()


if __name__ == "__main__":
    main()
