"""Paper ledger and guarded order routing, independent of the UI and SDK."""
import json
import math
import sqlite3
import time
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")


def now():
    return datetime.now(IST).isoformat(timespec="seconds")


def finite(value, name="value"):
    try:
        result = float(value)
    except (ValueError, TypeError):
        raise ValueError(f"Invalid {name}.") from None
    if not math.isfinite(result):
        raise ValueError(f"Invalid {name}.")
    return result


class Ledger:
    def __init__(self, path):
        # Runtime access is serialized on the application event loop; TestClient
        # runs that loop on its portal thread rather than the import thread.
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute("CREATE TABLE IF NOT EXISTS orders (id TEXT PRIMARY KEY, payload TEXT NOT NULL)")
        self.db.commit()

    def save(self, order):
        self.db.execute("INSERT OR REPLACE INTO orders VALUES (?, ?)",
                        (order["id"], json.dumps(order, allow_nan=False)))
        self.db.commit()

    def orders(self, mode=None):
        items = [json.loads(row[0]) for row in self.db.execute("SELECT payload FROM orders ORDER BY rowid")]
        return [o for o in items if mode is None or o["mode"] == mode]

    def positions(self, quotes, source, realized_day=None):
        positions = {}
        for order in self.orders("dry"):
            if order.get("source") != source or order["status"] != "FILLED":
                continue
            key = order["key"]
            p = positions.setdefault(key, {"key": key, "symbol": order["symbol"], "qty": 0,
                                          "average": 0., "realized": 0., "underlying": order["underlying"]})
            delta = order["qty"] * (1 if order["side"] == "B" else -1)
            old = p["qty"]
            price = order["fill_price"]
            if old == 0 or old * delta > 0:
                p["average"] = (abs(old) * p["average"] + abs(delta) * price) / (abs(old) + abs(delta))
            else:
                # Keep cost basis and carried quantity, but scope displayed profits to today.
                if realized_day is None or order.get("time", "")[:10] == realized_day:
                    p["realized"] += min(abs(old), abs(delta)) * (price - p["average"]) * (1 if old > 0 else -1)
                if abs(delta) > abs(old):
                    p["average"] = price
                elif abs(delta) == abs(old):
                    p["average"] = 0
            p["qty"] += delta
        for key, p in positions.items():
            quote = quotes.get(key, {})
            p["ltp"] = quote.get("ltp")
            p["stale"] = time.time() - quote.get("received", 0) > 15
            p["unrealized"] = None if p["qty"] and p["ltp"] is None else ((p["ltp"] or 0) - p["average"]) * p["qty"]
        return list(positions.values())


class Engine:
    def __init__(self, ledger, broker):
        self.ledger, self.broker = ledger, broker
        self.mode = "dry"
        self.source = "offline"
        self.armed = False
        self.halted = False
        self.account_key = None
        self.instruments = {}
        self.quotes = {}
        self.max_lots = 1
        self.max_order_value = 25000.

    def disarm(self):
        self.armed = False

    def arm(self, phrase=None):
        if self.mode != "live" or self.source != "broker" or not self.broker.ready:
            raise ValueError("Connect the broker and select live mode first.")
        if self.halted:
            raise ValueError("Release the order lock first.")
        if any(o["status"] in ("SUBMITTING", "UNKNOWN") for o in self.ledger.orders("live")):
            raise ValueError("An earlier live order needs reconciliation before live orders can be armed.")
        self.armed = True

    def set_mode(self, mode):
        if mode not in ("dry", "live"):
            raise ValueError("Unknown execution mode.")
        self.mode = mode
        self.disarm()

    async def order(self, request):
        key = request.get("key", "")
        client_id = request.get("client_id", "")
        if not isinstance(client_id, str) or not 8 <= len(client_id) <= 100:
            raise ValueError("An order request ID is required.")
        previous = next((o for o in self.ledger.orders() if o["id"] == client_id), None)
        if previous:
            return previous
        if request.get("mode") != self.mode:
            raise ValueError("Execution mode changed. Review the ticket again.")
        if self.halted:
            raise ValueError("Order entry is locked.")
        inst = self.instruments.get(key)
        quote = self.quotes.get(key)
        if not inst or not quote:
            raise ValueError("Choose a subscribed option with a current quote.")
        if time.time() - quote.get("received", 0) > 15 or quote.get("source") != self.source:
            raise ValueError("Quote is stale. Wait for a fresh market update.")
        if self.source == "broker" and not self.broker.market_connected:
            raise ValueError("The market WebSocket is disconnected.")
        side = request.get("side")
        if side not in ("B", "S"):
            raise ValueError("Choose Buy or Sell.")
        lots = request.get("lots")
        if type(lots) is not int or not 1 <= lots <= self.max_lots:
            raise ValueError(f"Lots must be between 1 and {self.max_lots}.")
        if not inst.get("lot_size") or not inst.get("tick_size"):
            raise ValueError("Broker lot/tick metadata is unavailable; this contract cannot be traded.")
        qty = lots * inst["lot_size"]
        price = finite(request.get("price"), "limit price")
        if price <= 0 or price * qty > self.max_order_value:
            raise ValueError(f"Enter a positive price; order premium is limited to ₹{self.max_order_value:,.0f}.")
        tick = inst["tick_size"]
        if abs(price / tick - round(price / tick)) > 0.00001:
            raise ValueError(f"Price must follow the broker tick size of {tick}.")
        order = {"id": client_id, "mode": self.mode, "source": self.source, "key": key,
                 "option_type": inst["option_type"], "account_key": self.account_key,
                 "symbol": inst["symbol"], "underlying": inst["underlying"], "side": side,
                 "qty": qty, "price": price, "time": now(), "status": "SUBMITTING", "filled_qty": 0}
        if self.mode == "dry":
            # Only marketable limit orders are simulated; no fictional pending fills.
            ltp = finite(quote["ltp"])
            fill = math.ceil(ltp * 1.0005 / tick) * tick if side == "B" else math.floor(ltp * .9995 / tick) * tick
            if (side == "B" and price < fill) or (side == "S" and price > fill):
                raise ValueError("Paper limit is not marketable after 0.05% simulated slippage. Adjust the limit price.")
            order.update(status="FILLED", fill_price=round(fill, 4), filled_qty=qty)
            self.ledger.save(order)
            return order
        if not self.armed or not self.broker.ready or self.source != "broker":
            raise ValueError("Live order entry is disarmed or broker reconciliation is unavailable.")
        current = datetime.now(IST)
        if current.weekday() >= 5 or not "09:15" <= current.strftime("%H:%M") < "15:30":
            raise ValueError("Live orders are limited to regular weekday market hours in IST.")
        if any(o["status"] in ("SUBMITTING", "UNKNOWN") for o in self.ledger.orders("live")):
            self.disarm()
            raise ValueError("Reconcile the uncertain live order before placing another.")
        self.ledger.save(order)  # Durable intent before the network request.
        try:
            result = await self.broker.place(inst, side, qty, price, client_id)
            if isinstance(result, dict) and str(result.get("stat", "")).lower() == "ok" and result.get("nOrdNo"):
                order.update(status="SUBMITTED", broker_id=str(result["nOrdNo"]))
                streamed = next((o for o in self.ledger.orders() if o["id"] == client_id), None)
                if streamed and streamed["status"] != "SUBMITTING":
                    order.update(streamed)
            else:
                # Broker failure responses can be ambiguous. Never retry automatically.
                order["status"] = "UNKNOWN"
                self.disarm()
        except Exception:
            order["status"] = "UNKNOWN"
            self.disarm()
        self.ledger.save(order)
        return order
