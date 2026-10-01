import asyncio
import sys
import time
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core import Engine, IST, Ledger
from strategy import SCHEDULE, Strategy, levels


class FakeBroker:
    ready = True
    management_ready = True
    market_connected = True
    def __init__(self):
        self.calls = []
    async def place(self, *args):
        self.calls.append(args)
        return {"stat": "Ok", "nOrdNo": "123"}
    async def strategy_order(self, *args):
        self.calls.append(args)
        return {"stat": "Ok", "nOrdNo": str(len(self.calls))}


@pytest.fixture
def engine(tmp_path):
    e = Engine(Ledger(tmp_path / "test.sqlite"), FakeBroker())
    e.source = "demo"
    for side in ("CE", "PE"):
        key = "demo|" + side
        e.instruments[key] = {"key": key, "symbol": "SENSEX " + side, "exchange": "bse_fo", "token": side,
                              "strike": 74600, "option_type": side, "underlying": "SENSEX", "expiry": "DEMO", "lot_size": 20, "tick_size": .05}
        e.quotes[key] = {"ltp": 100., "received": time.time(), "source": "demo"}
    e.quotes["index|SENSEX"] = {"ltp": 74610., "received": time.time(), "source": "demo"}
    return e


def request(**kw):
    return {"key": "demo|CE", "client_id": "test-order-01", "mode": "dry", "side": "S", "lots": 1, "price": 99., **kw}


def test_dry_cannot_call_broker_and_is_idempotent(engine):
    first = asyncio.run(engine.order(request()))
    second = asyncio.run(engine.order(request()))
    assert first == second
    assert first["fill_price"] == 99.95
    assert not engine.broker.calls
    assert len(engine.ledger.orders()) == 1


@pytest.mark.parametrize("field,value", [("lots", 0), ("lots", 2), ("lots", True), ("lots", 1.5), ("price", float("nan")), ("price", float("inf")), ("price", -1), ("price", 100.03), ("side", "X"), ("mode", "live")])
def test_invalid_orders_blocked(engine, field, value):
    with pytest.raises(ValueError):
        asyncio.run(engine.order(request(**{field: value})))
    assert not engine.broker.calls


def test_stale_quote_and_halt_block(engine):
    engine.quotes["demo|CE"]["received"] -= 30
    with pytest.raises(ValueError, match="stale"):
        asyncio.run(engine.order(request()))
    engine.quotes["demo|CE"]["received"] = time.time()
    engine.halted = True
    with pytest.raises(ValueError, match="locked"):
        asyncio.run(engine.order(request()))


def test_paper_partial_close_and_reversal(engine):
    asyncio.run(engine.order(request()))
    engine.quotes["demo|CE"]["ltp"] = 90
    asyncio.run(engine.order(request(client_id="test-order-02", side="B", price=100)))
    p = engine.ledger.positions(engine.quotes, "demo")[0]
    assert p["qty"] == 0
    assert p["realized"] == pytest.approx((99.95 - 90.05) * 20)
    assert engine.ledger.positions(engine.quotes, "broker") == []


def test_live_requires_source_and_arm(engine):
    engine.set_mode("live")
    with pytest.raises(ValueError):
        engine.arm("ENABLE LIVE")
    with pytest.raises(ValueError):
        asyncio.run(engine.order(request(mode="live", confirm="PLACE LIVE ORDER")))
    assert not engine.broker.calls


def test_schedule_and_sl_rounding():
    assert SCHEDULE == ["09:18", "09:45", "10:15", "10:45", "11:15", "11:45", "12:15", "12:45"]
    assert levels(100, 20, .05) == (120, 122.4)
    assert levels(100.05, 30, .05) == (130.1, 132.75)


def new_strategy(engine):
    s = Strategy(engine, lambda *args: None)
    s.start("SENSEX", "DEMO")
    return s, s.runs[0]


def test_independent_stops_and_gap_limit(engine):
    s, run = new_strategy(engine)
    asyncio.run(s.new_tranche(run, "09:18", 20))
    t = run["tranches"][0]
    assert len(engine.ledger.orders()) == 2
    ce, pe = t["legs"]
    assert ce["trigger"] == 119.95
    engine.quotes["demo|CE"]["ltp"] = 150
    asyncio.run(s.advance(run, t))
    assert ce["status"] == "SL LIMIT OPEN"
    assert ce["exit"] is None and pe["exit"] is None
    engine.quotes["demo|CE"]["ltp"] = 121
    asyncio.run(s.advance(run, t))
    assert ce["status"] == "STOPPED"
    assert pe["status"] == "OPEN"
    asyncio.run(s.close_all())
    assert t["status"] == "CLOSED"
    assert pe["exit"] is not None
    assert not engine.broker.calls


def test_restart_pauses_new_entries_and_retains_legs(engine):
    s, run = new_strategy(engine)
    asyncio.run(s.new_tranche(run, "09:18", 20))
    resumed = Strategy(engine, lambda *args: None)
    assert not resumed.runs[0]["enabled"]
    assert len(resumed.runs[0]["tranches"]) == 1
    assert resumed.has_open()


def test_broker_lot_mismatch_prevents_entry(engine):
    s, run = new_strategy(engine)
    engine.instruments["demo|CE"]["lot_size"] = 25
    with pytest.raises(ValueError, match="metadata"):
        asyncio.run(s.new_tranche(run, "09:18", 20))
    assert not engine.ledger.orders()


def test_cross_between_scheduler_steps_is_not_lost(engine):
    s, run = new_strategy(engine)
    asyncio.run(s.new_tranche(run, "09:18", 20))
    leg = run["tranches"][0]["legs"][0]
    s.observe_tick("demo|CE", {"ltp": 150, "received": time.time(), "source": "demo"})
    assert leg["triggered"] and leg["exit"] is None
    s.observe_tick("demo|CE", {"ltp": 110, "received": time.time(), "source": "demo"})
    assert leg["exit"] == 110.1
    assert leg["status"] == "STOPPED"
    count = len(engine.ledger.orders())
    s.observe_tick("demo|CE", {"ltp": 110, "received": time.time(), "source": "demo"})
    assert len(engine.ledger.orders()) == count


def test_stale_underlying_prevents_atm_selection(engine):
    s, run = new_strategy(engine)
    engine.quotes["index|SENSEX"]["received"] -= 30
    with pytest.raises(ValueError, match="index"):
        s.pair(run)


def test_live_second_leg_waits_for_first_stop_acceptance(engine):
    engine.mode, engine.source, engine.armed = "live", "broker", True
    for q in engine.quotes.values():
        q["source"] = "broker"
    s = Strategy(engine, lambda *args: None)
    s.start("SENSEX", "DEMO", "START LIVE TBS")
    run = s.runs[0]
    asyncio.run(s.new_tranche(run, "09:18", 20))
    t = run["tranches"][0]
    assert len(engine.broker.calls) == 1  # first sell only
    first = engine.ledger.orders()[0]
    first.update(status="COMPLETE", filled_qty=20, fill_price=100)
    engine.ledger.save(first)
    asyncio.run(s.advance(run, t))
    assert len(engine.broker.calls) == 2
    assert engine.broker.calls[1][3] == "SL"
    assert engine.broker.calls[1][4:6] == (122.4, 120)
    asyncio.run(s.advance(run, t))
    assert len(engine.broker.calls) == 2  # acknowledgement alone is not protection
    stop = engine.ledger.orders()[1]
    stop["status"] = "TRIGGER PENDING"
    engine.ledger.save(stop)
    asyncio.run(s.advance(run, t))
    assert len(engine.broker.calls) == 3  # second sell now allowed


def test_uncertain_live_order_is_not_retried(engine):
    async def uncertain(*args):
        engine.broker.calls.append(args)
        raise TimeoutError()
    engine.broker.strategy_order = uncertain
    engine.mode, engine.source, engine.armed = "live", "broker", True
    for q in engine.quotes.values():
        q["source"] = "broker"
    s = Strategy(engine, lambda *args: None)
    s.start("SENSEX", "DEMO", "START LIVE TBS")
    run = s.runs[0]
    asyncio.run(s.new_tranche(run, "09:18", 20))
    for _ in range(3):
        asyncio.run(s.advance(run, run["tranches"][0]))
    assert len(engine.broker.calls) == 1
    assert engine.ledger.orders()[0]["status"] == "UNKNOWN"
    assert not engine.armed and not run["enabled"]


def test_rejected_protective_stop_closes_first_leg_without_second_entry(engine):
    engine.mode, engine.source, engine.armed = "live", "broker", True
    for q in engine.quotes.values():
        q["source"] = "broker"
    s = Strategy(engine, lambda *args: None)
    s.start("SENSEX", "DEMO", "START LIVE TBS")
    run = s.runs[0]
    asyncio.run(s.new_tranche(run, "09:18", 20))
    t = run["tranches"][0]
    entry = engine.ledger.orders()[0]
    entry.update(status="COMPLETE", filled_qty=20, fill_price=100)
    engine.ledger.save(entry)
    asyncio.run(s.advance(run, t))
    stop = engine.ledger.orders()[1]
    stop["status"] = "REJECTED"
    engine.ledger.save(stop)
    asyncio.run(s.advance(run, t))
    assert len(engine.broker.calls) == 3
    assert engine.broker.calls[2][1:4] == ("B", 20, "MKT")
    assert t["legs"][1]["entry_id"] is None
    assert not run["enabled"]


def test_partial_stop_fill_reduces_exit_quantity(engine):
    engine.mode, engine.source, engine.armed = "live", "broker", True
    for q in engine.quotes.values():
        q["source"] = "broker"
    s = Strategy(engine, lambda *args: None)
    s.start("SENSEX", "DEMO", "START LIVE TBS")
    run = s.runs[0]
    asyncio.run(s.new_tranche(run, "09:18", 20))
    t = run["tranches"][0]
    entry = engine.ledger.orders()[0]
    entry.update(status="COMPLETE", filled_qty=20, fill_price=100)
    engine.ledger.save(entry)
    asyncio.run(s.advance(run, t))
    stop = engine.ledger.orders()[1]
    stop.update(status="CANCELLED", filled_qty=5, fill_price=121)
    engine.ledger.save(stop)
    engine.broker.ready = False  # Market-feed outage cannot block an authenticated close.
    t["exit_requested"] = True
    asyncio.run(s.advance(run, t))
    assert engine.broker.calls[-1][1:4] == ("B", 15, "MKT")
    exit_order = engine.ledger.orders()[-1]
    exit_order.update(status="COMPLETE", filled_qty=15, fill_price=110)
    engine.ledger.save(exit_order)
    asyncio.run(s.advance(run, t))
    assert t["legs"][0]["exit"] == pytest.approx(112.75)
    assert t["status"] == "CLOSED"


def test_clock_entries_exact_once_and_1514_exit(engine):
    engine.source = "broker"
    for q in engine.quotes.values():
        q["source"] = "broker"
    s, run = new_strategy(engine)
    current = datetime(2026, 9, 30, 9, 18, tzinfo=IST)
    run["date"] = current.date().isoformat()
    run["started_at"] = current.timestamp() - 60
    asyncio.run(s.step(current))
    asyncio.run(s.step(current))
    assert len(run["tranches"]) == 1
    asyncio.run(s.step(current.replace(hour=9, minute=45)))
    assert len(run["tranches"]) == 2
    assert run["tranches"][1]["sl"] == 30
    asyncio.run(s.step(current.replace(hour=15, minute=14)))
    assert all(t["status"] == "CLOSED" for t in run["tranches"])
    assert not run["enabled"]


def test_vault_encrypted_roundtrip(tmp_path):
    from vault import Vault
    vault = Vault(tmp_path / "test.dpapi")
    vault.save({"consumer_key": "fake-unit-test-secret"})
    assert b"fake-unit-test-secret" not in vault.path.read_bytes()
    assert vault.read() == {"consumer_key": "fake-unit-test-secret"}


def test_http_origin_and_token_protection(tmp_path, monkeypatch):
    monkeypatch.setenv("TERMINAL_DATA_DIR", str(tmp_path / "http"))
    from fastapi.testclient import TestClient
    import app
    with TestClient(app.app) as client:
        assert client.get("/").status_code == 200
        bootstrap = client.get("/api/bootstrap").json()
        assert "consumer_key" not in str(bootstrap)
        assert client.post("/api/demo", json={}).status_code == 403
        headers = {"origin": "http://evil.example", "x-terminal-token": bootstrap["token"]}
        assert client.post("/api/demo", json={}, headers=headers).status_code == 403
        headers["origin"] = "http://testserver"
        assert client.post("/api/demo", json={}, headers=headers).status_code == 200
        assert client.post("/api/arm", json={"phrase": "ENABLE LIVE"}, headers=headers).status_code == 400
        with client.websocket_connect("/ws", headers={"origin": "http://testserver"}) as ws:
            assert ws.receive_json()["mode"] == "dry"
