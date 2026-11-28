"""IG broker adapter. The ONLY module in Helios that can send an order.

It refuses to work unless it is connected to IG's demo environment (version 0.1).
"""

from __future__ import annotations

import logging
import time

log = logging.getLogger("exec.ig")


class IGBroker:
    def __init__(self, ig, allow_orders: bool):
        """ig: a logged-in helios_data.ig_ingest.IG. allow_orders=False makes every order method refuse."""
        self.ig = ig
        self.allow_orders = allow_orders
        if allow_orders and "demo-api" not in ig.base:
            raise SystemExit("Refusing to trade: not connected to IG's DEMO environment (set IG_ACC_TYPE=DEMO "
                             "and use the demo API key). Live trading is not enabled in this version.")
        if allow_orders and getattr(ig, "account_type", "SPREADBET") != "SPREADBET":
            raise SystemExit(f"Refusing to trade: the session is on a {ig.account_type} account; the systems' "
                             "markets are spread bets (DFB). Set HX_IG_ACCOUNT_ID to the demo spread bet account.")

    @property
    def account(self) -> str:
        env = "IG DEMO" if "demo-api" in self.ig.base else "IG LIVE (read-only)"
        acc = getattr(self.ig, "account_id", None)
        return f"{env} {getattr(self.ig, 'account_type', '')} {acc or ''}".strip()

    # --- reads -------------------------------------------------------------------------------------------------
    def market(self, epic: str) -> dict:
        m = self.ig._req("GET", f"/markets/{epic}", "3").json()
        snap, inst, rules = m.get("snapshot") or {}, m.get("instrument") or {}, m.get("dealingRules") or {}
        return {
            "epic": epic, "status": snap.get("marketStatus"), "bid": snap.get("bid"), "offer": snap.get("offer"),
            "expiry": inst.get("expiry") or "-",
            "currency": ((inst.get("currencies") or [{}])[0]).get("code", "GBP"),
            "min_size": float(((rules.get("minDealSize") or {}).get("value")) or 0.01),
            "min_stop": float(((rules.get("minNormalStopOrLimitDistance") or {}).get("value")) or 0),
        }

    def positions(self) -> list[dict]:
        out = []
        for item in self.ig._req("GET", "/positions", "2").json().get("positions", []):
            p, m = item.get("position") or {}, item.get("market") or {}
            out.append({"deal_id": p.get("dealId"), "deal_reference": p.get("dealReference"), "epic": m.get("epic"),
                        "direction": p.get("direction"), "size": p.get("size"), "level": p.get("level"),
                        "stop_level": p.get("stopLevel"), "limit_level": p.get("limitLevel"),
                        "bid": m.get("bid"), "offer": m.get("offer")})
        return out

    def confirm(self, deal_reference: str, tries: int = 5) -> dict:
        for i in range(tries):
            try:
                c = self.ig._req("GET", f"/confirms/{deal_reference}", "1").json()
                return {"status": c.get("dealStatus"), "deal_id": c.get("dealId"), "level": c.get("level"),
                        "reason": c.get("reason")}
            except RuntimeError:
                time.sleep(1 + i)
        return {"status": "UNKNOWN", "deal_id": None, "level": None, "reason": "no confirmation"}

    # --- orders (refused unless allow_orders) ------------------------------------------------------------------
    def _guard(self):
        if not self.allow_orders:
            raise RuntimeError("orders are not allowed in this mode")

    def open(self, epic: str, direction: str, size: float, stop_distance: float, limit_distance: float | None,
             deal_reference: str, expiry: str, currency: str) -> dict:
        self._guard()
        body = {"epic": epic, "expiry": expiry, "direction": direction, "size": round(size, 2), "orderType": "MARKET",
                "currencyCode": currency, "forceOpen": True, "guaranteedStop": False,
                "stopDistance": round(stop_distance, 2), "limitDistance": round(limit_distance, 2) if limit_distance else None,
                "dealReference": deal_reference}
        self.ig._req("POST", "/positions/otc", "2", json=body)
        return self.confirm(deal_reference)

    def close(self, deal_id: str, direction: str, size: float) -> dict:
        self._guard()
        opposite = "SELL" if direction == "BUY" else "BUY"
        r = self.ig.s.request("POST", self.ig.base + "/positions/otc", timeout=30,
                              headers={"Version": "1", "_method": "DELETE"},
                              json={"dealId": deal_id, "direction": opposite, "size": round(size, 2), "orderType": "MARKET"})
        if not r.ok:
            return {"status": "REJECTED", "deal_id": deal_id, "level": None, "reason": f"{r.status_code}: {r.text[:200]}"}
        return self.confirm(r.json().get("dealReference"))

    def amend(self, deal_id: str, stop_level: float | None, limit_level: float | None) -> dict:
        self._guard()
        r = self.ig._req("PUT", f"/positions/otc/{deal_id}", "2",
                         json={"stopLevel": stop_level, "limitLevel": limit_level, "trailingStop": False})
        return self.confirm(r.json().get("dealReference"))


class DemoIG:
    """A minimal IG session that can only ever talk to IG's DEMO environment.

    It uses its own settings (HX_IG_API_KEY, HX_IG_USERNAME, HX_IG_PASSWORD in .env), separate from the
    IG_* settings the nightly data job uses for the live account, so the two can never be mixed up.
    """

    base = "https://demo-api.ig.com/gateway/deal"

    def __init__(self):
        import os

        import requests
        try:
            from dotenv import load_dotenv
            load_dotenv()
        except ImportError:
            pass
        missing = [k for k in ("HX_IG_API_KEY", "HX_IG_USERNAME", "HX_IG_PASSWORD") if not os.getenv(k)]
        if missing:
            raise SystemExit(f"Missing demo settings in .env: {', '.join(missing)} (an IG DEMO account and its API key)")
        self._user, self._pw = os.environ["HX_IG_USERNAME"], os.environ["HX_IG_PASSWORD"]
        self.s = requests.Session()
        self.s.headers.update({"X-IG-API-KEY": os.environ["HX_IG_API_KEY"],
                               "Content-Type": "application/json; charset=UTF-8",
                               "Accept": "application/json; charset=UTF-8"})

    def _req(self, method, path, version="1", **kw):
        r = self.s.request(method, self.base + path, headers={"Version": version}, timeout=30, **kw)
        if not r.ok:
            raise RuntimeError(f"IG {method} {path} -> {r.status_code}: {r.text}")
        return r

    def login(self):
        import os
        r = self._req("POST", "/session", "2", json={"identifier": self._user, "password": self._pw})
        self.s.headers["CST"] = r.headers["CST"]
        self.s.headers["X-SECURITY-TOKEN"] = r.headers["X-SECURITY-TOKEN"]
        body = r.json()
        want = choose_account(body.get("accounts") or [], body.get("currentAccountId"), os.getenv("HX_IG_ACCOUNT_ID"))
        if want["accountId"] != body.get("currentAccountId"):
            s = self._req("PUT", "/session", "1", json={"accountId": want["accountId"], "defaultAccount": False})
            if s.headers.get("X-SECURITY-TOKEN"):
                self.s.headers["X-SECURITY-TOKEN"] = s.headers["X-SECURITY-TOKEN"]
        self.account_id, self.account_type = want["accountId"], want.get("accountType")

    def logout(self):
        try:
            self._req("DELETE", "/session")
        except Exception:
            pass


def choose_account(accounts: list[dict], current: str | None, wanted_id: str | None) -> dict:
    """IG demo logins hold a spread bet and a CFD account. Pick HX_IG_ACCOUNT_ID if set, otherwise the
    spread bet account (the systems trade DFB spread bets). Never silently fall back to a CFD account."""
    if wanted_id:
        for a in accounts:
            if a.get("accountId") == wanted_id:
                return a
        raise SystemExit(f"HX_IG_ACCOUNT_ID {wanted_id} is not one of this login's accounts: "
                         + ", ".join(f"{a.get('accountId')} ({a.get('accountType')})" for a in accounts))
    bets = [a for a in accounts if a.get("accountType") == "SPREADBET"]
    if not bets:
        raise SystemExit("No spread bet account on this IG demo login: "
                         + ", ".join(f"{a.get('accountId')} ({a.get('accountType')})" for a in accounts))
    cur = [a for a in bets if a.get("accountId") == current]
    return cur[0] if cur else bets[0]
