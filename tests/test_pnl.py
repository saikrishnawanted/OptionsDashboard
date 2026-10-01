import time
from datetime import datetime, timedelta

from core import IST
from pnl import PnlHistory, totals
from test_terminal import engine


DAY = "2026-10-01"


def fill(side="S", qty=20, price=100, option="CE", mode="dry", source="demo", day=DAY, account=None):
    return {"id": "id", "key": "demo|" + option, "symbol": "TEST" + option, "option_type": option,
            "underlying": "SENSEX", "mode": mode, "source": source, "account_key": account,
            "side": side, "filled_qty": qty, "fill_price": price, "time": day + "T10:00:00+05:30"}


def calculate(orders, quotes=None):
    if quotes is None:
        quotes = {"demo|CE": {"ltp": 90, "source": "demo", "received": time.time()}}
    return totals(orders, {}, quotes, DAY, "dry", "demo", "SENSEX")[0]


def test_partial_close_realized_and_unrealized():
    values = calculate([fill(), fill("B", 5, 80)])
    assert values["CE"] == {"realized": 100, "unrealized": 150, "open": 1, "total": 250}
    assert values["OVERALL"]["total"] == 250 and values["PE"]["total"] == 0


def test_flat_position_needs_no_quote_and_reversal_uses_new_average():
    assert calculate([fill(), fill("B", 20, 80)], {})["CE"]["total"] == 400
    values = calculate([fill(), fill("B", 30, 80)])
    assert values["CE"]["realized"] == 400
    assert values["CE"]["unrealized"] == 100


def test_missing_stale_and_wrong_feed_marks_are_gaps():
    for quotes in ({}, {"demo|CE": {"ltp": 90, "source": "demo", "received": 0}},
                   {"demo|CE": {"ltp": 90, "source": "broker", "received": time.time()}}):
        values = calculate([fill()], quotes)
        assert values["OVERALL"]["total"] is None
        assert values["CE"]["realized"] == 0


def test_filters_and_confirmed_partial_live_fills():
    orders = [fill(), fill(mode="live"), fill(source="broker"), fill(day="2026-09-30"), fill(account="other")]
    assert calculate(orders)["OVERALL"]["total"] == 200
    live = fill(mode="live", source="broker", qty=7, account="mine")
    live["status"] = "CANCELLED"  # Cancelled remainder does not erase confirmed fills.
    q = {"demo|CE": {"ltp": 90, "source": "broker", "received": time.time()}}
    result, has = totals([live], {}, q, DAY, "live", "broker", "SENSEX", "mine")
    assert has and result["CE"]["total"] == 70


def test_history_persists_isolates_modes_and_does_not_backfill(engine):
    current = datetime(2026, 10, 1, 10, tzinfo=IST)
    h = PnlHistory(engine.ledger)
    assert h.view(engine, current, "SENSEX")["points"] == []
    engine.ledger.save(fill())
    h.sample(engine, current)
    h.sample(engine, current + timedelta(seconds=1))
    restored = PnlHistory(engine.ledger)
    points = restored.view(engine, current, "SENSEX")["points"]
    assert len(points) == 1 and points[0]["CE"]["total"] == 0
    assert restored.view(engine, current, "NIFTY")["points"] == []
    engine.mode = "live"
    assert restored.view(engine, current, "SENSEX")["points"] == []
    engine.mode = "dry"
    assert restored.view(engine, current + timedelta(days=1), "SENSEX")["points"] == []


def test_history_retention_and_account_scope(engine):
    h = PnlHistory(engine.ledger)
    old = datetime(2026, 8, 1, 10, tzinfo=IST)
    engine.ledger.save(fill(day="2026-08-01"))
    h.sample(engine, old)
    h.sample(engine, datetime(2026, 10, 1, 10, tzinfo=IST))
    assert h.db.execute("SELECT COUNT(*) FROM pnl_history").fetchone()[0] == 0
    assert h.scope(old, "dry", "broker", "SENSEX", "a") != h.scope(old, "dry", "broker", "SENSEX", "b")


def test_pe_filter_and_rejected_unfilled_order():
    rejected = fill(qty=0)
    rejected["status"] = "REJECTED"
    q = {"demo|PE": {"ltp": 110, "source": "demo", "received": time.time()}}
    result = calculate([rejected, fill(option="PE")], q)
    assert result["CE"]["total"] == 0
    assert result["PE"]["total"] == -200
    assert result["OVERALL"]["total"] == -200


def test_pnl_endpoint_uses_selected_market_without_secrets(engine, monkeypatch):
    import app
    from fastapi.testclient import TestClient
    monkeypatch.setattr(app, "engine", engine)
    monkeypatch.setattr(app, "pnl_history", PnlHistory(engine.ledger))
    monkeypatch.setattr(app, "selection", {"underlying": "NIFTY", "expiry": "DEMO"})
    with TestClient(app.app) as client:
        response = client.get("/api/pnl")
        assert response.status_code == 200
        data = response.json()
        assert data["underlying"] == "NIFTY" and data["mode"] == "dry"
        assert data["points"] == [] and data["current"]["OVERALL"]["total"] == 0
        assert "account_key" not in response.text and "token" not in response.text
