import asyncio
import csv
import io
import hashlib
import json
import math
import os
import secrets
import sys
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, os.environ.get("NEO_SDK_PATH", str(ROOT / "vendor" / "kotak_neo")))
LOCAL = Path(os.environ.get("TERMINAL_DATA_DIR", str(ROOT / ".local")))
LOCAL.mkdir(parents=True, exist_ok=True)
os.environ["NEO_SCRIP_CACHE_DIR"] = str(LOCAL / "scrip_cache")

from broker import Broker
from core import Engine, IST, Ledger, finite, now
from vault import Vault
from strategy import Strategy
from pnl import PnlHistory
from remote import RemoteAccess

events = []
engine = None
selection = {"underlying": "SENSEX", "expiry": ""}
demo_task = None
lock = asyncio.Lock()
session_token = secrets.token_urlsafe(32)
vault = Vault(LOCAL / "credentials.dpapi")
ledger = Ledger(LOCAL / "terminal.sqlite3")
pnl_history = PnlHistory(ledger)


def event(message, disarm=False):
    if disarm and engine:
        engine.disarm()
    if not events or events[-1]["message"] != message:
        events.append({"time": now(), "message": message})
        del events[:-100]


def tick(key, quote):
    if engine.source == "broker":
        if quote.get("origin") == "snapshot" and time.time() - engine.quotes.get(key, {}).get("received", 0) <= 15:
            return
        engine.quotes[key] = quote
        strategy.observe_tick(key, quote)


def order_update(row):
    orders = [o for o in ledger.orders("live") if o.get("account_key") == engine.account_key]
    matched = any(str(row.get("nOrdNo", "")) == o.get("broker_id") or row.get("GuiOrdId") == o["id"] for o in orders)
    if not matched and engine.account_key and row.get("nOrdNo"):
        symbol = str(row.get("trdSym") or row.get("sym") or "")
        underlying = next((u for u in ("NIFTY", "SENSEX") if symbol.startswith(u)), None)
        option = next((s for s in ("CE", "PE") if symbol.endswith(s)), None)
        exchange, token = row.get("exSeg"), row.get("tok")
        stamp = str(row.get("ordDtTm") or "")
        parsed = None
        for fmt in ("%d-%b-%Y %H:%M:%S", "%d/%m/%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
            try:
                parsed = datetime.strptime(stamp, fmt).replace(tzinfo=IST)
                break
            except ValueError:
                pass
        if parsed is None:
            try:
                parsed = datetime.fromisoformat(stamp)
                parsed = parsed.replace(tzinfo=IST) if parsed.tzinfo is None else parsed.astimezone(IST)
            except ValueError:
                return  # Do not invent a trading date for an incomplete update.
        if not underlying or not option or not exchange or not token or row.get("trnsTp") not in ("B", "S"):
            return
        order = {"id": f"broker:{engine.account_key}:{row['nOrdNo']}", "broker_id": str(row["nOrdNo"]),
                 "mode": "live", "source": "broker", "account_key": engine.account_key,
                 "key": f"{exchange}|{token}", "symbol": symbol, "underlying": underlying,
                 "option_type": option, "side": row["trnsTp"], "qty": int(finite(row.get("qty", 0))),
                 "time": parsed.isoformat(timespec="seconds"), "status": "SUBMITTED", "filled_qty": 0,
                 "external": True}
        orders.append(order)
    for order in orders:
        if str(row.get("nOrdNo", "")) != order.get("broker_id") and row.get("GuiOrdId") != order["id"]:
            continue
        if row.get("nOrdNo"):
            order["broker_id"] = str(row["nOrdNo"])
        incoming = str(row.get("ordSt", row.get("stat", ""))).upper()
        # Ignore delayed lower-fill snapshots and do not regress terminal states.
        filled = int(finite(row.get("fldQty", order.get("filled_qty", 0))))
        if filled < order.get("filled_qty", 0):
            continue
        terminal = {"COMPLETE", "CANCELLED", "REJECTED"}
        if incoming and (order["status"] not in terminal or incoming in terminal):
            order["status"] = incoming
        order["filled_qty"] = filled
        if row.get("avgPrc") is not None:
            order["fill_price"] = finite(row["avgPrc"])
        ledger.save(order)


broker = Broker(tick, order_update, event)
engine = Engine(ledger, broker)
strategy = Strategy(engine, event)
strategy_task = None
dry_startup_available = True


async def stop_demo():
    global demo_task
    if demo_task:
        demo_task.cancel()
        await asyncio.gather(demo_task, return_exceptions=True)
        demo_task = None


async def demo_feed():
    start = time.time()
    while True:
        t = time.time() - start
        for name, center in (("SENSEX", 74600), ("NIFTY", 24800)):
            engine.quotes["index|" + name] = {"ltp": center + 15 * math.sin(t / 30), "received": time.time(), "source": "demo"}
        for i, (key, inst) in enumerate(engine.instruments.items()):
            center = 74600 if inst["underlying"] == "SENSEX" else 24800
            distance = inst["strike"] - center
            intrinsic = max(0, -distance if inst["option_type"] == "CE" else distance)
            base = 135 * math.exp(-abs(distance) / (600 if center == 74600 else 300)) + intrinsic
            ltp = round(max(.05, base + 7 * math.sin(t / 15 + i / 2)), 2)
            engine.quotes[key] = {"ltp": ltp, "received": time.time(), "source": "demo",
                                  "change": round(4 * math.sin(t / 20 + i), 2), "oi": 42000 + i * 1730, "volume": 180000 + i * 8120}
            strategy.observe_tick(key, engine.quotes[key])
        await asyncio.sleep(1)


def snapshot():
    if engine.armed and not broker.ready:
        engine.disarm()
    today = datetime.now(IST).date().isoformat()
    positions = ledger.positions(engine.quotes, engine.source, realized_day=today) if engine.mode == "dry" else []
    today_keys = {o["key"] for o in ledger.orders(engine.mode) if o.get("time", "")[:10] == today and o.get("source") == engine.source}
    positions = [p for p in positions if p["qty"] or p["key"] in today_keys]
    from pnl import totals
    daily = [totals(ledger.orders(engine.mode), engine.instruments, engine.quotes, today,
             engine.mode, engine.source, market, engine.account_key)[0]["OVERALL"] for market in ("NIFTY", "SENSEX")]
    session_pnl = {"date": today, "realized": sum(v["realized"] for v in daily),
                   "unrealized": None if any(v["unrealized"] is None for v in daily) else sum(v["unrealized"] for v in daily),
                   "open": sum(v["open"] for v in daily)}
    contracts = []
    for key, inst in engine.instruments.items():
        if inst["underlying"] != selection["underlying"] or inst["expiry"] != selection["expiry"]:
            continue
        quote = engine.quotes.get(key, {})
        contracts.append({**inst, **quote, "stale": time.time() - quote.get("received", 0) > 15})
    return {"time": now(), "mode": engine.mode, "source": engine.source, "armed": engine.armed,
            "halted": engine.halted, "connected": broker.market_connected, "authenticated": broker.authenticated,
            "broker_ready": broker.ready, "connection_error": broker.reconciliation_error,
            "saved_credentials": vault.path.exists(), "selection": selection,
            "contracts": contracts, "positions": positions, "session_pnl": session_pnl,
            "orders": [o for o in ledger.orders(engine.mode) if o["time"][:10] == today][-100:][::-1],
            "broker_positions": [{k: row.get(k) for k in ("trdSym", "exSeg", "prod", "cfBuyQty", "cfSellQty", "flBuyQty", "flSellQty", "buyAmt", "sellAmt")}
                                 for row in broker.positions],
            "events": events[-12:][::-1], "strategy": strategy.view(selection["underlying"]),
            "limits": {"max_lots": engine.max_lots, "max_order_value": engine.max_order_value}}


@asynccontextmanager
async def lifespan(app):
    global strategy_task
    event("Terminal started in dry mode. A new paper TBS session starts after login and chain loading; recovered sessions stay paused.")
    strategy_task = asyncio.create_task(strategy_loop())
    yield
    strategy_task.cancel()
    await asyncio.gather(strategy_task, return_exceptions=True)
    await stop_demo()
    await broker.close()


async def strategy_loop():
    while True:
        try:
            async with lock:
                await strategy.step()
        except asyncio.CancelledError:
            raise
        except Exception:
            strategy.pause()
            event("Strategy management encountered an error. Check broker positions immediately.", True)
        try:
            async with lock:
                pnl_history.sample(engine, datetime.now(IST))
        except Exception:
            # Analytics must never pause protection or trading management.
            event("P&L history could not be recorded. Current positions remain available.")
        await asyncio.sleep(1)


remote_access = RemoteAccess(os.environ.get("TERMINAL_REMOTE_ORIGIN", ""), os.environ.get("TERMINAL_PROXY_TOKEN", ""))
app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "testserver"] + ([remote_access.host] if remote_access.host else []))


def local_origin(origin, host):
    return remote_access.allowed_origin(origin, host)


@app.middleware("http")
async def protect(request: Request, call_next):
    if not remote_access.authorized_proxy(request.headers, request.client.host if request.client else None):
        return JSONResponse({"error": "Sign in through the configured HTTPS gateway."}, status_code=403)
    if request.method not in ("GET", "HEAD"):
        if not local_origin(request.headers.get("origin"), request.headers.get("host", "")):
            return JSONResponse({"error": "Trading changes must come from the configured terminal address."}, status_code=403)
        if not secrets.compare_digest(request.headers.get("x-terminal-token", ""), session_token):
            return JSONResponse({"error": "Reload the terminal to renew its session."}, status_code=403)
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    return response


@app.get("/")
async def index():
    return FileResponse(ROOT / "static" / "index.html")


@app.get("/api/bootstrap")
async def bootstrap():
    return {"token": session_token, "state": snapshot()}


@app.get("/api/health")
async def health():
    return {"application": "OptionsDashboard", "workspace": str(ROOT), "data_directory": str(LOCAL.resolve())}


@app.get("/api/pnl")
async def pnl_view():
    async with lock:
        current = datetime.now(IST)
        return pnl_history.view(engine, current, selection["underlying"])


@app.get("/api/performance")
async def daily_performance():
    async with lock:
        return pnl_history.performance(engine, datetime.now(IST))


@app.get("/api/export")
async def export():
    stream = io.StringIO()
    fields = ["time", "mode", "source", "id", "broker_id", "symbol", "side", "qty", "price", "status", "filled_qty", "fill_price"]
    writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    for order in ledger.orders():
        writer.writerow({k: ("'" + str(v) if isinstance(v, str) and v.startswith(("=", "+", "-", "@")) else v) for k, v in order.items()})
    return Response(stream.getvalue(), media_type="text/csv", headers={"Content-Disposition": 'attachment; filename="terminal-orders.csv"'})


@app.post("/api/{action}")
async def action(action: str, request: Request):
    global demo_task, dry_startup_available
    try:
        raw = await request.body()
        if len(raw) > 16000:
            raise ValueError("Request is too large.")
        data = json.loads(raw or b"{}")
        if not isinstance(data, dict):
            raise ValueError("Invalid request.")
        async with lock:
            if action == "connect":
                if any(r["source"] == "demo" and (r["enabled"] or any(t["status"] not in ("CLOSED", "FAILED") for t in r["tranches"])) for r in strategy.runs):
                    raise ValueError("Exit the demo TBS session before switching to broker data.")
                credentials = vault.read() if data.get("use_saved") else {k: str(data.get(k, "")).strip() for k in ("consumer_key", "mobile_number", "ucc", "mpin")}
                if not all(credentials.get(k) for k in ("consumer_key", "mobile_number", "ucc", "mpin")):
                    raise ValueError("Consumer key, mobile number, UCC and MPIN are required.")
                account_key = hashlib.sha256((credentials["ucc"].upper() + "|" + credentials["mobile_number"]).encode()).hexdigest()
                if any(r["source"] == "broker" and r.get("account_key") != account_key and any(t["status"] not in ("CLOSED", "FAILED") for t in r["tranches"]) for r in strategy.runs):
                    raise ValueError("Open strategy legs belong to another account. Reconnect the original broker account.")
                code = str(data.get("totp", "")).strip()
                if len(code) != 6 or not code.isdigit():
                    raise ValueError("Enter the current six-digit TOTP code.")
                engine.disarm()
                await stop_demo()
                engine.source = "offline"
                engine.instruments.clear()
                engine.quotes.clear()
                await broker.login(credentials, code)
                engine.account_key = account_key
                engine.source = "broker"
                # Restore instruments held by a saved strategy, then subscribe when the feed is ready.
                for run in strategy.runs:
                    if run["source"] == "broker":
                        for tranche in run["tranches"]:
                            for leg in tranche["legs"]:
                                engine.instruments[leg["instrument"]["key"]] = leg["instrument"]
                for _ in range(40):
                    if broker.market_connected:
                        break
                    await asyncio.sleep(.25)
                if engine.instruments and broker.market_connected:
                    await broker.subscribe(list(engine.instruments))
                selection["expiry"] = ""
                if data.get("save"):
                    vault.save(credentials)
                event("Broker authenticated. Load an expiry to subscribe to its option chain.")
            elif action == "disconnect":
                if strategy.has_open():
                    raise ValueError("Pause new TBS entries and close its open legs before disconnecting.")
                engine.disarm()
                await stop_demo()
                await broker.close()
                engine.source = "offline"
                engine.quotes.clear()
                event("Broker disconnected. Existing broker orders and positions are unchanged.")
            elif action == "forget":
                vault.path.unlink(missing_ok=True)
                event("Saved credentials removed.")
            elif action == "demo":
                if any(r["source"] != "demo" and (r["enabled"] or any(t["status"] not in ("CLOSED", "FAILED") for t in r["tranches"])) for r in strategy.runs):
                    raise ValueError("Pause TBS and close its open legs before changing the feed.")
                if engine.mode != "dry":
                    raise ValueError("Switch to dry mode before starting demo data.")
                engine.disarm()
                await stop_demo()
                await broker.close()
                engine.source = "demo"
                engine.instruments.clear()
                engine.quotes.clear()
                selection["expiry"] = "DEMO"
                for name, center, step, lot in (("SENSEX", 74600, 100, 20), ("NIFTY", 24800, 50, 65)):
                    for strike in range(center - step * 6, center + step * 7, step):
                        for side in ("CE", "PE"):
                            key = f"demo|{name}{strike}{side}"
                            engine.instruments[key] = {"key": key, "exchange": "demo", "token": key, "symbol": f"{name} DEMO {strike} {side}",
                                "strike": strike, "option_type": side, "expiry": "DEMO", "underlying": name, "lot_size": lot, "tick_size": .05}
                demo_task = asyncio.create_task(demo_feed())
                event("Synthetic demo data started. Demo lot sizes are illustrative, not broker contract specifications.")
            elif action == "select":
                name = data.get("underlying")
                if name not in ("SENSEX", "NIFTY"):
                    raise ValueError("Choose SENSEX or NIFTY.")
                selection["underlying"] = name
                selection["expiry"] = "DEMO" if engine.source == "demo" else ""
            elif action == "expiries":
                return {"expiries": await broker.expiries(selection["underlying"])}
            elif action == "chain":
                expiry = str(data.get("expiry", ""))
                contracts = await broker.chain(selection["underlying"], expiry)
                if len(set(engine.instruments) | set(contracts)) > 400:
                    raise ValueError("Subscription limit reached. Reconnect to reset subscriptions.")
                await broker.subscribe(list(contracts))
                engine.instruments.update(contracts)
                selection["expiry"] = expiry
                event(f"Subscribed to {len(contracts)} {selection['underlying']} option contracts.")
                if dry_startup_available:
                    dry_startup_available = False
                    strategy.start(selection["underlying"], expiry, automatic=True)
            elif action == "mode":
                if any((r["enabled"] or any(t["status"] not in ("CLOSED", "FAILED") for t in r["tranches"])) and r["mode"] != data.get("mode") for r in strategy.runs):
                    raise ValueError("Pause TBS and close its legs before changing execution mode.")
                engine.set_mode(data.get("mode"))
                dry_startup_available = False
                event(f"Execution set to {engine.mode.upper()}. Live entry is disarmed.")
            elif action == "arm":
                engine.arm()
                event("Live order entry armed for this process session.")
            elif action == "disarm":
                engine.disarm()
                event("Live order entry disarmed.")
            elif action == "halt":
                dry_startup_available = False
                engine.halted = True
                engine.disarm()
                event("Order entry locked. Existing broker orders and positions remain open.")
            elif action == "resume":
                engine.halted = False
                event("Order entry unlocked. Live mode remains disarmed.")
            elif action == "order":
                if any(r["mode"] == engine.mode and r["source"] == engine.source and any(l["instrument"]["key"] == data.get("key") and l["entry"] is not None and l["exit"] is None for t in r["tranches"] for l in t["legs"]) for r in strategy.runs):
                    raise ValueError("TBS manages an open leg in this contract. Use Exit TBS to avoid conflicting manual orders.")
                result = await engine.order(data)
                event(f"{result['mode'].upper()} {result['symbol']} · {result['status']}")
                return {"order": result, "state": snapshot()}
            elif action == "reconcile":
                await broker.reconcile()
                event("Broker order book and positions reconciled.")
            elif action == "cancel":
                if not broker.authenticated:
                    raise ValueError("Connect the broker first.")
                order = next((o for o in ledger.orders("live") if o["id"] == data.get("id")), None)
                if not order or not order.get("broker_id"):
                    raise ValueError("No broker order ID available for cancellation.")
                result = await asyncio.to_thread(broker.client.cancel_order, order_id=order["broker_id"])
                from broker import valid_response
                valid_response(result)
                event("Cancellation requested; awaiting broker confirmation.")
                await broker.reconcile()
            elif action == "strategy":
                dry_startup_available = False
                strategy.start(selection["underlying"], selection["expiry"], data.get("confirm", ""))
            elif action == "strategy-pause":
                dry_startup_available = False
                strategy.pause()
            elif action == "strategy-exit":
                dry_startup_available = False
                await strategy.close_all()
            elif action == "strategy-demo-entry":
                if engine.source != "demo" or engine.mode != "dry":
                    raise ValueError("Manual demo entries require demo data and dry mode.")
                run = next((r for r in reversed(strategy.runs) if r["source"] == "demo" and r["underlying"] == selection["underlying"] and r["date"] == datetime.now(IST).date().isoformat()), None)
                if not run or not run["enabled"]:
                    raise ValueError("Start dry TBS first.")
                from strategy import SCHEDULE
                count = len(run["tranches"])
                if count >= len(SCHEDULE):
                    raise ValueError(f"All {len(SCHEDULE)} demo tranches have been entered for this session.")
                await strategy.new_tranche(run, SCHEDULE[count], 20 if count == 0 else 30)
            else:
                return JSONResponse({"error": "Unknown action."}, status_code=404)
        return {"state": snapshot()}
    except (ValueError, KeyError, TypeError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    except Exception:
        event("Operation failed. Check the broker connection and retry the requested action.", True)
        return JSONResponse({"error": "Operation failed. No credentials were logged. Check the broker connection."}, status_code=502)


@app.websocket("/ws")
async def websocket(ws: WebSocket):
    if not remote_access.authorized_proxy(ws.headers, ws.client.host if ws.client else None):
        await ws.close(code=1008)
        return
    if not local_origin(ws.headers.get("origin"), ws.headers.get("host", "")):
        await ws.close(code=1008)
        return
    await ws.accept()
    try:
        while True:
            await ws.send_json(snapshot())
            await asyncio.sleep(1)
    except (WebSocketDisconnect, RuntimeError, OSError):
        pass


app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")
