"""Manage already-filled one-lot manual paper legs through the local API.

This optional helper reads an explicit private job file; it does not enter new
trades, run live orders, or adopt manual positions into the TBS heatmap.
"""
import argparse
import json
import math
import time
import urllib.request
import uuid
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")


def up(value, tick):
    return round(math.ceil((value - 1e-9) / tick) * tick, 4)


def read(base):
    with urllib.request.urlopen(base + "/api/bootstrap", timeout=5) as response:
        return json.load(response)


def save(path, job):
    path.write_text(json.dumps(job, indent=2), encoding="utf-8")


def manage(path, base="http://127.0.0.1:8765"):
    job = json.loads(path.read_text(encoding="utf-8"))
    while any(not leg.get("closed") for leg in job["legs"]):
        try:
            bootstrap = read(base)
            state = bootstrap["state"]
            # The API also checks mode on each order, covering a switch after this read.
            if state["mode"] != "dry" or state["source"] != "broker":
                raise ValueError("Waiting for broker data in dry mode.")
            orders = {o["id"]: o for o in state["orders"]}
            contracts = {c["key"]: c for c in state["contracts"]}
            # Position quotes remain available when the user selects another market.
            for position in state["positions"]:
                contracts.setdefault(position["key"], position)
            closing = datetime.now(IST) >= datetime.fromisoformat(job["exit_at"])
            for leg in job["legs"]:
                if leg.get("closed"):
                    continue
                quote = contracts.get(leg["key"])
                if not quote or quote.get("stale"):
                    continue
                price = quote["ltp"]
                # A gap latches the stop; wait for a fill within the limit on a pullback.
                leg["triggered"] = leg.get("triggered", False) or price >= leg["trigger"]
                buy_fill = up(price * 1.0005, leg["tick_size"])
                if not (closing or (leg["triggered"] and buy_fill <= leg["limit"])):
                    continue
                if not leg.get("exit_id"):
                    # Persist before POST so a lost response reuses the same paper order.
                    leg["exit_id"] = uuid.uuid4().hex
                    save(path, job)
                existing = orders.get(leg["exit_id"])
                if existing:
                    order = existing
                else:
                    payload = {"client_id": leg["exit_id"], "mode": "dry", "key": leg["key"], "side": "B", "lots": 1,
                               "price": up(price * 1.002, leg["tick_size"]) if closing else leg["limit"]}
                    request = urllib.request.Request(base + "/api/order", data=json.dumps(payload).encode(),
                              headers={"Content-Type": "application/json", "Origin": base,
                                       "X-Terminal-Token": bootstrap["token"]}, method="POST")
                    with urllib.request.urlopen(request, timeout=10) as response:
                        order = json.load(response)["order"]
                if order["mode"] != "dry" or order["status"] != "FILLED" or order["filled_qty"] != leg["qty"]:
                    raise ValueError("Paper close is not confirmed.")
                leg.update(closed=True, exit_price=order["fill_price"], exit_time=order["time"], reason="15:10 exit" if closing else "30% stop")
                print(leg["symbol"] + " paper close confirmed.", flush=True)
            job["error"] = ""
            job["checked_at"] = state["time"]
            save(path, job)
        except Exception as exc:
            job["error"] = type(exc).__name__ + ": " + str(exc)
            save(path, job)
        time.sleep(.25)
    job["status"] = "CLOSED"
    save(path, job)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("job", type=Path)
    args = parser.parse_args()
    manage(args.job)
