"""Adapter for the adjacent Kotak Neo SDK. Credentials stay in this process."""
import asyncio
import math
import os
import time
from datetime import datetime

os.environ["NEO_LOG_FILE_ENABLED"] = "false"
os.environ["NEO_LOG_LEVEL"] = "CRITICAL"


def valid_response(value):
    if not isinstance(value, dict) or any(value.get(k) for k in ("error", "Error", "errMsg")):
        raise ValueError("Broker request failed. Check the account session and try again.")
    if str(value.get("stat", "ok")).lower() not in ("ok", "success"):
        raise ValueError("Broker rejected the request.")
    return value


def report_rows(value, label):
    """The OMS uses an explicit 5203/No Data response for an empty account."""
    if isinstance(value, list):
        return value
    if (isinstance(value, dict) and not value.get("error") and not value.get("Error")
            and str(value.get("stCode")) == "5203"
            and str(value.get("errMsg", "")).strip().lower() == "no data"):
        return []
    result = valid_response(value)
    rows = result.get("data")
    if isinstance(rows, list):
        return rows
    raise ValueError(f"Broker {label} format is unrecognized (fields: {', '.join(sorted(result))}).")


def chain_data(response):
    """Accept the SDK's documented wrapper and the production flat payload."""
    response = valid_response(response)
    data = response.get("data", response)
    if not isinstance(data, dict) or not isinstance(data.get("call"), list) or not isinstance(data.get("put"), list):
        raise ValueError("Broker option-chain response has an unrecognized format.")
    return data


class Broker:
    def __init__(self, on_tick, on_order, on_event):
        self.client = None
        self.market = None
        self.order_feed = None
        self.tasks = []
        self.on_tick, self.on_order, self.on_event = on_tick, on_order, on_event
        self.reconciled_at = 0
        self.positions = []
        self.authenticated = False
        self.reconciliation_error = ""

    @property
    def market_connected(self):
        return bool(self.market and self.market.is_connected)

    @property
    def ready(self):
        return self.management_ready and self.market_connected

    @property
    def management_ready(self):
        # Closing existing exposure must not depend on a working market feed.
        return self.authenticated and time.time() - self.reconciled_at < 30

    async def login(self, credentials, totp):
        await self.close()
        from neo_api_client import NeoAPI
        client = NeoAPI(consumer_key=credentials["consumer_key"], environment="prod")
        result = await asyncio.to_thread(client.totp_login, mobile_number=credentials["mobile_number"],
                                        ucc=credentials["ucc"], totp=totp)
        data = valid_response(result).get("data", {})
        if data.get("status") != "success":
            raise ValueError("TOTP authentication failed. Check your details and current code.")
        result = await asyncio.to_thread(client.totp_validate, mpin=credentials["mpin"])
        data = valid_response(result).get("data", {})
        if data.get("status") != "success" or not data.get("token"):
            raise ValueError("MPIN validation failed.")
        self.client, self.authenticated = client, True
        self.tasks = [asyncio.create_task(self.market_loop()), asyncio.create_task(self.order_loop()),
                      asyncio.create_task(self.reconcile_loop())]

    async def market_loop(self):
        try:
            async with self.client.create_websocket() as ws:
                self.market = ws
                ws.on_error = lambda _: self.on_event("Market connection interrupted; live orders disarmed.", True)
                self.on_event("Broker market WebSocket connected.", False)
                from neo_api_client.websocket.feed import WsToken
                await ws.subscribe_index([WsToken("nse_cm", "Nifty 50"), WsToken("bse_cm", "SENSEX")])
                async for message in ws:
                    token = getattr(message, "instrument_token", None)
                    ltp = getattr(message, "last_traded_price", None)
                    if token is None or ltp is None or not math.isfinite(float(ltp)) or float(ltp) <= 0:
                        continue
                    stamp = getattr(message, "last_update_time", getattr(message, "last_trade_time", 0)) or 0
                    # Reject old exchange snapshots instead of refreshing stale prices on reconnect.
                    received = min(time.time(), float(stamp)) if stamp else 0
                    if message.type == "index":
                        name = str(getattr(message, "name", "")).upper().replace(" ", "")
                        underlying = "NIFTY" if name in ("NIFTY50", "NIFTY") else "SENSEX" if name in ("SENSEX", "BSESENSEX") else None
                        if underlying:
                            self.on_tick("index|" + underlying, {"ltp": float(ltp), "received": received, "source": "broker"})
                        continue
                    self.on_tick(f"{message.exchange_segment}|{token}", {
                        "ltp": float(ltp), "received": received, "source": "broker",
                        "change": getattr(message, "net_change_percent", 0),
                        "oi": getattr(message, "open_interest", 0),
                        "volume": getattr(message, "volume_traded_today", 0)})
        except asyncio.CancelledError:
            raise
        except Exception:
            self.on_event("Market connection failed. Reconnect from Broker settings.", True)
        finally:
            self.market = None

    async def order_loop(self):
        try:
            async with self.client.create_order_feed() as ws:
                self.order_feed = ws
                ws.on_disconnect = lambda: self.on_event("Order stream disconnected; live orders disarmed.", True)
                async for message in ws:
                    if message.type == "order":
                        self.on_order(message.data.model_dump(by_alias=True, exclude_none=True))
        except asyncio.CancelledError:
            raise
        except Exception:
            self.on_event("Order stream unavailable. Order book reconciliation remains active.", True)
        finally:
            self.order_feed = None

    async def reconcile(self):
        if not self.authenticated:
            raise ValueError("Connect the broker first.")
        rows = report_rows(await asyncio.to_thread(self.client.order_report), "order book")
        positions = report_rows(await asyncio.to_thread(self.client.positions), "positions")
        for row in rows:
            self.on_order(row)
        self.positions = positions
        self.reconciled_at = time.time()
        self.reconciliation_error = ""

    async def reconcile_loop(self):
        while True:
            try:
                await self.reconcile()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.reconciled_at = 0
                self.reconciliation_error = str(exc) if isinstance(exc, ValueError) else f"Broker reconciliation failed ({type(exc).__name__})."
                self.on_event(self.reconciliation_error + " Live orders disarmed.", True)
            await asyncio.sleep(10)

    async def expiries(self, underlying):
        if not self.authenticated:
            raise ValueError("Connect the broker first.")
        result = valid_response(await asyncio.to_thread(self.client.expiries,
                                exchange="bse_fo" if underlying == "SENSEX" else "nse_fo", underlying=underlying))
        return result.get("expiries", [])

    async def chain(self, underlying, expiry):
        if not self.market_connected:
            raise ValueError("Wait for the broker market WebSocket to connect.")
        exchange = "bse_fo" if underlying == "SENSEX" else "nse_fo"
        result = valid_response(await asyncio.to_thread(self.client.option_chain, exchange=exchange,
                                underlying=underlying, expiry=expiry, instrument_type="option", count=20))
        data = chain_data(result)
        metadata = await asyncio.to_thread(self.client.search_scrip, exchange_segment=exchange,
                                          symbol=underlying, expiry=datetime.strptime(expiry, "%Y-%m-%d").strftime("%d%b%Y"),
                                          option_type="CE,PE", ignore_50multiple=False)
        by_symbol = {str(x["pTrdSymbol"]): x for x in metadata} if isinstance(metadata, list) else {}
        instruments = {}
        for side in ("call", "put"):
            for row in data.get(side, []):
                inst = row.get("instrument", row.get("inst", {}))
                key = inst["neoSymbol"]
                if not key.startswith(exchange + "|"):
                    continue
                meta = by_symbol.get(inst["symbol"], {})
                lot = int(float(meta.get("lLotSize") or meta.get("iLotSize") or data.get("common_data", {}).get("mktLot") or 0))
                tick = float(meta.get("dTickSize") or 0) / 100  # NSE/BSE master prices are in paise.
                strike = float(inst.get("strikePrice", inst.get("strkPrc")))
                option_type = inst.get("optionType", inst.get("optType"))
                if not math.isfinite(strike) or option_type not in ("CE", "PE"):
                    raise ValueError("Broker returned invalid contract metadata.")
                instruments[key] = {"key": key, "exchange": exchange, "token": key.split("|", 1)[1],
                                    "symbol": inst["symbol"], "strike": strike,
                                    "option_type": option_type, "expiry": expiry,
                                    "underlying": underlying, "lot_size": lot, "tick_size": tick}
                quote = row.get("quote", {})
                oi = row.get("openInterest", row.get("oi", {}))
                ltp = float(quote.get("ltp") or 0)
                if math.isfinite(ltp) and ltp > 0:
                    previous = float(quote.get("prevClose", quote.get("pc")) or 0)
                    self.on_tick(key, {"ltp": ltp, "received": 0, "source": "broker", "origin": "snapshot",
                                       "change": round((ltp / previous - 1) * 100, 2) if previous > 0 else 0,
                                       "oi": int(float(oi.get("current", oi.get("cur")) or 0)),
                                       "volume": int(float(quote.get("volume", quote.get("vol")) or 0))})
        if not instruments:
            raise ValueError("No contracts returned for that underlying and expiry.")
        return instruments

    async def subscribe(self, keys):
        from neo_api_client.websocket.feed import WsToken
        await self.market.subscribe_scrips([WsToken(*key.split("|", 1)) for key in keys])

    async def place(self, inst, side, qty, price, client_id):
        return await asyncio.to_thread(self.client.place_order, exchange_segment=inst["exchange"], product="MIS",
                                       price=str(price), order_type="L", quantity=str(qty), validity="DAY",
                                       trading_symbol=inst["symbol"], transaction_type=side, amo="NO", tag=client_id)

    async def strategy_order(self, inst, side, qty, kind, price, trigger, client_id):
        return await asyncio.to_thread(self.client.place_order, exchange_segment=inst["exchange"], product="MIS",
                                       price=str(price), trigger_price=str(trigger), order_type=kind,
                                       quantity=str(qty), validity="DAY", trading_symbol=inst["symbol"],
                                       transaction_type=side, amo="NO", tag=client_id)

    async def close(self):
        self.authenticated = False
        self.reconciled_at = 0
        for task in self.tasks:
            task.cancel()
        if self.tasks:
            await asyncio.gather(*self.tasks, return_exceptions=True)
        self.tasks = []
        self.market = self.order_feed = None
        self.positions = []
        self.reconciliation_error = ""
