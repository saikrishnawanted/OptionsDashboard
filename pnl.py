"""Observed intraday P&L for terminal fills, isolated by feed, mode and account."""
import json
import math
import time
from datetime import timedelta

from core import IST


def totals(orders, instruments, quotes, day, mode, source, underlying, account=None):
    positions = {}
    for order in sorted(orders, key=lambda o: o.get("time", "")):
        if (order.get("mode") != mode or order.get("source") != source
                or order.get("underlying") != underlying or order.get("time", "")[:10] != day
                or (order.get("account_key") and order["account_key"] != account)):
            continue
        qty, price = order.get("filled_qty", 0), order.get("fill_price")
        if not qty or price is None or not math.isfinite(float(price)):
            continue
        inst = instruments.get(order["key"], {})
        side = order.get("option_type") or inst.get("option_type")
        if side not in ("CE", "PE"):
            side = next((s for s in ("CE", "PE") if order.get("symbol", "").endswith(s)), None)
        if not side:
            continue
        p = positions.setdefault(order["key"], {"qty": 0, "average": 0, "realized": 0, "side": side})
        delta = qty * (1 if order["side"] == "B" else -1)
        old = p["qty"]
        if not old or old * delta > 0:
            p["average"] = (abs(old) * p["average"] + qty * price) / (abs(old) + qty)
        else:
            p["realized"] += min(abs(old), qty) * (price - p["average"]) * (1 if old > 0 else -1)
            if qty > abs(old):
                p["average"] = price
            elif qty == abs(old):
                p["average"] = 0
        p["qty"] += delta
    groups = {side: {"realized": 0, "unrealized": 0, "open": 0} for side in ("CE", "PE")}
    for key, p in positions.items():
        g = groups[p["side"]]
        g["realized"] += p["realized"]
        if p["qty"]:
            g["open"] += 1
            q = quotes.get(key, {})
            # Unavailable marks are gaps, never zero P&L or carried-forward prices.
            price = q.get("ltp")
            fresh = q.get("source") == source and time.time() - q.get("received", 0) <= 15
            if not fresh or price is None or not math.isfinite(float(price)):
                g["unrealized"] = None
            elif g["unrealized"] is not None:
                g["unrealized"] += (price - p["average"]) * p["qty"]
    groups["OVERALL"] = {"realized": sum(g["realized"] for g in groups.values()),
        "unrealized": None if any(g["unrealized"] is None for g in groups.values()) else sum(g["unrealized"] for g in groups.values()),
        "open": sum(g["open"] for g in groups.values())}
    for g in groups.values():
        g["total"] = None if g["unrealized"] is None else g["realized"] + g["unrealized"]
    return groups, bool(positions)


class PnlHistory:
    def __init__(self, ledger):
        self.ledger = ledger
        self.db = ledger.db
        self.db.execute("CREATE TABLE IF NOT EXISTS pnl_history (scope TEXT, stamp INTEGER, payload TEXT NOT NULL, PRIMARY KEY(scope, stamp))")
        # Daily results outlive the detailed chart's 30-day retention window.
        self.db.execute("CREATE TABLE IF NOT EXISTS daily_performance (scope TEXT PRIMARY KEY, stamp INTEGER, payload TEXT NOT NULL)")
        self.db.commit()
        self.last = {}
        self.cleaned = None

    @staticmethod
    def scope(current, mode, source, underlying, account):
        return json.dumps([current.date().isoformat(), mode, source, underlying, account if source == "broker" else None])

    def sample(self, engine, current):
        if engine.source not in ("broker", "demo"):
            return
        stamp = int(current.timestamp()) // 5 * 5
        for mode in (("dry", "live") if engine.source == "broker" else ("dry",)):
            for underlying in ("NIFTY", "SENSEX"):
                scope = self.scope(current, mode, engine.source, underlying, engine.account_key)
                if self.last.get(scope) == stamp:
                    continue
                values, has_fills = totals(self.ledger.orders(mode), engine.instruments, engine.quotes,
                    current.date().isoformat(), mode, engine.source, underlying, engine.account_key)
                if has_fills:
                    self.db.execute("INSERT OR REPLACE INTO pnl_history VALUES (?, ?, ?)",
                        (scope, stamp, json.dumps(values, allow_nan=False)))
                    self.db.execute("INSERT OR REPLACE INTO daily_performance VALUES (?, ?, ?)",
                        (scope, stamp, json.dumps(values, allow_nan=False)))
                self.last[scope] = stamp
        if self.cleaned != current.date():
            self.db.execute("DELETE FROM pnl_history WHERE stamp < ?", (int((current - timedelta(days=30)).timestamp()),))
            self.cleaned = current.date()
        self.db.commit()

    def view(self, engine, current, underlying):
        scope = self.scope(current, engine.mode, engine.source, underlying, engine.account_key)
        points = [{"time": stamp, **json.loads(payload)} for stamp, payload in self.db.execute(
            "SELECT stamp, payload FROM pnl_history WHERE scope=? ORDER BY stamp", (scope,))]
        values, _ = totals(self.ledger.orders(engine.mode), engine.instruments, engine.quotes,
            current.date().isoformat(), engine.mode, engine.source, underlying, engine.account_key)
        return {"date": current.date().isoformat(), "mode": engine.mode, "source": engine.source,
                "underlying": underlying, "points": points, "current": values}

    def performance(self, engine, current):
        """Backfill closed legacy days from fills, never invent old open marks."""
        today = current.date().isoformat()
        groups = {}
        for order in self.ledger.orders():
            if not order.get("filled_qty") or order.get("fill_price") is None:
                continue
            account = order.get("account_key")
            if engine.account_key and account and account != engine.account_key:
                continue
            key = (order["time"][:10], order["mode"], order["source"], order["underlying"], account)
            groups.setdefault(key, []).append(order)
        saved = {tuple(json.loads(scope)): (stamp, json.loads(payload)) for scope, stamp, payload in
                 self.db.execute("SELECT scope, stamp, payload FROM daily_performance")}
        # Recover existing chart samples created before the daily archive existed.
        for scope, stamp, payload in self.db.execute("SELECT scope, stamp, payload FROM pnl_history ORDER BY stamp"):
            key = tuple(json.loads(scope))
            if key not in saved or stamp > saved[key][0]:
                saved[key] = (stamp, json.loads(payload))
        rows = []
        for key in set(groups) | set(saved):
            day, mode, source, underlying, account = key
            if engine.account_key and account and account != engine.account_key:
                continue
            quotes = engine.quotes if day == today and source == engine.source else {}
            values, has = totals(groups.get(key, []), engine.instruments, quotes, day, mode, source, underlying, account)
            stamp, observed = saved.get(key, (None, None))
            if day != today and observed and (not has or values["OVERALL"]["open"]):
                values = observed
            elif has:
                stamp = int(current.timestamp()) if day == today else stamp
            status = "Today" if day == today else "Closed" if values["OVERALL"]["open"] == 0 else "Last observed / open"
            if day < today:
                self.db.execute("INSERT OR REPLACE INTO daily_performance VALUES (?, ?, ?)",
                    (json.dumps(list(key)), stamp, json.dumps(values, allow_nan=False)))
            rows.append({"date": day, "mode": mode, "source": source, "underlying": underlying,
                         "values": values, "status": status, "observed_at": stamp})
        self.db.commit()
        return {"today": today, "rows": sorted(rows, key=lambda r: (r["date"], r["underlying"], r["mode"]), reverse=True)}
