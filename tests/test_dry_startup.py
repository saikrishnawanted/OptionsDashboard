import asyncio
import time
from datetime import datetime

import pytest

import strategy as strategy_module
from core import IST
from strategy import Strategy
from test_terminal import engine


@pytest.fixture
def paper(engine, monkeypatch):
    class Clock(datetime):
        current = datetime(2026, 10, 1, 9, 25, tzinfo=IST)

        @classmethod
        def now(cls, tz=None):
            return cls.current

    monkeypatch.setattr(strategy_module, "datetime", Clock)
    engine.source = "broker"
    engine.account_key = "test-account"
    for quote in engine.quotes.values():
        quote["source"] = "broker"
    return engine, Strategy(engine, lambda *args: None), Clock


def test_startup_fills_once_and_pnl_matches_positions(paper):
    e, s, clock = paper
    assert s.start("SENSEX", "DEMO", automatic=True)
    asyncio.run(s.step(clock.current))
    asyncio.run(s.step(clock.current))
    assert not s.start("SENSEX", "DEMO", automatic=True)
    s.start("SENSEX", "DEMO")
    asyncio.run(s.step(clock.current))
    assert len(e.ledger.orders()) == 2
    tranche = s.view("SENSEX")["tranches"][0]
    assert tranche["startup"] and tranche["sl"] == 20
    assert all(leg["qty"] == 20 and leg["status"] == "OPEN" for leg in tranche["legs"])
    positions = e.ledger.positions(e.quotes, "broker")
    assert [p["qty"] for p in positions] == [-20, -20]
    assert tranche["pnl"] == pytest.approx(sum(p["realized"] + p["unrealized"] for p in positions))
    assert tranche["scenarios"]["20"] * 20 == pytest.approx(tranche["pnl"])
    assert not e.broker.calls


def test_startup_waits_for_all_three_fresh_ticks(paper):
    e, s, clock = paper
    assert s.start("SENSEX", "DEMO", automatic=True)
    for key in ("index|SENSEX", "demo|CE", "demo|PE"):
        e.quotes[key]["received"] = 0
        asyncio.run(s.step(clock.current))
        assert not e.ledger.orders()
        assert s.view("SENSEX")["startup_pending"]
        e.quotes[key]["received"] = time.time()
    asyncio.run(s.step(clock.current))
    assert len(e.ledger.orders()) == 2
    assert not e.broker.calls


@pytest.mark.parametrize("mode,source,halted", [("live", "broker", False), ("dry", "demo", False), ("dry", "offline", False), ("dry", "broker", True)])
def test_automatic_entry_never_arms_live_or_bypasses_lock(paper, mode, source, halted):
    e, s, clock = paper
    e.mode, e.source, e.halted = mode, source, halted
    assert not s.start("SENSEX", "DEMO", automatic=True)
    asyncio.run(s.step(clock.current))
    assert not s.runs and not e.ledger.orders() and not e.broker.calls
    assert not e.armed


@pytest.mark.parametrize("current", [datetime(2026, 10, 1, 9, 14, tzinfo=IST), datetime(2026, 10, 1, 15, 14, tzinfo=IST), datetime(2026, 10, 3, 9, 25, tzinfo=IST)])
def test_automatic_entry_obeys_session_hours(paper, current):
    e, s, clock = paper
    clock.current = current
    assert not s.start("SENSEX", "DEMO", automatic=True)
    assert not e.ledger.orders()


def test_pause_prevents_pending_entry_and_keeps_existing_stop_active(paper):
    e, s, clock = paper
    s.start("SENSEX", "DEMO", automatic=True)
    s.pause()
    asyncio.run(s.step(clock.current))
    assert not e.ledger.orders()
    assert not s.start("SENSEX", "DEMO", automatic=True)
    s.start("SENSEX", "DEMO")
    asyncio.run(s.step(clock.current))
    s.pause()
    e.quotes["demo|CE"]["ltp"] = 121
    s.observe_tick("demo|CE", e.quotes["demo|CE"])
    assert s.runs[0]["tranches"][0]["legs"][0]["status"] == "STOPPED"
    asyncio.run(s.step(clock.current.replace(minute=45)))
    assert len(s.runs[0]["tranches"]) == 1
    assert not e.broker.calls


def test_restart_requires_resume_and_never_repeats_first_tranche(paper):
    e, s, clock = paper
    s.start("SENSEX", "DEMO", automatic=True)
    asyncio.run(s.step(clock.current))
    recovered = Strategy(e, lambda *args: None)
    assert not recovered.start("SENSEX", "DEMO", automatic=True)
    asyncio.run(recovered.step(clock.current))
    assert not recovered.runs[0]["enabled"]
    recovered.start("SENSEX", "DEMO")
    asyncio.run(recovered.step(clock.current))
    assert len(e.ledger.orders()) == 2
    assert len(recovered.runs[0]["tranches"]) == 1


def test_delayed_startup_does_not_stack_same_minute_entry(paper):
    e, s, clock = paper
    s.start("SENSEX", "DEMO", automatic=True)
    clock.current = clock.current.replace(minute=45, second=0)
    asyncio.run(s.step(clock.current))
    asyncio.run(s.step(clock.current))
    assert len(s.runs[0]["tranches"]) == 1
    clock.current = clock.current.replace(hour=10, minute=15)
    asyncio.run(s.step(clock.current))
    assert [t["sl"] for t in s.runs[0]["tranches"]] == [20, 30]
    assert [t["slot"] for t in s.runs[0]["tranches"]] == ["09:18", "10:15"]
    asyncio.run(s.step(clock.current.replace(hour=15, minute=14)))
    assert all(t["status"] == "CLOSED" for t in s.runs[0]["tranches"])
    assert not s.runs[0]["enabled"]
    assert all(p["qty"] == 0 for p in e.ledger.positions(e.quotes, "broker"))
    assert not e.broker.calls


def test_exit_is_idempotent_and_preserves_realized_pnl(paper):
    e, s, clock = paper
    s.start("SENSEX", "DEMO", automatic=True)
    asyncio.run(s.step(clock.current))
    e.quotes["demo|CE"]["ltp"] = 90
    e.quotes["demo|PE"]["ltp"] = 95
    asyncio.run(s.close_all())
    asyncio.run(s.close_all())
    assert len(e.ledger.orders()) == 4
    positions = e.ledger.positions(e.quotes, "broker")
    assert all(p["qty"] == 0 for p in positions)
    assert sum(p["realized"] for p in positions) == pytest.approx((99.95 - 90.05 + 99.95 - 95.05) * 20)
    assert s.view("SENSEX")["tranches"][0]["pnl"] == pytest.approx(sum(p["realized"] for p in positions))
    assert not e.broker.calls


def test_chain_route_starts_paper_once_and_controls_protect_open_legs(paper, tmp_path, monkeypatch):
    e, s, clock = paper
    monkeypatch.setenv("TERMINAL_DATA_DIR", str(tmp_path / "http"))
    from fastapi.testclient import TestClient
    import app

    async def chain(*args):
        return e.instruments.copy()

    async def subscribe(*args):
        pass

    e.broker.chain, e.broker.subscribe = chain, subscribe
    e.broker.authenticated, e.broker.reconciliation_error, e.broker.positions = True, "", []
    for name, value in {"engine": e, "broker": e.broker, "ledger": e.ledger, "strategy": s,
                        "selection": {"underlying": "SENSEX", "expiry": ""},
                        "dry_startup_available": True, "events": [], "lock": asyncio.Lock()}.items():
        monkeypatch.setattr(app, name, value)
    client = TestClient(app.app)
    headers = {"origin": "http://testserver", "x-terminal-token": client.get("/api/bootstrap").json()["token"]}

    def post(action, data=None):
        return client.post("/api/" + action, json=data or {}, headers=headers)

    assert post("chain", {"expiry": "DEMO"}).status_code == 200
    asyncio.run(s.step(clock.current))
    assert len(e.ledger.orders()) == 2
    assert post("chain", {"expiry": "DEMO"}).status_code == 200
    assert client.get("/api/bootstrap").json()["state"]["strategy"]["enabled"]
    assert len(e.ledger.orders()) == 2
    assert post("mode", {"mode": "live"}).status_code == 400
    assert post("disconnect").status_code == 400
    assert post("order", {"key": "demo|CE"}).status_code == 400
    assert post("strategy-pause").status_code == 200
    assert post("chain", {"expiry": "DEMO"}).status_code == 200
    assert not s.runs[0]["enabled"]
    assert post("strategy-exit").status_code == 200
    assert post("strategy-exit").status_code == 200
    exported = client.get("/api/export")
    assert exported.status_code == 200
    assert len(exported.text.strip().splitlines()) == 5  # heading plus two sells and two closes
    assert len(e.ledger.orders()) == 4 and not e.broker.calls
