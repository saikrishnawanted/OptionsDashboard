import pytest

from core import Engine, Ledger
from pnl import totals


@pytest.mark.parametrize("closed,realized,remaining", [(20, 400, 0), (5, 100, 1)])
def test_manual_broker_close_is_imported_once(tmp_path, monkeypatch, closed, realized, remaining):
    import app
    ledger = Ledger(tmp_path / "orders.sqlite")
    engine = Engine(ledger, None)
    engine.account_key = "account-a"
    monkeypatch.setattr(app, "ledger", ledger)
    monkeypatch.setattr(app, "engine", engine)
    entry = {"id": "entry", "broker_id": "100", "mode": "live", "source": "broker",
             "account_key": "account-a", "key": "nse_fo|123", "symbol": "NIFTY26O0621800CE",
             "underlying": "NIFTY", "option_type": "CE", "side": "S", "qty": 20,
             "filled_qty": 20, "fill_price": 100, "status": "COMPLETE",
             "time": "2026-10-05T09:20:00+05:30"}
    close = {"nOrdNo": "200", "trdSym": entry["symbol"], "exSeg": "nse_fo", "tok": "123",
             "trnsTp": "B", "qty": str(closed), "fldQty": str(closed), "avgPrc": "80",
             "ordSt": "complete", "ordDtTm": "05-Oct-2026 10:00:00"}
    # Broker reports can arrive newest first.
    app.order_update(close)
    ledger.save(entry)
    app.order_update(close)
    app.order_update({"nOrdNo": "200", "fldQty": "0", "ordSt": "open"})
    assert len(ledger.orders()) == 2
    values, _ = totals(ledger.orders(), {}, {}, "2026-10-05", "live", "broker", "NIFTY", "account-a")
    assert values["CE"]["realized"] == realized
    assert values["CE"]["open"] == remaining
    if not remaining:
        assert values["CE"]["unrealized"] == 0
        assert values["CE"]["total"] == realized


def test_incomplete_external_update_does_not_invent_a_fill(tmp_path, monkeypatch):
    import app
    ledger = Ledger(tmp_path / "orders.sqlite")
    engine = Engine(ledger, None)
    engine.account_key = "account-a"
    monkeypatch.setattr(app, "ledger", ledger)
    monkeypatch.setattr(app, "engine", engine)
    app.order_update({"nOrdNo": "200", "fldQty": "20", "avgPrc": "80"})
    assert ledger.orders() == []
