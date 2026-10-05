"""Scheduled short ATM straddles with independently managed CE/PE protection.

Real entries are sequential: sell a leg, confirm its fill, confirm its native
stop, then sell the other leg. Durable intents are never automatically retried.
"""
import math
import time
import uuid
from datetime import datetime

from core import IST, finite, now

# Entry slots end at 14:45; protection continues until the separate 15:10 exit.
SCHEDULE = ["09:18", "09:45", "10:15", "10:45", "11:15", "11:45", "12:15", "12:45", "13:15", "13:45", "14:15", "14:45"]
EXIT_TIME = "15:10"
LOTS = {"NIFTY": 65, "SENSEX": 20}
FINAL = {"COMPLETE", "FILLED", "CANCELLED", "REJECTED"}
ACCEPTED_STOPS = {"TRIGGER PENDING", "OPEN", "COMPLETE", "FILLED"}


def up(price, tick):
    return round(math.ceil((price - 1e-9) / tick) * tick, 4)


def levels(entry, percentage, tick):
    trigger = up(entry * (1 + percentage / 100), tick)
    return trigger, up(trigger * 1.02, tick)


class Strategy:
    def __init__(self, engine, event):
        self.engine, self.event = engine, event
        self.db = engine.ledger.db
        self.db.execute("CREATE TABLE IF NOT EXISTS strategy (id TEXT PRIMARY KEY, payload TEXT NOT NULL)")
        import json
        self.runs = [json.loads(row[0]) for row in self.db.execute("SELECT payload FROM strategy")]
        for run in self.runs:
            run["enabled"] = False  # Restart never re-enables fresh entries.
        self.save()

    def save(self):
        import json
        for run in self.runs:
            self.db.execute("INSERT OR REPLACE INTO strategy VALUES (?, ?)", (run["id"], json.dumps(run, allow_nan=False)))
        self.db.commit()

    def has_open(self):
        return any(any(t["status"] not in ("CLOSED", "FAILED") for t in r["tranches"]) or r["enabled"] for r in self.runs)

    def start(self, underlying, expiry, confirmation="", automatic=False):
        e = self.engine
        if automatic and (e.mode != "dry" or e.source != "broker" or e.halted):
            return False
        if e.source not in ("demo", "broker") or e.halted:
            raise ValueError("Connect a feed and unlock order entry first.")
        if not expiry:
            raise ValueError("Load the chosen expiry first.")
        if e.mode == "live" and (not e.armed or not e.broker.ready):
            raise ValueError("Enable live orders before starting real automatic trading.")
        if any(o["status"] in ("UNKNOWN", "SUBMITTING") for o in e.ledger.orders("live")) and e.mode == "live":
            raise ValueError("Resolve uncertain live orders before starting automation.")
        today = datetime.now(IST).date().isoformat()
        run = next((r for r in self.runs if r["date"] == today and r["underlying"] == underlying and r["mode"] == e.mode and r["source"] == e.source and (e.source == "demo" or r.get("account_key") == e.account_key)), None)
        if automatic:
            # Loading the first chain may start a NEW paper session only.
            # Never resume a paused/recovered session or enter live orders.
            if e.mode != "dry" or e.source != "broker" or run:
                return False
            current = datetime.now(IST)
            if current.weekday() >= 5 or not "09:15" <= current.strftime("%H:%M") < EXIT_TIME:
                return False
        if run and run["expiry"] != expiry:
            raise ValueError("The active strategy expiry is fixed for the trading day.")
        if not run:
            run = {"id": uuid.uuid4().hex, "date": today, "mode": e.mode, "source": e.source,
                   "underlying": underlying, "expiry": expiry, "tranches": [], "enabled": False, "error": "", "account_key": e.account_key}
            self.runs.append(run)
        # Do not replay entry times missed while the app was stopped.
        run["enabled"] = True
        run["started_at"] = time.time()
        run["error"] = ""
        run["startup_pending"] = e.mode == "dry" and e.source == "broker" and not any(t["slot"] == SCHEDULE[0] for t in run["tranches"])
        self.save()
        self.event(f"{underlying} DRY TBS started. First straddle will enter on fresh ATM ticks; later entries follow the schedule." if run["startup_pending"] else f"{underlying} {e.mode.upper()} TBS started. Past entry times are not replayed.")
        return True

    def pause(self):
        for run in self.runs:
            run["enabled"] = False
        self.save()
        self.event("New TBS entries paused. Existing leg protection and scheduled exits remain active.")

    def fresh(self, key):
        quote = self.engine.quotes.get(key, {})
        return quote if time.time() - quote.get("received", 0) <= 15 and quote.get("source") == self.engine.source else None

    def pair(self, run):
        spot = self.fresh("index|" + run["underlying"])
        if not spot:
            raise ValueError("Waiting for a fresh underlying index tick.")
        step = 50 if run["underlying"] == "NIFTY" else 100
        strike = math.floor(spot["ltp"] / step + .5) * step
        options = [i for i in self.engine.instruments.values() if i["underlying"] == run["underlying"] and i["expiry"] == run["expiry"] and i["strike"] == strike]
        pair = [next((i for i in options if i["option_type"] == side), None) for side in ("CE", "PE")]
        if not all(pair):
            raise ValueError(f"ATM {strike} is outside the loaded chain. Reload the chain before the next entry.")
        for inst in pair:
            if inst["lot_size"] != LOTS[run["underlying"]] or inst["tick_size"] <= 0:
                raise ValueError("Broker lot/tick metadata differs from the configured strategy; entry blocked.")
            if not self.fresh(inst["key"]):
                raise ValueError("Waiting for fresh ATM option ticks.")
            if self.fresh(inst["key"])["ltp"] * inst["lot_size"] > self.engine.max_order_value:
                raise ValueError("ATM leg premium exceeds the per-order limit of ₹25,000.")
        return pair

    async def new_tranche(self, run, slot, percentage, startup=False):
        pair = self.pair(run)
        tranche = {"slot": slot, "sl": percentage, "strike": pair[0]["strike"], "time": now(),
                   "startup": startup, "status": "ENTERING", "legs": [], "exit_requested": False}
        for inst in pair:
            tranche["legs"].append({"instrument": inst.copy(), "entry_id": None, "stop_id": None,
                                    "exit_id": None, "entry": None, "exit": None, "status": "WAITING",
                                    "qty": inst["lot_size"], "scenarios": {}})
        run["tranches"].append(tranche)
        self.save()
        await self.advance(run, tranche)

    def order(self, order_id):
        return next((o for o in self.engine.ledger.orders() if o["id"] == order_id), None)

    async def submit(self, run, leg, role, side, qty, kind="MKT", price=0, trigger=0):
        inst = leg["instrument"]
        order_id = uuid.uuid4().hex
        leg[role + "_id"] = order_id
        self.save()
        order = {"id": order_id, "mode": run["mode"], "source": run["source"], "strategy": run["id"],
                 "option_type": inst["option_type"], "account_key": run.get("account_key"),
                 "key": inst["key"], "symbol": inst["symbol"], "underlying": inst["underlying"],
                 "side": side, "qty": qty, "price": price, "trigger": trigger, "order_type": kind,
                 "time": now(), "created": time.time(), "status": "SUBMITTING", "filled_qty": 0}
        self.engine.ledger.save(order)
        if run["mode"] == "dry":
            quote = self.fresh(inst["key"])
            if not quote:
                order["status"] = "REJECTED"
            else:
                fill = up(quote["ltp"] * 1.0005, inst["tick_size"]) if side == "B" else round(math.floor(quote["ltp"] * .9995 / inst["tick_size"]) * inst["tick_size"], 4)
                order.update(status="FILLED", fill_price=fill, filled_qty=qty)
        else:
            try:
                response = await self.engine.broker.strategy_order(inst, side, qty, kind, price, trigger, order_id)
                if isinstance(response, dict) and str(response.get("stat", "")).lower() == "ok" and response.get("nOrdNo"):
                    order.update(status="SUBMITTED", broker_id=str(response["nOrdNo"]))
                    # An order-feed event can race the REST acknowledgement.
                    streamed = self.order(order_id)
                    if streamed and streamed["status"] != "SUBMITTING":
                        order.update(streamed)
                elif isinstance(response, dict) and str(response.get("stat", "")).lower() in ("not_ok", "not ok"):
                    order["status"] = "REJECTED"
                else:
                    order["status"] = "UNKNOWN"
            except Exception:
                order["status"] = "UNKNOWN"
            if order["status"] == "UNKNOWN":
                self.fail(run, "Uncertain broker order. Reconcile in the broker before continuing; no retry was sent.")
        self.engine.ledger.save(order)
        self.save()
        return order

    def fail(self, run, message):
        run["enabled"] = False
        run["error"] = message
        self.engine.disarm()
        self.event(message, True)
        self.save()

    def initialize_leg(self, leg, order, percentage):
        leg["entry"] = order["fill_price"]
        leg["qty"] = order["filled_qty"]
        leg["trigger"], leg["limit"] = levels(leg["entry"], percentage, leg["instrument"]["tick_size"])
        leg["scenarios"] = {str(p): {"triggered": False, "exit": None} for p in range(10, 101, 10)}
        leg["status"] = "OPEN"

    async def request_cancel(self, run, order):
        if order.get("cancel_requested") or order["status"] in FINAL:
            return
        if not order.get("broker_id"):
            self.fail(run, "Cannot cancel an uncertain order without its broker ID. Manual reconciliation required.")
            return
        order["cancel_requested"] = True
        self.engine.ledger.save(order)
        try:
            await asyncio.to_thread(self.engine.broker.client.cancel_order, order_id=order["broker_id"])
            await self.engine.broker.reconcile()
        except Exception:
            self.fail(run, "Cancellation outcome is uncertain. No replacement order was sent; reconcile with the broker.")

    async def advance(self, run, tranche):
        live = run["mode"] == "live"
        if live and not self.engine.broker.management_ready:
            return
        for leg in tranche["legs"]:
            inst = leg["instrument"]
            entry = self.order(leg["entry_id"])
            if not entry:
                if leg["entry_id"]:
                    self.fail(run, "Recovered an incomplete order intent; manual reconciliation required.")
                    return
                if tranche["exit_requested"] or not run["enabled"] or self.engine.halted or (live and not self.engine.armed):
                    leg["status"] = "SKIPPED"
                    tranche["exit_requested"] = True
                    continue
                if not self.fresh(inst["key"]):
                    return
                entry = await self.submit(run, leg, "entry", "S", leg["qty"])
            if entry["status"] in ("UNKNOWN", "SUBMITTING"):
                return
            if entry["status"] not in FINAL:
                # Cancel an incomplete entry before covering its confirmed fills.
                if time.time() - entry.get("created", time.time()) > 15 or tranche["exit_requested"]:
                    tranche["exit_requested"] = True
                    await self.request_cancel(run, entry)
                return
            if not entry.get("filled_qty"):
                leg["status"] = "SKIPPED"
                tranche["exit_requested"] = True
                continue
            if leg["entry"] is None:
                self.initialize_leg(leg, entry, tranche["sl"])
                self.save()
            if live and not leg["stop_id"] and not tranche["exit_requested"]:
                stop = await self.submit(run, leg, "stop", "B", leg["qty"], "SL", leg["limit"], leg["trigger"])
                if stop["status"] == "REJECTED":
                    tranche["exit_requested"] = True
                    self.fail(run, "Broker rejected a protective stop. Closing confirmed strategy fills.")
                else:
                    return
            if live and leg["stop_id"]:
                stop = self.order(leg["stop_id"])
                if not stop or stop["status"] in ("UNKNOWN", "SUBMITTING"):
                    return
                if stop["status"] in ("REJECTED", "CANCELLED") and not tranche["exit_requested"]:
                    tranche["exit_requested"] = True
                    self.fail(run, "Protective stop is no longer active. Closing the remaining strategy quantity.")
                if stop.get("filled_qty", 0) >= leg["qty"]:
                    leg["exit"], leg["status"] = stop.get("fill_price"), "STOPPED"
                elif not tranche["exit_requested"] and stop["status"] not in ACCEPTED_STOPS:
                    if time.time() - stop.get("created", time.time()) > 30:
                        self.fail(run, "Protective stop has not been confirmed. Reconcile the broker order book immediately.")
                    return
            quote = self.fresh(inst["key"])
            if quote:
                self.update_scenarios(leg, quote["ltp"], tranche["exit_requested"])
            if not live and leg["exit"] is None and quote:
                if quote["ltp"] >= leg["trigger"]:
                    leg["triggered"] = True
                buy = up(quote["ltp"] * 1.0005, inst["tick_size"])
                if tranche["exit_requested"] or (leg.get("triggered") and buy <= leg["limit"]):
                    exit_order = await self.submit(run, leg, "exit", "B", leg["qty"])
                    if exit_order["status"] == "FILLED":
                        leg["exit"] = exit_order["fill_price"]
                        leg["status"] = "CLOSED" if tranche["exit_requested"] else "STOPPED"
                elif leg.get("triggered"):
                    leg["status"] = "SL LIMIT OPEN"
            if live and tranche["exit_requested"] and leg["exit"] is None:
                await self.exit_live(run, leg)
        done = all(l["exit"] is not None or l["status"] == "SKIPPED" for l in tranche["legs"])
        tranche["status"] = "CLOSED" if done else "EXITING" if tranche["exit_requested"] else "OPEN" if all(l["entry"] is not None for l in tranche["legs"]) else "ENTERING"
        self.save()

    async def exit_live(self, run, leg):
        stop = self.order(leg["stop_id"])
        if stop and stop["status"] not in FINAL:
            await self.request_cancel(run, stop)
            return  # Wait for a confirmed terminal status before replacement.
        covered = stop.get("filled_qty", 0) if stop else 0
        remaining = leg["qty"] - covered
        if remaining <= 0:
            leg["exit"], leg["status"] = stop.get("fill_price"), "STOPPED"
            return
        if not leg["exit_id"]:
            await self.submit(run, leg, "exit", "B", remaining)
            return
        exit_order = self.order(leg["exit_id"])
        if not exit_order:
            self.fail(run, "Exit intent needs manual broker reconciliation.")
            return
        if exit_order.get("filled_qty", 0) == remaining:
            leg["exit"] = (covered * (stop.get("fill_price", 0) if stop else 0) + remaining * exit_order["fill_price"]) / leg["qty"]
            leg["status"] = "CLOSED"
        elif exit_order["status"] in ("REJECTED", "CANCELLED", "UNKNOWN") or time.time() - exit_order.get("created", time.time()) > 30:
            self.fail(run, "Strategy exit is incomplete. Check the broker immediately; automatic resubmission is blocked.")

    def update_scenarios(self, leg, ltp, closing=False):
        for percentage, scenario in leg["scenarios"].items():
            if scenario["exit"] is not None:
                continue
            trigger, limit = levels(leg["entry"], int(percentage), leg["instrument"]["tick_size"])
            if ltp >= trigger:
                scenario["triggered"] = True
            buy = up(ltp * 1.0005, leg["instrument"]["tick_size"])
            if closing or (scenario["triggered"] and buy <= limit):
                scenario["exit"] = buy

    def observe_tick(self, key, quote):
        """Latch every received tick, including crosses between UI refreshes."""
        if time.time() - quote.get("received", 0) > 15:
            return
        changed = False
        for run in self.runs:
            if run["source"] != quote.get("source") or run["mode"] != self.engine.mode:
                continue
            for tranche in run["tranches"]:
                for leg in tranche["legs"]:
                    if leg["instrument"]["key"] != key or leg["entry"] is None:
                        continue
                    previous = [(v["triggered"], v["exit"]) for v in leg["scenarios"].values()]
                    self.update_scenarios(leg, quote["ltp"], tranche["exit_requested"])
                    changed = changed or previous != [(v["triggered"], v["exit"]) for v in leg["scenarios"].values()]
                    if run["mode"] != "dry" or leg["exit"] is not None:
                        continue
                    if quote["ltp"] >= leg["trigger"]:
                        changed = changed or not leg.get("triggered")
                        leg["triggered"] = True
                    fill = up(quote["ltp"] * 1.0005, leg["instrument"]["tick_size"])
                    if tranche["exit_requested"] or (leg.get("triggered") and fill <= leg["limit"]):
                        order_id = uuid.uuid4().hex
                        leg.update(exit_id=order_id, exit=fill, status="CLOSED" if tranche["exit_requested"] else "STOPPED")
                        changed = True
                        inst = leg["instrument"]
                        self.engine.ledger.save({"id": order_id, "mode": "dry", "source": run["source"], "strategy": run["id"],
                            "key": key, "symbol": inst["symbol"], "underlying": inst["underlying"], "side": "B", "qty": leg["qty"],
                            "price": leg["limit"], "time": now(), "status": "FILLED", "filled_qty": leg["qty"], "fill_price": fill})
                    elif leg.get("triggered"):
                        leg["status"] = "SL LIMIT OPEN"
        if changed:
            self.save()

    async def step(self, current=None):
        current = current or datetime.now(IST)
        for run in self.runs:
            if run["source"] != self.engine.source or run["mode"] != self.engine.mode:
                continue
            # Retain exact instruments needed to manage positions after a process restart.
            for tranche in run["tranches"]:
                for leg in tranche["legs"]:
                    self.engine.instruments.setdefault(leg["instrument"]["key"], leg["instrument"])
            close_time = run["source"] != "demo" and (current.date().isoformat() > run["date"] or (current.date().isoformat() == run["date"] and current.strftime("%H:%M") >= EXIT_TIME))
            for tranche in run["tranches"]:
                # Even after actual stop-outs, continue all counterfactual heatmap paths until exit.
                if close_time:
                    tranche["exit_requested"] = True
                await self.advance(run, tranche)
            if close_time:
                run["enabled"] = False
            if run["source"] == "demo":
                self.save()
                continue  # Demo uses explicit "next tranche" actions, never the wall clock.
            if not run["enabled"] or self.engine.halted or (run["mode"] == "live" and (not self.engine.armed or not self.engine.broker.ready)):
                continue
            if run["date"] != current.date().isoformat() or (current.weekday() >= 5 and run["source"] == "broker"):
                continue
            if run.get("startup_pending") and run["mode"] == "dry" and "09:15" <= current.strftime("%H:%M") < EXIT_TIME:
                if any(t["slot"] == SCHEDULE[0] for t in run["tranches"]):
                    run["startup_pending"] = False
                else:
                    try:
                        await self.new_tranche(run, SCHEDULE[0], 20, startup=True)
                        run["startup_pending"] = False
                        # Later tranches must be future slots, even if ticks arrived late.
                        run["started_at"] = current.timestamp()
                        run["startup_at"] = current.timestamp()
                        run["error"] = ""
                        self.event(f"{run['underlying']} first dry straddle entered at {current.strftime('%H:%M:%S')} IST with 20% leg stops.")
                    except ValueError as exc:
                        run["error"] = str(exc)
                        await self.refresh_chain(run, str(exc))
                    self.save()
                    continue
            for index, slot in enumerate(SCHEDULE):
                if index == 0 and run.get("startup_pending"):
                    continue
                scheduled = current.replace(hour=int(slot[:2]), minute=int(slot[3:]), second=0, microsecond=0)
                if 0 <= (current - scheduled).total_seconds() < 60 and scheduled.timestamp() >= run["started_at"] - 1 and scheduled.timestamp() > run.get("startup_at", 0) and not any(t["slot"] == slot for t in run["tranches"]):
                    try:
                        await self.new_tranche(run, slot, 20 if index == 0 else 30)
                        run["error"] = ""
                    except ValueError as exc:
                        run["error"] = str(exc)
                        await self.refresh_chain(run, str(exc))
            self.save()

    async def refresh_chain(self, run, message):
        if "outside the loaded chain" in message and run["source"] == "broker" and time.time() - run.get("refreshed_at", 0) > 15:
            run["refreshed_at"] = time.time()
            contracts = await self.engine.broker.chain(run["underlying"], run["expiry"])
            if len(set(self.engine.instruments) | set(contracts)) <= 400:
                await self.engine.broker.subscribe(list(contracts))
                self.engine.instruments.update(contracts)

    async def close_all(self):
        self.pause()
        for run in self.runs:
            if run["mode"] != self.engine.mode or run["source"] != self.engine.source:
                continue
            for tranche in run["tranches"]:
                tranche["exit_requested"] = True
                await self.advance(run, tranche)
        self.save()

    def view(self, underlying):
        today = datetime.now(IST).date().isoformat()
        runs = [r for r in self.runs if r["date"] == today and r["underlying"] == underlying and r["mode"] == self.engine.mode and r["source"] == self.engine.source]
        run = runs[-1] if runs else None
        tranches = []
        if run:
            for t in run["tranches"]:
                row = {k: t[k] for k in ("slot", "sl", "strike", "status")}
                row.update(time=t["time"], startup=t.get("startup", False))
                row["legs"], row["scenarios"] = [], {}
                row["pnl"] = 0
                row["entry"] = sum(l["entry"] or 0 for l in t["legs"])
                row["ltp"] = 0
                for leg in t["legs"]:
                    quote = self.fresh(leg["instrument"]["key"])
                    mark = leg["exit"] if leg["exit"] is not None else quote["ltp"] if quote else None
                    pnl = (leg["entry"] - mark) * leg["qty"] if mark is not None and leg["entry"] is not None else None
                    row["legs"].append({"side": leg["instrument"]["option_type"], "entry": leg["entry"], "ltp": quote["ltp"] if quote else None,
                                         "exit": leg["exit"], "qty": leg["qty"], "trigger": leg.get("trigger"), "limit": leg.get("limit"), "status": leg["status"], "pnl": pnl})
                    row["pnl"] = row["pnl"] + pnl if row["pnl"] is not None and pnl is not None else None
                    row["ltp"] = row["ltp"] + quote["ltp"] if quote and row["ltp"] is not None else None
                for p in range(10, 101, 10):
                    values = []
                    for leg in t["legs"]:
                        scenario = leg["scenarios"].get(str(p), {})
                        q = self.fresh(leg["instrument"]["key"])
                        mark = scenario.get("exit") if scenario.get("exit") is not None else q["ltp"] if q else None
                        values.append(leg["entry"] - mark if leg["entry"] is not None and mark is not None else None)
                    row["scenarios"][str(p)] = sum(values) if all(v is not None for v in values) else None
                tranches.append(row)
        current = datetime.now(IST)
        next_slot = next((slot for slot in SCHEDULE if slot > current.strftime("%H:%M") and not any(t["slot"] == slot for t in tranches)), None)
        return {"enabled": bool(run and run["enabled"]), "error": run["error"] if run else "", "schedule": SCHEDULE,
                "startup_pending": bool(run and run["enabled"] and run.get("startup_pending")), "next_entry": next_slot if run and run["enabled"] else None,
                "exit_time": EXIT_TIME, "tranches": tranches, "configured": True,
                "lot_size": LOTS[underlying], "open": self.has_open()}


import asyncio
